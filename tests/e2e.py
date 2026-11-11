#!/usr/bin/env python3
"""End-to-end tests for dist/kwbackup.

The binary is run as a real process; rclone is replaced by a local stub that
keeps the "cloud" in a temporary directory, the database dumpers are stubs
that print SQL. Real tar/zstd/gzip are used.

    python3 tests/e2e.py [-v]
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist" / "kwbackup"

FAKE_RCLONE = r'''#!/usr/bin/env python3
import os, shutil, sys
from pathlib import Path
CLOUD = Path(os.environ["FAKE_CLOUD"])
argv = sys.argv[1:]
while "--config" in argv:
    i = argv.index("--config"); del argv[i:i + 2]
DROP = {"--s3-no-check-bucket", "--copy-links", "--update", "--verbose", "--progress"}
argv = [a for a in argv if a not in DROP]
cmd, rest = argv[0], argv[1:]

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
version: 1
project: e2e
log: {{level: info}}
runtime: {{temp_dir: {self.tmp}}}
rclone:
  binary: {self.bin / "rclone"}
  remote: fake
"""
        body += extra.get("extra", "")
        return self.write_config(body)

    def run(self, *args, expect=0, extra_env=None):
        env = dict(os.environ)
        env["PATH"] = f"{self.bin}:{env['PATH']}"
        env["FAKE_CLOUD"] = str(self.cloud)
        env["FAKE_MYSQL_OUT"] = str(self.dir / "mysql.out")
        if extra_env:
            env.update(extra_env)
        cmd = [str(DIST), *map(str, args)]
        if VERBOSE:
            print("   $", " ".join(cmd))
        res = subprocess.run(cmd, capture_output=True, text=True, env=env)
        if VERBOSE or res.returncode != expect:
            if res.stdout.strip():
                print(res.stdout)
            if res.stderr.strip():
                print(res.stderr)
        if res.returncode != expect:
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
  target: "fake:/DB"
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
  target: "fake:/DB"
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
  target: "fake:/STORAGE"
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
  target: "fake:/S"
  sources:
    - path: {src}
      dest: custom/dir
""")
        env.run("-c", env.config, "-s")
        assert env.cloud_tree() == ["S/custom/dir/x"], env.cloud_tree()
    finally:
        env.cleanup()


def tar_entries(archive):
    data = subprocess.run(["zstd", "-dc", str(archive)], capture_output=True, check=True).stdout
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
  target: "fake:/ARCHIVE"
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
  target: "fake:/A"
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
  target: "fake:/DB"
  databases: [db1]
  retention:
    daily: {{keep: 3}}
archive:
  enabled: true
  compressor: zstd
  compressor_args: ["-1"]
  name: site
  root: {root}
  target: "fake:/ARCHIVE"
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
  target: "fake:/DB"
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
  target: "fake:/DB"
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
  target: "fake:/DB"
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
  target: "fake:/DB"
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
  target: "fake:/STORAGE"
  sources: [{storage_src}]
archive:
  enabled: true
  compressor: zstd
  name: site
  root: {root}
  target: "fake:/ARCHIVE"
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
  target: "fake:/DB"
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
        env.run("-c", out, "--check-config")
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
    """Every examples/*.yaml must pass --check-config."""
    env = Env()
    try:
        secret_env = {"RCLONE_KEY_ID": "k", "RCLONE_SECRET": "s", "MYSQL_PWD": "p"}
        files = sorted((ROOT / "examples").glob("*.yaml"))
        assert files, "no example configs found"
        for path in files:
            env.run("-c", path, "--check-config", extra_env=secret_env)
    finally:
        env.cleanup()


TESTS = [test_database_retention, test_database_disabled, test_storage_sync, test_storage_dest,
         test_archive_contents_and_retention, test_archive_include_list,
         test_list_and_verify, test_verify_detects_corruption,
         test_restore_database, test_restore_into_db, test_restore_requires_confirmation,
         test_restore_archive_and_storage, test_restore_at_filter,
         test_migrate_old_config, test_migrate_uses_keep_by_default,
         test_documented_examples_validate]


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
