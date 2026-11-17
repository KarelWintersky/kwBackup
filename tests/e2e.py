#!/usr/bin/env python3
"""End-to-end tests for dist/kwbackup.

The binary is run as a real process; rclone is replaced by a local stub that
keeps the "cloud" in a temporary directory, the database dumpers are stubs
that print SQL. Real tar/zstd/gzip are used.

    python3 tests/e2e.py [-v]
"""

import datetime as dt
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist" / "kwbackup"

FAKE_RSYNC = r'''#!/usr/bin/env python3
import os, shutil, sys
from pathlib import Path
root = Path(os.environ["FAKE_RSYNC_ROOT"])
args = sys.argv[1:]
list_only = "--list-only" in args
clean, i = [], 0
while i < len(args):
    a = args[i]
    if a in ("-a", "--list-only", "--protect-args"):
        i += 1
        continue
    if a == "-e":
        i += 2
        continue
    if a.startswith("--bwlimit="):
        i += 1
        continue
    clean.append(a)
    i += 1
args = clean

def local(p):
    if ":" in p:
        return root / p.split(":", 1)[1].lstrip("/")
    return Path(p)

if list_only:
    remote = local(args[-1])
    if remote.is_dir():
        for path in sorted(remote.rglob("*")):
            if path.is_file():
                print(f"-rw-r--r-- {path.stat().st_size} 2026/01/01 00:00:00 "
                      f"{path.relative_to(remote)}")
    sys.exit(0)
if len(args) >= 2 and ":" in args[-1]:
    dst = local(args[-1])
    dst.mkdir(parents=True, exist_ok=True)
    for src in args[:-1]:
        source = local(src)
        shutil.copy2(source, dst / source.name)
    sys.exit(0)
if len(args) == 2 and ":" in args[0]:
    src, dst = local(args[0]), Path(args[1])
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    sys.exit(0)
print("fake rsync: unsupported invocation", args, file=sys.stderr)
sys.exit(2)
'''

FAKE_SSH = r'''#!/usr/bin/env python3
import os, sys
root = os.environ["FAKE_RSYNC_ROOT"]
args = sys.argv[1:]
i = 0
while i < len(args) and args[i].startswith("-"):
    if args[i] in ("-p", "-i", "-o", "-l", "-F", "-E", "-c"):
        i += 2
    else:
        i += 1
host = args[i]
command = " ".join(args[i + 1:]).replace("/srv/backup", root + "/srv/backup")
os.execvp("/bin/sh", ["/bin/sh", "-c", command])
'''

FAKE_RCLONE = r'''#!/usr/bin/env python3
import os, shutil, sys
from pathlib import Path
CLOUD = Path(os.environ["FAKE_CLOUD"])
argv = sys.argv[1:]
if os.environ.get("FAKE_RCLONE_LOG"):
    with open(os.environ["FAKE_RCLONE_LOG"], "a") as fh:
        fh.write(" ".join(argv) + "\n")
if "--progress" in argv:
    print("Transferred: 1 KiB / 1 KiB, 100%", file=sys.stderr)
while "--config" in argv:
    i = argv.index("--config")
    if "broken" in argv[i + 1]:
        print("fake rclone: broken config", file=sys.stderr); sys.exit(1)
    del argv[i:i + 2]
COMMANDS = {"lsf", "copyto", "copy", "sync", "deletefile", "cat"}
DROP = {"--s3-no-check-bucket", "--copy-links", "--update", "--verbose", "--progress"}
idx = next(i for i, a in enumerate(argv) if a in COMMANDS)
cmd, rest = argv[idx], [a for a in argv[idx + 1:] if a not in DROP]

def resolve(p):
    p = str(p)
    if ":" in p:
        return CLOUD / p.split(":", 1)[1].lstrip("/")
    return Path(p).resolve()

def tree(d):
    return sorted(f for f in d.rglob("*") if f.is_file())

if cmd == "lsf":
    d = resolve(rest[-1])
    if not d.is_dir():
        print("directory not found", file=sys.stderr); sys.exit(3)
    for f in tree(d):
        print(f"{f.relative_to(d)};{f.stat().st_size}")
elif cmd == "copyto":
    src, dst = resolve(rest[0]), resolve(rest[-1])
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
elif cmd in ("copy", "sync"):
    src, dst = resolve(rest[0]), resolve(rest[-1])
    dst.mkdir(parents=True, exist_ok=True)
    for f in tree(src):
        target = dst / f.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, target)
    if cmd == "sync":
        for f in tree(dst):
            if not (src / f.relative_to(dst)).exists():
                f.unlink()
elif cmd == "deletefile":
    f = resolve(rest[-1])
    f.unlink(missing_ok=True)
elif cmd == "cat":
    sys.stdout.buffer.write(resolve(rest[-1]).read_bytes())
else:
    print(f"fake rclone: unsupported command {cmd}", file=sys.stderr); sys.exit(2)
'''

LOADER = r'''#!/bin/sh
cat > "$FAKE_MYSQL_OUT"
'''

DUMPER = r'''#!/bin/sh
echo "-- MySQL dump stub"
echo "-- args: $*"
echo "CREATE TABLE t (id INT);"
'''

VERBOSE = "-v" in sys.argv


class Env:
    def __init__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="kwbackup-e2e-"))
        self.bin = self.dir / "bin"
        self.cloud = self.dir / "cloud"
        self.tmp = self.dir / "tmp"
        for d in (self.bin, self.cloud, self.tmp):
            d.mkdir(parents=True)
        self._tool("rclone", FAKE_RCLONE)
        self._tool("mysqldump", DUMPER)
        self._tool("mariadb-dump", DUMPER)
        self._tool("pg_dump", DUMPER)
        self._tool("mysql", LOADER)
        self.config = self.dir / "config.yaml"

    def _tool(self, name, body):
        path = self.bin / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)

    def write_config(self, text):
        self.config.write_text(text, encoding="utf-8")
        return self.config

    def base_config(self, **extra):
        body = f"""
project: e2e
log: {{level: info}}
runtime: {{temp_dir: {self.tmp}}}
rclone:
  binary: {self.bin / "rclone"}
upstreams:
  fake: {{}}
"""
        body += extra.get("extra", "")
        return self.write_config(body)

    def run(self, *args, expect=0, extra_env=None):
        """expect=None runs without checking the exit code."""
        env = dict(os.environ)
        env["PATH"] = f"{self.bin}:{env['PATH']}"
        env["FAKE_CLOUD"] = str(self.cloud)
        env["FAKE_MYSQL_OUT"] = str(self.dir / "mysql.out")
        env["FAKE_RCLONE_LOG"] = str(self.dir / "rclone.log")
        if extra_env:
            env.update(extra_env)
        cmd = [str(DIST), *map(str, args)]
        if VERBOSE:
            print("   $", " ".join(cmd))
        res = subprocess.run(cmd, capture_output=True, text=True, env=env)
        if VERBOSE or (expect is not None and res.returncode != expect):
            if res.stdout.strip():
                print(res.stdout)
            if res.stderr.strip():
                print(res.stderr)
        if expect is not None and res.returncode != expect:
            raise AssertionError(f"expected exit {expect}, got {res.returncode}: "
                                 f"{' '.join(cmd)}")
        return res

    def cloud_tree(self):
        if not self.cloud.exists():
            return []
        return sorted(str(p.relative_to(self.cloud))
                      for p in self.cloud.rglob("*") if p.is_file())

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)


def sql(text):
    return text.encode()


def test_database_retention():
    env = Env()
    try:
        env.base_config(extra=f"""
database:
  enabled: true
  type: mysql
  compressor: zstd
  compressor_args: ["-1"]
  targets: ["fake:/DB"]
  databases: [db1]
  retention:
    daily: {{keep: 2}}
""")
        env.run("-c", env.config, "-d")
        assert len(env.cloud_tree()) == 1, env.cloud_tree()
        time.sleep(1.1)
        env.run("-c", env.config, "-d")
        time.sleep(1.1)
        env.run("-c", env.config, "-d")
        files = env.cloud_tree()
        assert len(files) == 2, files
        assert not list(env.tmp.iterdir()), "temp dir must be empty"
    finally:
        env.cleanup()


def test_database_disabled():
    env = Env()
    try:
        env.base_config(extra="""
database:
  enabled: false
  targets: ["fake:/DB"]
  databases: [db1]
  retention:
    daily: {keep: 2}
""")
        res = env.run("-c", env.config, "-d")
        assert "disabled" in res.stderr, res.stderr
        assert env.cloud_tree() == []
    finally:
        env.cleanup()


def test_storage_sync():
    env = Env()
    try:
        src = env.dir / "files"
        (src / "sub").mkdir(parents=True)
        (src / "a.txt").write_text("a")
        (src / "sub" / "b.txt").write_text("b")
        (src / ".hidden").write_text("h")
        env.base_config(extra=f"""
storage:
  enabled: true
  mode: sync
  targets: ["fake:/STORAGE"]
  sources:
    - {src}
""")
        env.run("-c", env.config, "-s")
        tree = env.cloud_tree()
        assert tree == ["STORAGE/files/.hidden", "STORAGE/files/a.txt",
                        "STORAGE/files/sub/b.txt"], tree

        (src / "a.txt").unlink()
        (src / "c.txt").write_text("c")
        env.run("-c", env.config, "-s")
        tree = env.cloud_tree()
        assert tree == ["STORAGE/files/.hidden", "STORAGE/files/c.txt",
                        "STORAGE/files/sub/b.txt"], tree
    finally:
        env.cleanup()


def test_storage_dest():
    env = Env()
    try:
        src = env.dir / "src"
        src.mkdir()
        (src / "x").write_text("x")
        env.base_config(extra=f"""
storage:
  enabled: true
  targets: ["fake:/S"]
  sources:
    - path: {src}
      dest: custom/dir
""")
        env.run("-c", env.config, "-s")
        assert env.cloud_tree() == ["S/custom/dir/x"], env.cloud_tree()
    finally:
        env.cleanup()


def tar_entries(archive):
    archive = Path(archive)
    if archive.name.endswith((".zst", ".zstd")):
        data = subprocess.run(["zstd", "-dc", str(archive)],
                              capture_output=True, check=True).stdout
    elif archive.name.endswith(".gz"):
        data = subprocess.run(["gzip", "-dc", str(archive)],
                              capture_output=True, check=True).stdout
    else:
        data = archive.read_bytes()
    out = subprocess.run(["tar", "-tf", "-"], input=data, capture_output=True, check=True)
    entries = []
    for line in out.stdout.decode().splitlines():
        line = line.rstrip("/")
        if line.startswith("./"):
            line = line[2:]
        if line and line != ".":
            entries.append(line)
    return sorted(entries)


def test_archive_contents_and_retention():
    env = Env()
    try:
        root = env.dir / "site"
        (root / "sub").mkdir(parents=True)
        (root / "index.html").write_text("<html>")
        (root / "sub" / "app.js").write_text("js")
        (root / ".env").write_text("SECRET=1")
        (root / "big.mp4").write_text("video")
        env.base_config(extra=f"""
archive:
  enabled: true
  compressor: zstd
  compressor_args: ["-1"]
  name: site
  root: {root}
  include: ["*"]
  exclude: ["*.mp4"]
  targets: ["fake:/ARCHIVE"]
  retention: {{keep: 2}}
""")
        env.run("-c", env.config, "-a")
        files = env.cloud_tree()
        assert len(files) == 1, files
        archive = env.cloud / files[0]
        entries = tar_entries(archive)
        assert "index.html" in entries, entries
        assert ".env" in entries, f"dotfiles must be archived: {entries}"
        assert "sub/app.js" in entries, entries
        assert not any(e.endswith(".mp4") for e in entries), entries
        assert not list(env.tmp.iterdir()), "temp dir must be empty"

        time.sleep(1.1)
        env.run("-c", env.config, "-a")
        time.sleep(1.1)
        env.run("-c", env.config, "-a")
        files = env.cloud_tree()
        assert len(files) == 2, files
    finally:
        env.cleanup()


def test_archive_include_list():
    env = Env()
    try:
        root = env.dir / "data"
        (root / "keep").mkdir(parents=True)
        (root / "skip").mkdir()
        (root / "keep" / "a.txt").write_text("a")
        (root / "skip" / "b.txt").write_text("b")
        (root / "top.txt").write_text("t")
        env.base_config(extra=f"""
archive:
  enabled: true
  compressor: gzip
  name: data
  root: {root}
  include: ["keep/*", "top.txt"]
  targets: ["fake:/A"]
""")
        env.run("-c", env.config, "-a")
        archive = env.cloud / env.cloud_tree()[0]
        data = subprocess.run(["gzip", "-dc", str(archive)], capture_output=True, check=True).stdout
        out = subprocess.run(["tar", "-tf", "-"], input=data, capture_output=True, check=True)
        entries = [line.rstrip("/") for line in out.stdout.decode().splitlines()
                   if line.strip("./")]
        assert "keep/a.txt" in entries, entries
        assert "top.txt" in entries, entries
        assert not any("skip" in e for e in entries), entries
    finally:
        env.cleanup()


def test_list_and_verify():
    env = Env()
    try:
        root = env.dir / "site"
        root.mkdir()
        (root / "a.txt").write_text("a")
        env.base_config(extra=f"""
database:
  enabled: true
  type: mysql
  compressor: zstd
  compressor_args: ["-1"]
  targets: ["fake:/DB"]
  databases: [db1]
  retention:
    daily: {{keep: 3}}
archive:
  enabled: true
  compressor: zstd
  compressor_args: ["-1"]
  name: site
  root: {root}
  targets: ["fake:/ARCHIVE"]
""")
        env.run("-c", env.config, "-d", "-a")
        out = env.run("-c", env.config, "--list")
        assert "DATABASE db1" in out.stdout, out.stdout
        assert "DAILY" in out.stdout
        assert "ARCHIVE" in out.stdout
        res = env.run("-c", env.config, "--verify")
        assert res.stdout.count("✓") == 2, res.stdout
    finally:
        env.cleanup()


def test_verify_detects_corruption():
    env = Env()
    try:
        env.base_config(extra="""
database:
  enabled: true
  type: mysql
  compressor: zstd
  targets: ["fake:/DB"]
  databases: [db1]
  retention:
    daily: {keep: 3}
""")
        env.run("-c", env.config, "-d")
        victim = env.cloud / env.cloud_tree()[0]
        good = victim.stat().st_size
        victim.write_bytes(b"x" * good)
        res = env.run("-c", env.config, "--verify", expect=5)
        assert "✗" in res.stdout, res.stdout
    finally:
        env.cleanup()


def test_restore_database():
    env = Env()
    try:
        env.base_config(extra="""
database:
  enabled: true
  type: mysql
  compressor: zstd
  compressor_args: ["-1"]
  targets: ["fake:/DB"]
  databases: [db1]
  retention:
    daily: {keep: 3}
""")
        env.run("-c", env.config, "-d")
        out = env.dir / "restore"
        env.run("-c", env.config, "--restore", "-d", "--to", out)
        files = list(out.glob("*.sql"))
        assert len(files) == 1, files
        assert b"MySQL dump" in files[0].read_bytes()
        assert not list(env.tmp.iterdir()), "temp dir must be empty"
    finally:
        env.cleanup()


def test_restore_into_db():
    env = Env()
    try:
        env.base_config(extra="""
database:
  enabled: true
  type: mysql
  compressor: zstd
  targets: ["fake:/DB"]
  databases: [db1]
  retention:
    daily: {keep: 3}
""")
        env.run("-c", env.config, "-d")
        env.run("-c", env.config, "--restore", "-d", "--into-db", "db1", "--yes")
        captured = (env.dir / "mysql.out").read_bytes()
        assert b"MySQL dump" in captured, captured
    finally:
        env.cleanup()


def test_restore_requires_confirmation():
    env = Env()
    try:
        env.base_config(extra="""
database:
  enabled: true
  type: mysql
  targets: ["fake:/DB"]
  databases: [db1]
  retention:
    daily: {keep: 3}
""")
        env.run("-c", env.config, "-d")
        env.run("-c", env.config, "--restore", "-d", "--into-db", "db1", expect=1)
    finally:
        env.cleanup()


def test_restore_archive_and_storage():
    env = Env()
    try:
        root = env.dir / "site"
        (root / "sub").mkdir(parents=True)
        (root / "index.html").write_text("html")
        (root / "sub" / "a.js").write_text("js")
        storage_src = env.dir / "files"
        storage_src.mkdir()
        (storage_src / "one.txt").write_text("1")
        env.base_config(extra=f"""
storage:
  enabled: true
  targets: ["fake:/STORAGE"]
  sources: [{storage_src}]
archive:
  enabled: true
  compressor: zstd
  name: site
  root: {root}
  targets: ["fake:/ARCHIVE"]
  retention: {{keep: 2}}
""")
        env.run("-c", env.config, "-a", "-s")
        out = env.dir / "restored"
        env.run("-c", env.config, "--restore", "-a", "-s", "--to", out)
        assert (out / "index.html").read_text() == "html"
        assert (out / "sub" / "a.js").read_text() == "js"
        assert (out / "files" / "one.txt").read_text() == "1", list(out.rglob("*"))
    finally:
        env.cleanup()


def test_restore_at_filter():
    env = Env()
    try:
        env.base_config(extra="""
database:
  enabled: true
  type: mysql
  compressor: zstd
  targets: ["fake:/DB"]
  databases: [db1]
  retention:
    daily: {keep: 3}
""")
        env.run("-c", env.config, "-d")
        env.run("-c", env.config, "--restore", "-d", "--to", env.dir / "r",
                "--at", "1999-01-01", expect=1)
    finally:
        env.cleanup()


OLD_CONFIG = r"""#!/usr/bin/env bash
export NOW=`date '+%F-%H-%M-%S'`
export TEMP_PATH=/tmp
export LOG_FILE=/var/log/kwbackup.log
export USE_ARCHIVER=zstd
export RCLONE_CONFIG=/etc/kwbackup/rclone.conf
export RCLONE_PROVIDER=beget
export RCLONE_CUSTOM_OPTIONS=("--s3-access-key-id=XXX" "--s3-no-check-bucket")
export ENABLE_BACKUP_DATABASE=1
export DB_TYPE=mysql
export DB_HOST=localhost
export DATABASES=(db1 db2)
export DATABASE_MYSQL_OPTIONS="-Q --single-transaction"
export DATABASE_ZSTD_OPTIONS="-9"
export DB_BACKUP_DAILY=1
export DB_BACKUP_WEEKLY=1
export DB_BACKUP_MONTHLY=0
export DB_MIN_AGE_DAILY=7d
export DB_MIN_AGE_WEEKLY=43d
export DB_MIN_AGE_MONTHLY=360d
export DATABASE_CLOUD_CONTAINER="/DB"
export ENABLE_BACKUP_STORAGE=1
export STORAGE_SOURCES_ROOT=/srv
export STORAGE_SOURCES=(files.aait files.etks)
export STORAGE_CLOUD_CONTAINER="/STORAGE"
export STORAGE_BACKUP_ALGO="sync"
export ENABLE_BACKUP_ARCHIVE=1
export ARCHIVE_USE_COMPRESSOR="rar"
export ARCHIVE_RAR_OPTIONS="-r -s -m5"
export ARCHIVE_FILENAME=site_${NOW}
export ARCHIVE_ROOT=/srv/site
export ARCHIVE_INCLUDE_LIST=("*")
export ARCHIVE_EXCLUDE_LIST=("*.mp4")
export ARCHIVE_CLOUD_CONTAINER="/ARCHIVE"
export ARCHIVE_ROTATION_PERIOD="71d"
"""


def test_migrate_old_config():
    env = Env()
    try:
        old = env.dir / "old.conf"
        old.write_text(OLD_CONFIG, encoding="utf-8")
        out = env.dir / "new.yaml"
        env.run("migrate", old, "-o", out, "-p", "legacy")
        text = out.read_text()
        for expected in ("project: legacy", "- db1", "- db2", "keep: 6", "monthly: false",
                         "/srv/files.aait", "name: site", "retention: {keep: 71}",
                         "compressor: zstd"):
            assert expected in text, f"missing {expected!r} in:\n{text}"
        res = env.run("-c", out, "--check", expect=None)
        assert res.returncode in (0, 1), res.returncode
        assert "invalid config" not in res.stderr, res.stderr
        env.run("migrate", old, "-o", out, expect=3)      # no --force
        env.run("migrate", old, "-o", out, "-f")
    finally:
        env.cleanup()


def test_migrate_uses_keep_by_default():
    env = Env()
    try:
        old = env.dir / "c.conf"
        old.write_text('export ENABLE_BACKUP_DATABASE=1\n'
                       'export DB_TYPE=postgres\n'
                       'export DATABASES=(pg1)\n'
                       'export DB_BACKUP_DAILY=1\n'
                       'export DB_MIN_AGE_DAILY=14d\n'
                       'export DATABASE_CLOUD_CONTAINER="/PG"\n',
                       encoding="utf-8")
        out = env.dir / "o.yaml"
        env.run("migrate", old, "-o", out)
        text = out.read_text()
        assert "keep: 14" in text, text
        assert "type: postgres" in text, text
    finally:
        env.cleanup()


def test_documented_examples_validate():
    """Every examples/*.yaml must pass --check."""
    env = Env()
    try:
        secret_env = {"RCLONE_KEY_ID": "k", "RCLONE_SECRET": "s", "MYSQL_PWD": "p"}
        files = [p for p in sorted((ROOT / "examples").glob("*.yaml"))
                 if p.name != "defaults.yaml"]   # defaults are not a task
        assert files, "no example configs found"
        for path in files:
            res = env.run("-c", path, "--check",
                          "--defaults", ROOT / "debian" / "kwbackup.defaults.yaml",
                          extra_env=secret_env, expect=None)
            # Examples point at real host paths, so preflight may fail (1);
            # what must never happen is a config error (2).
            assert res.returncode in (0, 1), (path, res.returncode)
            assert "invalid config" not in res.stderr, (path, res.stderr)
    finally:
        env.cleanup()


def test_check_fails_on_preflight_problems():
    env = Env()
    try:
        env.write_config(f"""
project: checkpre
log: {{level: info}}
runtime: {{temp_dir: {env.tmp}}}
rclone: {{binary: /nonexistent/rclone}}
upstreams: {{fake: {{}}}}
database:
  enabled: true
  targets: ["fake:/DB"]
  databases: [db1]
  retention: {{daily: {{keep: 1}}}}
""")
        res = env.run("-c", env.config, "--check", expect=1)
        assert "preflight problems" in res.stdout, res.stdout
        assert "binary not found in PATH: /nonexistent/rclone" in res.stdout, res.stdout
    finally:
        env.cleanup()


def test_check_preflight_failure_notifies():
    env = Env()
    try:
        with _HookServer() as hook:
            env.write_config(f"""
project: checknotify
log: {{level: info}}
runtime: {{temp_dir: {env.tmp}}}
rclone: {{binary: /nonexistent/rclone}}
upstreams: {{fake: {{}}}}
database:
  enabled: true
  targets: ["fake:/DB"]
  databases: [db1]
  retention: {{daily: {{keep: 1}}}}
notify:
  on: [failure]
  webhook: {{url: "{hook.url}"}}
""")
            env.run("-c", env.config, "--check", expect=1)
            assert len(hook.requests) == 1, hook.requests
            body = hook.requests[0]
            assert "check failed (exit 1)" in body["subject"], body["subject"]
            assert "binary not found" in body["text"], body["text"]
    finally:
        env.cleanup()


def test_storage_sync_refuses_empty_source():
    env = Env()
    try:
        src = env.dir / "empty"
        src.mkdir()
        cloud = env.cloud / "ST" / "empty"      # dest defaults to the basename
        cloud.mkdir(parents=True)
        (cloud / "old.txt").write_text("keep me", encoding="utf-8")
        env.base_config(extra=f"""
storage:
  enabled: true
  mode: sync
  sources: ["{src}"]
  targets: ["fake:/ST"]
""")
        res = env.run("-c", env.config, "-s", expect=1)
        assert "is empty" in res.stderr and "allow_empty" in res.stderr, res.stderr
        assert (cloud / "old.txt").exists(), "the remote must not be touched"
    finally:
        env.cleanup()


def test_storage_sync_allows_empty_source_when_flagged():
    env = Env()
    try:
        src = env.dir / "empty"
        src.mkdir()
        cloud = env.cloud / "ST" / "empty"
        cloud.mkdir(parents=True)
        (cloud / "old.txt").write_text("delete me", encoding="utf-8")
        env.base_config(extra=f"""
storage:
  enabled: true
  mode: sync
  allow_empty: true
  sources: ["{src}"]
  targets: ["fake:/ST"]
""")
        env.run("-c", env.config, "-s")
        assert not (cloud / "old.txt").exists(), "sync mirrors the empty source"
    finally:
        env.cleanup()


def test_defaults_file_layer():
    env = Env()
    try:
        defaults = env.dir / "defaults.yaml"
        defaults.write_text(f"""
log: {{level: warning}}
rclone:
  binary: {env.bin / "rclone"}
  args: ["--s3-no-check-bucket"]
upstreams:
  fake: {{}}
database:
  type: mysql
  compressor: zstd
  compressor_args: ["-1"]
  retention:
    daily: {{keep: 1}}
""", encoding="utf-8")
        task = env.dir / "task.yaml"
        task.write_text("""
project: layered
rclone:
  args:
    - "--s3-access-key-id=KEY1"
    - "--s3-secret-access-key=SECRET1"
database:
  enabled: true
  targets: ["fake:/DB"]
  databases: [db1]
""", encoding="utf-8")
        base = ["-c", task, "--defaults", defaults]

        out = env.run(*base, "--check")
        assert "defaults.yaml" in out.stdout, out.stdout
        assert "keep 1" in out.stdout, out.stdout

        env.run(*base, "-d")
        log = (env.dir / "rclone.log").read_text()
        assert "--s3-no-check-bucket" in log, log           # from defaults
        assert "--s3-access-key-id=KEY1" in log, log        # from the task, appended
        assert "--s3-secret-access-key=SECRET1" in log, log

        env.run("-c", task, "--defaults", env.dir / "missing.yaml",
                "--check", expect=2)
    finally:
        env.cleanup()


def test_builtin_defaults_only():
    """Without /etc/kwbackup/defaults.yaml the built-in defaults apply."""
    env = Env()
    try:
        env.base_config(extra="""
database:
  enabled: true
  targets: ["fake:/DB"]
  databases: [db1]
  retention:
    daily: {keep: 2}
""")
        out = env.run("-c", env.config, "--check")
        assert "built-in defaults only" in out.stdout, out.stdout
        assert "compressor zstd" in out.stdout, out.stdout      # builtin value
        env.run("-c", env.config, "-d")
        assert env.cloud_tree()[0].endswith(".sql.zst"), env.cloud_tree()
    finally:
        env.cleanup()


def test_build_version_and_shebang():
    """build.py substitutes the version and produces a -B shebang."""
    env = Env()
    try:
        out = env.dir / "kwbackup-built"
        res = subprocess.run(
            [sys.executable, str(ROOT / "build.py"), "--version", "9.9.9",
             "-o", str(out)],
            capture_output=True, text=True)
        assert res.returncode == 0, res.stderr
        assert out.read_text(encoding="utf-8").splitlines()[0] == "#!/usr/bin/python3 -B"
        ver = subprocess.run([str(out), "--version"], capture_output=True, text=True)
        assert ver.stdout.strip() == "kwBackup (Version: 9.9.9)", ver.stdout
    finally:
        env.cleanup()


def _artifact():
    """Import the built single-file tool as a module (main() is guarded)."""
    import importlib.util
    from importlib.machinery import SourceFileLoader
    saved = sys.dont_write_bytecode
    sys.dont_write_bytecode = True          # never leave dist/__pycache__
    try:
        loader = SourceFileLoader("kwbackup_built", str(DIST))
        spec = importlib.util.spec_from_loader("kwbackup_built", loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        return module
    finally:
        sys.dont_write_bytecode = saved


def _key_paths(data, prefix=""):
    out = set()
    for key, value in data.items():
        path = f"{prefix}{key}"
        out.add(path)
        if isinstance(value, dict):
            out |= _key_paths(value, path + ".")
    return out


def _value(data, path):
    cur = data
    for part in path.split("."):
        cur = cur[part]
    return cur


def test_defaults_file_covers_builtins():
    """The shipped defaults file must be at least as complete as BUILTIN_DEFAULTS."""
    kw = _artifact()
    raw = kw.yaml_loads((ROOT / "debian" / "kwbackup.defaults.yaml").read_text(encoding="utf-8"))
    missing = _key_paths(kw.BUILTIN_DEFAULTS) - _key_paths(raw)
    assert not missing, f"debian/kwbackup.defaults.yaml misses built-in keys: {sorted(missing)}"
    for path in ("runtime.temp_dir", "runtime.keep_temp", "rclone.binary",
                 "database.mode", "archive.mode", "storage.mode",
                 "archive.include", "archive.exclude"):
        assert _value(raw, path) == _value(kw.BUILTIN_DEFAULTS, path), path
    assert "retention" in raw["database"], "database retention must be a host default"
    assert "retention" not in raw["archive"], "archive is not pruned by default"


def test_mode_copy_in_config():
    env = Env()
    try:
        env.base_config(extra="""
database:
  enabled: true
  mode: copy
  targets: ["fake:/DB"]
  databases: [db1]
  retention: {daily: {keep: 2}}
archive:
  enabled: true
  mode: copy
  name: site
  root: /tmp
  targets: ["fake:/ARCHIVE"]
""")
        out = env.run("-c", env.config, "--check")
        assert "mode" in out.stdout and "copy" in out.stdout, out.stdout
    finally:
        env.cleanup()


def test_mode_sync_rejected_in_config():
    env = Env()
    try:
        base = """
project: mirror
rclone: {binary: %s}
upstreams: {fake: {}}
""" % (env.bin / "rclone")
        task = env.dir / "task.yaml"
        task.write_text(base + """
database:
  enabled: true
  mode: sync
  targets: ["fake:/DB"]
  databases: [db1]
  retention: {daily: {keep: 2}}
""", encoding="utf-8")
        res = env.run("-c", task, "--check", expect=2)
        assert "only 'copy' is supported" in res.stderr, res.stderr
    finally:
        env.cleanup()


def test_archive_sync_mirrors():
    env = Env()
    try:
        root = env.dir / "site"
        root.mkdir()
        (root / "a.txt").write_text("a")
        env.base_config(extra=f"""
archive:
  enabled: true
  compressor: zstd
  name: site
  root: {root}
  targets: ["fake:/ARCHIVE"]
  retention: {{keep: 5}}
""")
        env.run("-c", env.config, "-a")
        time.sleep(1.1)
        env.run("-c", env.config, "-a", "-m", "sync")
        files = env.cloud_tree()
        assert len(files) == 1, files          # mirror deleted the older archive
        env.run("-c", env.config, "--verify")  # and the survivor is valid
    finally:
        env.cleanup()


def test_database_sync_mirrors():
    env = Env()
    try:
        env.base_config(extra="""
database:
  enabled: true
  targets: ["fake:/DB"]
  databases: [db1]
  retention: {daily: {keep: 5}}
""")
        env.run("-c", env.config, "-d")
        time.sleep(1.1)
        env.run("-c", env.config, "-d", "-m", "sync")
        files = env.cloud_tree()
        assert len(files) == 1, files          # one copy per class dir
    finally:
        env.cleanup()


def test_no_action_does_nothing():
    """No action flag: print the list, run nothing, touch nothing."""
    env = Env()
    try:
        src = env.dir / "files"
        src.mkdir()
        (src / "a.txt").write_text("a")
        env.base_config(extra=f"""
storage:
  enabled: true
  targets: ["fake:/S"]
  sources: [{src}]
""")
        out = env.run("-c", env.config)
        assert "kwBackup: Backup tool over rclone" in out.stdout, out.stdout
        assert "Version: " in out.stdout, out.stdout
        assert "No one backup action(s) requested. Allowed:" in out.stdout, out.stdout
        assert "(use --storage  option)  enabled" in out.stdout, out.stdout
        assert "(use --database option)  not configured" in out.stdout, out.stdout
        assert "(use --archive  option)  not configured" in out.stdout, out.stdout
        assert "--check" in out.stdout, out.stdout
        assert env.cloud_tree() == [], "nothing must be uploaded"

        # a section that exists but is switched off is "disabled", not "not configured"
        env.config.write_text(env.config.read_text(encoding="utf-8") + """
database:
  enabled: false
  targets: ["fake:/DB"]
  databases: [db1]
  retention: {daily: {keep: 1}}
""", encoding="utf-8")
        out = env.run("-c", env.config)
        assert "(use --database option)  disabled" in out.stdout, out.stdout
    finally:
        env.cleanup()


def test_verbose_reaches_rclone():
    env = Env()
    try:
        env.base_config(extra="""
database:
  enabled: true
  targets: ["fake:/DB"]
  databases: [db1]
  retention: {daily: {keep: 2}}
""")
        rlog = env.dir / "rclone.log"

        res = env.run("-c", env.config, "-d")
        assert "--progress" not in rlog.read_text(), "quiet by default"
        assert "Transferred:" not in res.stderr, "rclone output stays hidden"
        assert "run:" not in res.stderr, "command lines are debug-only"

        rlog.unlink()
        res = env.run("-c", env.config, "-d", "--verbose")
        assert "--progress" in rlog.read_text(), "--verbose must reach rclone"
        assert "Transferred:" in res.stderr, "rclone progress must reach the terminal"
        assert "\n\nTransferred:" in res.stderr, "empty line before rclone output"
        assert "100%\n\n" in res.stderr, "empty line after rclone output"
        assert "run:" not in res.stderr, "--verbose must not turn on debug logging"

        rlog.unlink()
        res = env.run("-c", env.config, "-d", "--debug")
        assert "--progress" in rlog.read_text(), "--debug implies verbose for rclone"
        assert "Transferred:" in res.stderr, "progress under --debug too"
        assert "run:" in res.stderr, "--debug prints every command"
    finally:
        env.cleanup()


def test_no_args_shows_banner():
    env = Env()
    try:
        res = env.run(expect=3)
        out = res.stdout + res.stderr
        assert "kwBackup: Backup tool over rclone: database dumps, file storage, archives" in out, out
        assert "Version: " in out, out
        assert "ERROR: -c/--config is required" in out, out
    finally:
        env.cleanup()


def test_archive_name_defaults_to_project():
    env = Env()
    try:
        root = env.dir / "site"
        root.mkdir()
        (root / "a.txt").write_text("a")
        env.base_config(extra=f"""
archive:
  enabled: true
  compressor: zstd
  root: {root}
  targets: ["fake:/A"]
""")
        out = env.run("-c", env.config, "--check")
        assert "name" in out.stdout and "e2e" in out.stdout  # project: e2e
        env.run("-c", env.config, "-a")
        assert env.cloud_tree()[0].startswith("A/e2e_"), env.cloud_tree()
    finally:
        env.cleanup()


def test_shared_target_keeps_foreign_archives():
    """Rotation must only touch files with our own <name>_ prefix."""
    env = Env()
    try:
        root = env.dir / "site"
        root.mkdir()
        (root / "a.txt").write_text("a")

        def config(project):
            path = env.dir / f"{project}.yaml"
            path.write_text(f"""
project: {project}
rclone: {{binary: {env.bin / "rclone"}}}
upstreams: {{fake: {{}}}}
archive:
  enabled: true
  compressor: zstd
  root: {root}
  targets: ["fake:/SHARED"]
  retention: {{keep: 1}}
""", encoding="utf-8")
            return path

        alpha, beta = config("alpha"), config("beta")
        env.run("-c", alpha, "-a")
        env.run("-c", beta, "-a")
        time.sleep(1.1)
        env.run("-c", alpha, "-a")
        names = [Path(item).name for item in env.cloud_tree()]
        assert sum(n.startswith("alpha_") for n in names) == 1, names
        assert sum(n.startswith("beta_") for n in names) == 1, names
    finally:
        env.cleanup()


def test_shared_target_keeps_foreign_databases():
    """Same for dumps: two single-db configs share <target>/DAILY."""
    env = Env()
    try:
        def config(dbname):
            path = env.dir / f"{dbname}.yaml"
            path.write_text(f"""
project: {dbname}
rclone: {{binary: {env.bin / "rclone"}}}
upstreams: {{fake: {{}}}}
database:
  enabled: true
  targets: ["fake:/DB"]
  databases: [{dbname}]
  retention: {{daily: {{keep: 1}}}}
""", encoding="utf-8")
            return path

        one, two = config("db1"), config("db2")
        env.run("-c", one, "-d")
        env.run("-c", two, "-d")
        time.sleep(1.1)
        env.run("-c", one, "-d")
        names = [Path(item).name for item in env.cloud_tree()]
        assert sum(n.startswith("db1_") for n in names) == 1, names
        assert sum(n.startswith("db2_") for n in names) == 1, names
    finally:
        env.cleanup()


def test_unknown_key_is_a_warning():
    """A leftover key must not stop a backup; it is reported and ignored."""
    env = Env()
    try:
        env.base_config(extra="""
version: 1
database:
  enabled: true
  targets: ["fake:/DB"]
  databases: [db1]
  retention: {daily: {keep: 2}}
""")
        out = env.run("-c", env.config, "--check")
        assert "unknown key, ignored" in out.stdout, out.stdout
        assert "config.version" in out.stdout, out.stdout

        res = env.run("-c", env.config, "-d")
        assert "unknown key, ignored" in res.stderr, res.stderr   # warning is visible
        assert len(env.cloud_tree()) == 1, env.cloud_tree()
    finally:
        env.cleanup()


def test_s3_custom_options():
    """Global flags, section overrides, booleans and null removal."""
    env = Env()
    try:
        src = env.dir / "files"
        src.mkdir()
        (src / "a.txt").write_text("a")
        env.write_config(f"""
project: s3opts
log: {{level: warning}}
runtime: {{temp_dir: {env.tmp}}}
rclone:
  binary: {env.bin / "rclone"}
  args: ["--s3-upload-cutoff=1G"]
  s3-custom-options:
    s3-upload-cutoff: 200M
    s3-chunk-size: 5M
    s3-no-check-bucket: true
    s3-server-side-encryption: AES256
upstreams:
  fake: {{}}
storage:
  enabled: true
  targets: ["fake:/S"]
  sources: [{src}]
  s3-custom-options:
    s3-access-key-id: KEY2
    s3-secret-access-key: SECRET2
    s3-server-side-encryption: null
    s3-no-check-bucket: false
""")
        out = env.run("-c", env.config, "--check")
        assert "s3-chunk-size" in out.stdout and "s3-no-check-bucket" in out.stdout, out.stdout
        assert "+s3-secret-access-key" in out.stdout, out.stdout
        assert "-s3-server-side-encryption" in out.stdout, out.stdout

        env.run("-c", env.config, "-s")
        line = [l for l in (env.dir / "rclone.log").read_text().splitlines()
                if " sync " in l][-1]
        argv = line.split()
        assert "--s3-upload-cutoff=200M" in argv, argv       # overrides args
        assert "--s3-upload-cutoff=1G" in argv, argv         # simple path kept
        assert argv.index("--s3-upload-cutoff=200M") > argv.index("--s3-upload-cutoff=1G")
        assert "--s3-chunk-size=5M" in argv, argv
        assert "--s3-no-check-bucket=false" in argv, argv    # boolean false is explicit
        assert "--s3-no-check-bucket" not in argv, argv
        assert "--s3-access-key-id=KEY2" in argv, argv       # section keys
        assert "--s3-secret-access-key=SECRET2" in argv, argv
        assert not any(a.startswith("--s3-server-side-encryption") for a in argv), argv
    finally:
        env.cleanup()


def test_quiet_by_default():
    """No flags: the terminal is silent, info lands in the log file."""
    env = Env()
    try:
        logfile = env.dir / "kwbackup.log"
        env.write_config(f"""
project: quiet
log: {{level: info, file: {logfile}}}
runtime: {{temp_dir: {env.tmp}}}
rclone: {{binary: {env.bin / "rclone"}}}
upstreams: {{fake: {{}}}}
database:
  enabled: true
  targets: ["fake:/DB"]
  databases: [db1]
  retention: {{daily: {{keep: 2}}}}
""")
        res = env.run("-c", env.config, "-d")
        assert "dumping" not in res.stderr, res.stderr
        assert "done in" not in res.stderr, res.stderr
        log = logfile.read_text(encoding="utf-8")
        assert "dumping" in log, log
        assert "done in" in log, log

        res = env.run("-c", env.config, "-d", "--verbose")
        assert "dumping" in res.stderr, res.stderr
        assert "done in" in res.stderr, res.stderr

        # dry-run prints the plan even without --verbose
        res = env.run("-c", env.config, "-d", "--dry-run")
        assert "dry-run:" in res.stderr, res.stderr
    finally:
        env.cleanup()


def _multi_config(env, upstreams, sections, root=None):
    body = f"""
project: multi
log: {{level: warning}}
runtime: {{temp_dir: {env.tmp}}}
rclone: {{binary: {env.bin / "rclone"}}}
upstreams:
"""
    for name, config in upstreams.items():
        Path(config).write_text("", encoding="utf-8")
        body += f"  {name}: {{config: {config}}}\n"
    body += sections
    return env.write_config(body)


def test_multiple_upstreams_upload():
    """One archive, two upstreams, each with its own rclone config."""
    env = Env()
    try:
        root = env.dir / "site"
        root.mkdir()
        (root / "a.txt").write_text("a")
        cfg_a = env.dir / "a.conf"
        cfg_b = env.dir / "b.conf"
        _multi_config(env, {"a": cfg_a, "b": cfg_b}, f"""
archive:
  enabled: true
  compressor: zstd
  root: {root}
  targets: [a:/A1, b:/A2]
  retention: {{keep: 2}}
""")
        env.run("-c", env.config, "-a")
        tree = env.cloud_tree()
        assert any(p.startswith("A1/") for p in tree), tree
        assert any(p.startswith("A2/") for p in tree), tree
        log = (env.dir / "rclone.log").read_text()
        assert f"--config {cfg_a}" in log, log
        assert f"--config {cfg_b}" in log, log
    finally:
        env.cleanup()


def test_upstream_failure_keeps_going():
    """A broken upstream is logged and the healthy one still gets the file."""
    env = Env()
    try:
        root = env.dir / "site"
        root.mkdir()
        (root / "a.txt").write_text("a")
        _multi_config(env, {"good": env.dir / "good.conf",
                            "bad": env.dir / "broken.conf"}, f"""
archive:
  enabled: true
  compressor: zstd
  root: {root}
  targets: [good:/G, bad:/B]
""")
        res = env.run("-c", env.config, "-a", expect=1)
        assert "upstream bad" in res.stderr, res.stderr
        tree = env.cloud_tree()
        assert any(p.startswith("G/") for p in tree), tree
        assert not any(p.startswith("B/") for p in tree), tree
    finally:
        env.cleanup()


def test_restore_from_upstream():
    env = Env()
    try:
        (env.dir / "a.conf").write_text("", encoding="utf-8")
        (env.dir / "b.conf").write_text("", encoding="utf-8")
        env.write_config(f"""
project: multi
log: {{level: warning}}
runtime: {{temp_dir: {env.tmp}}}
rclone: {{binary: {env.bin / "rclone"}}}
upstreams:
  a: {{config: {env.dir / "a.conf"}}}
  b: {{config: {env.dir / "b.conf"}}}
database:
  enabled: true
  targets: [a:/A, b:/B]
  databases: [db1]
  retention: {{daily: {{keep: 2}}}}
""")
        env.run("-c", env.config, "-d")
        out = env.dir / "restore"
        env.run("-c", env.config, "--restore", "-d", "--from", "b", "--to", out)
        assert list(out.glob("*.sql")), list(out.glob("*"))
        log = (env.dir / "rclone.log").read_text().splitlines()
        assert any("b:/B/DAILY/" in line and "copyto" in line for line in log), log[-3:]
    finally:
        env.cleanup()


def test_unknown_upstream_rejected():
    env = Env()
    try:
        env.write_config(f"""
project: bad
rclone: {{binary: {env.bin / "rclone"}}}
upstreams: {{a: {{}}}}
database:
  enabled: true
  targets: [nope:/DB]
  databases: [db1]
  retention: {{daily: {{keep: 1}}}}
""")
        res = env.run("-c", env.config, "--check", expect=2)
        assert "unknown upstream 'nope'" in res.stderr, res.stderr
    finally:
        env.cleanup()


def _incremental_config(env, root, snapshot, retention="keep: 3"):
    return env.write_config(f"""
project: inc
log: {{level: warning}}
runtime: {{temp_dir: {env.tmp}}}
rclone: {{binary: {env.bin / "rclone"}}}
upstreams: {{fake: {{}}}}
incremental:
  enabled: true
  name: data
  root: {root}
  initial_day: 1
  snapshot: {snapshot}
  targets: ["fake:/INCR"]
  retention: {{{retention}}}
""")


def test_incremental_full_then_increment():
    env = Env()
    try:
        root = env.dir / "data"
        root.mkdir()
        (root / "a.txt").write_text("a")
        snap = env.dir / "state" / "data.snar"
        _incremental_config(env, root, snap)

        env.run("-c", env.config, "-i")
        tree = env.cloud_tree()
        fulls = [p for p in tree if "_full_" in p]
        assert len(fulls) == 1, tree
        assert not any("_inc_" in p for p in tree), tree
        assert "a.txt" in tar_entries(env.cloud / fulls[0])

        time.sleep(1.1)
        env.run("-c", env.config, "-i")
        tree = env.cloud_tree()
        incs = [p for p in tree if "_inc_" in p]
        assert len(incs) == 1, tree
        # nothing changed: the increment carries no regular files
        assert "a.txt" not in tar_entries(env.cloud / incs[0]), tar_entries(env.cloud / incs[0])
        assert snap.exists(), "snapshot file must be kept between runs"
    finally:
        env.cleanup()


def test_incremental_changed_file_lands_in_increment():
    env = Env()
    try:
        root = env.dir / "data"
        root.mkdir()
        (root / "a.txt").write_text("a")
        snap = env.dir / "state" / "data.snar"
        _incremental_config(env, root, snap)
        env.run("-c", env.config, "-i")

        (root / "b.txt").write_text("b")
        time.sleep(1.1)
        env.run("-c", env.config, "-i")
        inc = [p for p in env.cloud_tree() if "_inc_" in p][0]
        entries = tar_entries(env.cloud / inc)
        assert "b.txt" in entries, entries
        assert "a.txt" not in entries, entries
    finally:
        env.cleanup()


def test_incremental_retention_drops_whole_chain():
    env = Env()
    try:
        root = env.dir / "data"
        root.mkdir()
        (root / "a.txt").write_text("a")
        snap = env.dir / "state" / "data.snar"
        # seed an old chain
        old_dir = env.cloud / "INCR"
        old_dir.mkdir(parents=True)
        (old_dir / "data_2020-01_full_2020-01-01-00-00-00.tar").write_bytes(b"old")
        (old_dir / "data_2020-01_inc_2020-01-02-00-00-00.tar").write_bytes(b"old")
        _incremental_config(env, root, snap, retention="keep: 1")
        env.run("-c", env.config, "-i")
        tree = env.cloud_tree()
        assert not any("2020-01" in p for p in tree), tree
        assert any("_full_" in p for p in tree), tree
        assert not any("_inc_" in p for p in tree), tree
    finally:
        env.cleanup()


def test_incremental_missing_snapshot_forces_full():
    env = Env()
    try:
        root = env.dir / "data"
        root.mkdir()
        (root / "a.txt").write_text("a")
        snap = env.dir / "state" / "data.snar"
        _incremental_config(env, root, snap)
        env.run("-c", env.config, "-i")
        snap.unlink()
        time.sleep(1.1)
        env.run("-c", env.config, "-i")
        fulls = [p for p in env.cloud_tree() if "_full_" in p]
        assert len(fulls) == 2, env.cloud_tree()
    finally:
        env.cleanup()


def test_incremental_restore_chain():
    env = Env()
    try:
        root = env.dir / "data"
        root.mkdir()
        (root / "a.txt").write_text("a")
        snap = env.dir / "state" / "data.snar"
        _incremental_config(env, root, snap)
        env.run("-c", env.config, "-i")
        (root / "b.txt").write_text("b")
        time.sleep(1.1)
        env.run("-c", env.config, "-i")

        out = env.dir / "restored"
        env.run("-c", env.config, "--restore", "-i", "--to", out)
        assert (out / "a.txt").read_text() == "a", list(out.rglob("*"))
        assert (out / "b.txt").read_text() == "b", list(out.rglob("*"))
    finally:
        env.cleanup()


def test_incremental_restore_applies_deletion():
    """GNU tar records removed files in the chain; restore must drop them."""
    env = Env()
    try:
        root = env.dir / "data"
        root.mkdir()
        (root / "keep.txt").write_text("k")
        (root / "gone.txt").write_text("g")
        snap = env.dir / "state" / "data.snar"
        _incremental_config(env, root, snap)
        env.run("-c", env.config, "-i")
        (root / "gone.txt").unlink()
        time.sleep(1.1)
        env.run("-c", env.config, "-i")

        out = env.dir / "restored"
        env.run("-c", env.config, "--restore", "-i", "--to", out)
        assert (out / "keep.txt").exists(), list(out.rglob("*"))
        assert not (out / "gone.txt").exists(), "deleted file must not be restored"
    finally:
        env.cleanup()


def test_incremental_list_and_verify():
    env = Env()
    try:
        root = env.dir / "data"
        root.mkdir()
        (root / "a.txt").write_text("a")
        snap = env.dir / "state" / "data.snar"
        _incremental_config(env, root, snap)
        env.run("-c", env.config, "-i")
        (root / "b.txt").write_text("b")
        time.sleep(1.1)
        env.run("-c", env.config, "-i")

        out = env.run("-c", env.config, "--list")
        assert "INCREMENTAL" in out.stdout, out.stdout
        assert "1 inc" in out.stdout, out.stdout

        res = env.run("-c", env.config, "--verify")
        assert res.stdout.count("✓") == 2, res.stdout
    finally:
        env.cleanup()


def test_incremental_at():
    env = Env()
    try:
        root = env.dir / "data"
        root.mkdir()
        (root / "a.txt").write_text("a")
        snap = env.dir / "state" / "data.snar"
        _incremental_config(env, root, snap)
        env.run("-c", env.config, "-i")

        out = env.dir / "restored"
        today = time.strftime("%Y-%m-%d")
        env.run("-c", env.config, "--restore", "-i", "--to", out, "--at", today)
        assert (out / "a.txt").exists()

        env.run("-c", env.config, "--restore", "-i", "--to", env.dir / "r2",
                "--at", "1999-01", expect=1)
    finally:
        env.cleanup()


def test_runtime_verbose_from_config():
    env = Env()
    try:
        env.write_config(f"""
project: quiet
log: {{level: info, file: {env.dir / "kwbackup.log"}}}
runtime: {{temp_dir: {env.tmp}, verbose: true}}
rclone: {{binary: {env.bin / "rclone"}}}
upstreams: {{fake: {{}}}}
database:
  enabled: true
  targets: ["fake:/DB"]
  databases: [db1]
  retention: {{daily: {{keep: 2}}}}
""")
        res = env.run("-c", env.config, "-d")
        assert "dumping" in res.stderr, res.stderr
        assert "run:" not in res.stderr, res.stderr
        assert "--progress" in (env.dir / "rclone.log").read_text()
    finally:
        env.cleanup()


def test_runtime_debug_from_config_implies_verbose():
    env = Env()
    try:
        env.write_config(f"""
project: dbg
log: {{level: info, file: {env.dir / "kwbackup.log"}}}
runtime: {{temp_dir: {env.tmp}, verbose: false, debug: true}}
rclone: {{binary: {env.bin / "rclone"}}}
upstreams: {{fake: {{}}}}
database:
  enabled: true
  targets: ["fake:/DB"]
  databases: [db1]
  retention: {{daily: {{keep: 2}}}}
""")
        res = env.run("-c", env.config, "-d")
        assert "dumping" in res.stderr, res.stderr
        assert "run:" in res.stderr, res.stderr
        assert "--progress" in (env.dir / "rclone.log").read_text()
    finally:
        env.cleanup()


def test_console_and_file_levels_are_separate():
    env = Env()
    try:
        logfile = env.dir / "kwbackup.log"
        task = env.dir / "task.yaml"

        def write(level, runtime):
            task.write_text(f"""
project: levels
log: {{level: {level}, file: {logfile}}}
runtime: {{temp_dir: {env.tmp}{runtime}}}
rclone: {{binary: {env.bin / "rclone"}}}
upstreams: {{fake: {{}}}}
database:
  enabled: true
  targets: ["fake:/DB"]
  databases: [db1]
  retention: {{daily: {{keep: 2}}}}
""", encoding="utf-8")

        # console debug, file warning: commands on screen, not in the file
        write("warning", ", debug: true")
        res = env.run("-c", task, "-d")
        assert "run:" in res.stderr, res.stderr
        assert "run:" not in logfile.read_text(encoding="utf-8")

        # file debug, console quiet: commands in the file, not on screen
        logfile.unlink()
        write("debug", "")
        res = env.run("-c", task, "-d")
        assert "run:" not in res.stderr, res.stderr
        assert "run:" in logfile.read_text(encoding="utf-8")
    finally:
        env.cleanup()


class _HookHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.server.requests.append(json.loads(self.rfile.read(length)))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


class _HookServer:
    """Local webhook receiver for the notification tests."""

    def __enter__(self):
        self.server = http.server.HTTPServer(("127.0.0.1", 0), _HookHandler)
        self.server.requests = []
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/hook"
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    @property
    def requests(self):
        return self.server.requests


def test_vault_rsync_transport():
    env = Env()
    try:
        env._tool("rsync", FAKE_RSYNC)
        env._tool("ssh", FAKE_SSH)
        root = env.dir / "vroot"
        root.mkdir()
        (root / "a.txt").write_text("a", encoding="utf-8")
        rsync_root = env.dir / "rsync"
        config = env.write_config(f"""
project: vrs
log: {{level: info}}
runtime: {{temp_dir: {env.tmp}}}
rclone: {{binary: {env.bin / "rclone"}}}
upstreams:
  nas:
    type: rsync
    host: backup@nas
    ssh_options: ["-p", "2222"]
vault:
  enabled: true
  root: {root}
  state_dir: {env.dir / "vstate"}
  volume_size: 1M
  targets: ["nas:/srv/backup/vault"]
  journal: {{type: scan}}
""")
        extra = {"FAKE_RSYNC_ROOT": str(rsync_root)}
        env.run("-c", config, "--vault", "--full", extra_env=extra)
        remote = rsync_root / "srv" / "backup" / "vault"
        assert list(remote.glob("*.manifest.json")), list(remote.iterdir())
        (root / "b.txt").write_text("b", encoding="utf-8")
        env.run("-c", config, "--vault", extra_env=extra)
        manifests = sorted(remote.glob("*.manifest.json"))
        assert len(manifests) == 2, manifests
        last = json.loads(manifests[-1].read_text(encoding="utf-8"))
        assert last["added"] == ["b.txt"], last
        shutil.rmtree(env.dir / "vstate")
        (root / "c.txt").write_text("c", encoding="utf-8")
        env.run("-c", config, "--vault", extra_env=extra)
        last = json.loads(sorted(remote.glob("*.manifest.json"))[-1].read_text())
        assert last["added"] == ["c.txt"], last
    finally:
        env.cleanup()


def test_rsync_upstream_is_vault_only():
    env = Env()
    try:
        env.write_config(f"""
project: bad
runtime: {{temp_dir: {env.tmp}}}
rclone: {{binary: {env.bin / "rclone"}}}
upstreams:
  nas: {{type: rsync, host: backup@nas}}
database:
  enabled: true
  targets: ["nas:/DB"]
  databases: [db1]
  retention: {{daily: {{keep: 1}}}}
""")
        res = env.run("-c", env.config, "--check", expect=2)
        assert "needs rclone" in res.stderr, res.stderr
    finally:
        env.cleanup()


def _vault_setup(env, journal="scan"):
    root = env.dir / "vroot"
    root.mkdir(exist_ok=True)
    (root / "a.txt").write_text("a", encoding="utf-8")
    jdir = env.dir / "journal"
    jdir.mkdir(exist_ok=True)
    extra = f"""
vault:
  enabled: true
  root: {root}
  state_dir: {env.dir / "vstate"}
  volume_size: 1M
  targets: ["fake:/V"]
"""
    if journal == "txt":
        extra += f"  journal: {{type: txt, period: daily, path: {jdir}}}\n"
    else:
        extra += "  journal: {type: scan}\n"
    env.base_config(extra=extra)
    return root, jdir


def _vault_layers(env):
    return [json.loads(path.read_text(encoding="utf-8"))
            for path in sorted((env.cloud / "V").glob("*.manifest.json"))]


def test_vault_full_and_empty_layer():
    env = Env()
    try:
        _vault_setup(env)
        env.run("-c", env.config, "--vault", "--full")
        layers = _vault_layers(env)
        assert len(layers) == 1, layers
        assert layers[0]["base"] is True, layers[0]
        assert [entry["path"] for entry in layers[0]["files"]] == ["a.txt"], layers[0]
        env.run("-c", env.config, "--vault")
        layers = _vault_layers(env)
        assert len(layers) == 2, layers
        assert layers[-1]["base"] is False and layers[-1]["files"] == [], layers[-1]
        names = {p.name for p in (env.cloud / "V").iterdir()}
        assert len({name.split(".")[0] for name in names}) == 2, names
    finally:
        env.cleanup()


def test_vault_journal_add_and_delete():
    env = Env()
    try:
        root, jdir = _vault_setup(env, "txt")
        env.run("-c", env.config, "--vault", "--full")
        yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
        (root / "b.txt").write_text("b", encoding="utf-8")
        (root / "a.txt").unlink()
        journal = jdir / f"{yesterday}.journal.txt"
        journal.write_text(
            f"{yesterday}T01:00:00Z\tADDED\tb.txt\n"
            f"{yesterday}T01:00:01Z\tDELETED\ta.txt\n", encoding="utf-8")
        env.run("-c", env.config, "--vault")
        layers = _vault_layers(env)
        assert layers[-1]["added"] == ["b.txt"], layers[-1]
        assert layers[-1]["deleted"] == ["a.txt"], layers[-1]
        assert (jdir / f"{yesterday}.journal.txt.done").exists()
        env.run("-c", env.config, "--vault")      # no journal -> empty day
        layers = _vault_layers(env)
        assert layers[-1]["files"] == [] and layers[-1]["deleted"] == [], layers[-1]
    finally:
        env.cleanup()


def test_vault_reconcile_without_journal():
    env = Env()
    try:
        root, _ = _vault_setup(env)
        env.run("-c", env.config, "--vault", "--full")
        (root / "c.txt").write_text("c", encoding="utf-8")
        env.run("-c", env.config, "--vault", "--reconcile")
        layers = _vault_layers(env)
        assert layers[-1]["added"] == ["c.txt"], layers[-1]
    finally:
        env.cleanup()


def test_vault_rebuilds_index_from_manifests():
    env = Env()
    try:
        root, _ = _vault_setup(env)
        env.run("-c", env.config, "--vault", "--full")
        (root / "c.txt").write_text("c", encoding="utf-8")
        env.run("-c", env.config, "--vault")
        shutil.rmtree(env.dir / "vstate")
        (root / "d.txt").write_text("d", encoding="utf-8")
        env.run("-c", env.config, "--vault")
        layers = _vault_layers(env)
        assert layers[-1]["added"] == ["d.txt"], layers[-1]
    finally:
        env.cleanup()


def _hook_config(env, hooks, enabled=True, timeout=10):
    src = env.dir / "src"
    src.mkdir(exist_ok=True)
    (src / "a.txt").write_text("a", encoding="utf-8")
    return env.write_config(f"""
project: hooktest
log: {{level: info}}
runtime: {{temp_dir: {env.tmp}, hook_timeout: {timeout}}}
rclone: {{binary: {env.bin / "rclone"}}}
upstreams: {{fake: {{}}}}
archive:
  enabled: {str(enabled).lower()}
  name: site
  root: {src}
  targets: ["fake:/A"]
  retention: {{keep: 1}}
{hooks}
""")


def test_hooks_run_in_order_with_env():
    env = Env()
    try:
        order = env.dir / "order.txt"
        envfile = env.dir / "env.txt"
        _hook_config(env, f"""  before:
    - "echo before1 >> {order}"
    - "echo before2 >> {order}"
    - "env | grep ^KWBACKUP_ | sort >> {envfile}"
  after:
    - "echo after:$KWBACKUP_STATUS >> {order}"
""")
        env.run("-c", env.config, "-a")
        assert order.read_text().splitlines() == ["before1", "before2", "after:ok"], \
            order.read_text()
        values = envfile.read_text().splitlines()
        for expected in ("KWBACKUP_ACTION=archive", "KWBACKUP_PHASE=before",
                         "KWBACKUP_STATUS=starting", "KWBACKUP_PROJECT=hooktest",
                         f"KWBACKUP_TASK_FILE={env.config}"):
            assert expected in values, values
    finally:
        env.cleanup()


def test_before_hook_failure_aborts_task():
    env = Env()
    try:
        after = env.dir / "after.txt"
        _hook_config(env, f"""  before:
    - "exit 7"
  after:
    - "echo after:$KWBACKUP_STATUS >> {after}"
""")
        res = env.run("-c", env.config, "-a", expect=1)
        assert "before hook failed" in res.stderr, res.stderr
        assert after.read_text().strip() == "after:failed", after.read_text()
        assert env.cloud_tree() == [], env.cloud_tree()
    finally:
        env.cleanup()


def test_after_hook_failure_is_a_warning():
    env = Env()
    try:
        _hook_config(env, """  after:
    - "exit 3"
""")
        res = env.run("-c", env.config, "-a")
        assert "after hook failed" in res.stderr, res.stderr
        assert env.cloud_tree(), "the backup itself must succeed"
    finally:
        env.cleanup()


def test_hook_timeout():
    env = Env()
    try:
        _hook_config(env, """  before:
    - "sleep 5"
""", timeout=1)
        started = time.monotonic()
        res = env.run("-c", env.config, "-a", expect=1)
        assert "timed out" in res.stderr, res.stderr
        assert time.monotonic() - started < 5
    finally:
        env.cleanup()


def test_hooks_skipped_on_dry_run():
    env = Env()
    try:
        marker = env.dir / "ran.txt"
        _hook_config(env, f"""  before:
    - "echo ran >> {marker}"
""")
        env.run("-c", env.config, "-a", "--dry-run")
        assert not marker.exists(), "dry-run must not execute hooks"
    finally:
        env.cleanup()


def test_hooks_not_run_for_disabled_task():
    env = Env()
    try:
        marker = env.dir / "ran.txt"
        _hook_config(env, f"""  before:
    - "echo ran >> {marker}"
""", enabled=False)
        res = env.run("-c", env.config, "-a")
        assert "disabled" in res.stderr, res.stderr
        assert not marker.exists(), "a disabled task must not run hooks"
    finally:
        env.cleanup()


def _notify_config(env, hook_url, on="[failure]", broken=False, root=None):
    upstream = (f"  broken: {{config: {env.dir / 'broken.conf'}}}"
                if broken else "  fake: {}")
    target = "broken:/A" if broken else "fake:/A"
    return env.write_config(f"""
project: notifytest
log: {{level: info}}
runtime: {{temp_dir: {env.tmp}}}
rclone:
  binary: {env.bin / "rclone"}
  s3-custom-options:
    s3-secret-access-key: SUPERSECRET
upstreams:
{upstream}
archive:
  enabled: true
  name: site
  root: {root}
  targets: ["{target}"]
  retention: {{keep: 1}}
notify:
  on: {on}
  webhook: {{url: "{hook_url}"}}
""")


def _notify_source(env):
    src = env.dir / "src"
    src.mkdir(exist_ok=True)
    (src / "a.txt").write_text("a", encoding="utf-8")
    (env.dir / "broken.conf").write_text("[broken]\n", encoding="utf-8")
    return src


def test_notify_webhook_on_failure():
    env = Env()
    try:
        src = _notify_source(env)
        with _HookServer() as hook:
            _notify_config(env, hook.url, broken=True, root=src)
            env.run("-c", env.config, "-a", expect=1)
            assert len(hook.requests) == 1, hook.requests
            body = hook.requests[0]
            assert body["source"] == "kwbackup", body
            assert "backup failed (exit 1)" in body["subject"], body["subject"]
            assert str(env.config) in body["text"], body["text"]
            assert "archive → broken: rclone exited with 1" in body["text"], body["text"]
            assert "FAILED, exit 1" in body["text"], body["text"]
            # commands and keys must never reach the channel
            assert "SUPERSECRET" not in body["text"], body["text"]
            assert "s3-secret-access-key" not in body["text"], body["text"]
    finally:
        env.cleanup()


def test_notify_success_only():
    env = Env()
    try:
        src = _notify_source(env)
        with _HookServer() as hook:
            _notify_config(env, hook.url, on="[success]", root=src)
            env.run("-c", env.config, "-a")
            assert len(hook.requests) == 1, hook.requests
            assert "backup ok" in hook.requests[0]["subject"], hook.requests[0]
            _notify_config(env, hook.url, on="[success]", broken=True, root=src)
            env.run("-c", env.config, "-a", expect=1)
            assert len(hook.requests) == 1, "success-only must stay silent on failure"
    finally:
        env.cleanup()


def test_notify_empty_on_is_silent():
    env = Env()
    try:
        src = _notify_source(env)
        with _HookServer() as hook:
            _notify_config(env, hook.url, on="[]", broken=True, root=src)
            res = env.run("-c", env.config, "-a", expect=1)
            assert hook.requests == [], hook.requests
            assert "notify" not in res.stderr, res.stderr
    finally:
        env.cleanup()


def test_notify_unreachable_webhook_keeps_exit_code():
    env = Env()
    try:
        src = _notify_source(env)
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        _notify_config(env, f"http://127.0.0.1:{port}/hook", on="[success]", root=src)
        res = env.run("-c", env.config, "-a")
        assert "notify: webhook" in res.stderr, res.stderr
    finally:
        env.cleanup()


def test_notify_check_does_not_send():
    env = Env()
    try:
        src = _notify_source(env)
        with _HookServer() as hook:
            _notify_config(env, hook.url, root=src)
            env.run("-c", env.config, "--check")
            assert hook.requests == [], hook.requests
    finally:
        env.cleanup()


def _syslog_listener(path):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind(str(path))
    sock.settimeout(1.0)
    return sock


def _syslog_drain(sock):
    data = b""
    while True:
        try:
            data += sock.recv(65535)
        except socket.timeout:
            break
    sock.close()
    return data


def test_disabled_section_is_not_enforced():
    env = Env()
    try:
        env.base_config(extra="""
incremental:
  enabled: false
  root: /tmp
  snapshot: /tmp/state.snar
  targets: ["missing:/INCR"]
  retention: {keep: 2}
""")
        out = env.run("-c", env.config)
        assert "disabled" in out.stdout, out.stdout
        assert "invalid config" in out.stdout, out.stdout
        assert "see --check" in out.stdout, out.stdout
        env.run("-c", env.config, "--list")            # reports still work
        res = env.run("-c", env.config, "--check", expect=2)
        assert "unknown upstream 'missing'" in res.stderr, res.stderr
    finally:
        env.cleanup()


def test_disabled_section_enabled_key_is_still_fatal():
    env = Env()
    try:
        env.base_config(extra="""
storage:
  enabled: "yes"
""")
        res = env.run("-c", env.config, expect=2)
        assert "config.storage.enabled" in res.stderr, res.stderr
    finally:
        env.cleanup()


def test_disabled_section_forced_run_is_strict():
    env = Env()
    try:
        env.base_config(extra="""
incremental:
  enabled: false
  root: /tmp
  snapshot: /tmp/state.snar
  targets: ["missing:/INCR"]
  retention: {keep: 2}
""")
        env.run("-c", env.config, "-i")                # skipped, exit 0
        res = env.run("-c", env.config, "-i", "--force", expect=2)
        assert "unknown upstream 'missing'" in res.stderr, res.stderr
    finally:
        env.cleanup()


def test_syslog_sink_next_to_file():
    env = Env()
    try:
        path = env.dir / "syslog.sock"
        listener = _syslog_listener(path)
        logfile = env.dir / "kwbackup.log"
        env.write_config(f"""
project: sylog
log: {{level: info, file: {logfile}, syslog: "{path}", facility: local0}}
runtime: {{temp_dir: {env.tmp}}}
rclone: {{binary: {env.bin / "rclone"}}}
upstreams: {{fake: {{}}}}
database:
  enabled: true
  targets: ["fake:/DB"]
  databases: [db1]
  retention: {{daily: {{keep: 2}}}}
""")
        env.run("-c", env.config, "-d")
        data = _syslog_drain(listener)
        assert data.startswith(b"<134>"), data          # local0 (16) * 8 + info (6)
        assert b"kwbackup[" in data, data               # tag = SYSLOG_IDENTIFIER
        assert b"dumping" in data, data
        assert "dumping" in logfile.read_text(encoding="utf-8"), "file sink must stay"
    finally:
        env.cleanup()


def test_syslog_respects_log_level():
    env = Env()
    try:
        path = env.dir / "syslog.sock"
        listener = _syslog_listener(path)
        env.write_config(f"""
project: sylog
log: {{level: error, syslog: "{path}"}}
runtime: {{temp_dir: {env.tmp}}}
rclone: {{binary: {env.bin / "rclone"}}}
upstreams: {{fake: {{}}}}
database:
  enabled: true
  targets: ["fake:/DB"]
  databases: [db1]
  retention: {{daily: {{keep: 2}}}}
""")
        env.run("-c", env.config, "-d")
        data = _syslog_drain(listener)
        assert b"INFO" not in data, data
    finally:
        env.cleanup()


def test_syslog_true_uses_system_socket():
    env = Env()
    try:
        env.base_config(extra="")
        task = env.dir / "task.yaml"
        task.write_text(f"""
project: sylog
log: {{level: info, syslog: true}}
runtime: {{temp_dir: {env.tmp}}}
rclone: {{binary: {env.bin / "rclone"}}}
upstreams: {{fake: {{}}}}
""", encoding="utf-8")
        out = env.run("-c", task, "--check")
        assert "syslog daemon (system socket)" in out.stdout, out.stdout
    finally:
        env.cleanup()


def _syslog_task(env, log_line):
    return env.write_config(f"""
project: sylog
log: {log_line}
runtime: {{temp_dir: {env.tmp}}}
rclone: {{binary: {env.bin / "rclone"}}}
upstreams: {{fake: {{}}}}
database:
  enabled: true
  targets: ["fake:/DB"]
  databases: [db1]
  retention: {{daily: {{keep: 2}}}}
""")


def test_syslog_missing_socket_is_a_warning():
    env = Env()
    try:
        _syslog_task(env, f'{{level: info, syslog: "{env.dir / "nope.sock"}"}}')
        res = env.run("-c", env.config, "-d")
        assert "not found" in res.stderr, res.stderr
        assert env.cloud_tree(), "backup must still run"
    finally:
        env.cleanup()


def test_syslog_dead_socket_does_not_break_run():
    """A socket file without a reader refuses datagrams; the run must survive."""
    env = Env()
    try:
        path = env.dir / "dead.sock"
        _syslog_listener(path).close()          # the path stays, nobody listens
        _syslog_task(env, f'{{level: info, syslog: "{path}"}}')
        res = env.run("-c", env.config, "-d")
        assert "syslog" in res.stderr, res.stderr
        assert env.cloud_tree(), "backup must still run"
    finally:
        env.cleanup()


def test_syslog_bad_facility_is_fatal():
    env = Env()
    try:
        env.base_config(extra="")
        task = env.dir / "task.yaml"
        task.write_text(f"""
project: sylog
log: {{level: info, syslog: true, facility: local9}}
runtime: {{temp_dir: {env.tmp}}}
rclone: {{binary: {env.bin / "rclone"}}}
upstreams: {{fake: {{}}}}
""", encoding="utf-8")
        res = env.run("-c", task, "--check", expect=2)
        assert "facility" in res.stderr, res.stderr
    finally:
        env.cleanup()


TESTS = [test_database_retention, test_database_disabled, test_storage_sync, test_storage_dest,
         test_archive_contents_and_retention, test_archive_include_list,
         test_list_and_verify, test_verify_detects_corruption,
         test_restore_database, test_restore_into_db, test_restore_requires_confirmation,
         test_restore_archive_and_storage, test_restore_at_filter,
         test_migrate_old_config, test_migrate_uses_keep_by_default,
         test_documented_examples_validate, test_defaults_file_layer,
         test_builtin_defaults_only, test_build_version_and_shebang,
         test_mode_copy_in_config, test_mode_sync_rejected_in_config,
         test_archive_sync_mirrors, test_database_sync_mirrors,
         test_defaults_file_covers_builtins, test_no_action_does_nothing,
         test_verbose_reaches_rclone, test_quiet_by_default,
         test_no_args_shows_banner, test_archive_name_defaults_to_project,
         test_incremental_full_then_increment,
         test_incremental_changed_file_lands_in_increment,
         test_incremental_retention_drops_whole_chain,
         test_incremental_missing_snapshot_forces_full,
         test_incremental_restore_chain,
         test_incremental_restore_applies_deletion,
         test_incremental_list_and_verify,
         test_incremental_at,
         test_runtime_verbose_from_config,
         test_runtime_debug_from_config_implies_verbose,
         test_console_and_file_levels_are_separate,
         test_disabled_section_is_not_enforced,
         test_disabled_section_enabled_key_is_still_fatal,
         test_disabled_section_forced_run_is_strict,
         test_vault_rsync_transport,
         test_rsync_upstream_is_vault_only,
         test_vault_full_and_empty_layer,
         test_vault_journal_add_and_delete,
         test_vault_reconcile_without_journal,
         test_vault_rebuilds_index_from_manifests,
         test_check_fails_on_preflight_problems,
         test_check_preflight_failure_notifies,
         test_storage_sync_refuses_empty_source,
         test_storage_sync_allows_empty_source_when_flagged,
         test_hooks_run_in_order_with_env,
         test_before_hook_failure_aborts_task,
         test_after_hook_failure_is_a_warning,
         test_hook_timeout,
         test_hooks_skipped_on_dry_run,
         test_hooks_not_run_for_disabled_task,
         test_notify_webhook_on_failure,
         test_notify_success_only,
         test_notify_empty_on_is_silent,
         test_notify_unreachable_webhook_keeps_exit_code,
         test_notify_check_does_not_send,
         test_syslog_sink_next_to_file,
         test_syslog_respects_log_level,
         test_syslog_true_uses_system_socket,
         test_syslog_missing_socket_is_a_warning,
         test_syslog_dead_socket_does_not_break_run,
         test_syslog_bad_facility_is_fatal,
         test_multiple_upstreams_upload, test_upstream_failure_keeps_going,
         test_restore_from_upstream, test_unknown_upstream_rejected,
         test_shared_target_keeps_foreign_archives,
         test_shared_target_keeps_foreign_databases,
         test_unknown_key_is_a_warning, test_s3_custom_options]


def main():
    if not DIST.exists():
        sys.exit("tests: dist/kwbackup not built, run python3 build.py")
    failed = 0
    for test in TESTS:
        try:
            test()
            print(f"ok   {test.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {test.__name__}")
            traceback.print_exc()
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
