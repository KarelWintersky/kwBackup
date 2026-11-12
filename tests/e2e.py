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
if os.environ.get("FAKE_RCLONE_LOG"):
    with open(os.environ["FAKE_RCLONE_LOG"], "a") as fh:
        fh.write(" ".join(argv) + "\n")
while "--config" in argv:
    i = argv.index("--config"); del argv[i:i + 2]
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
        env["FAKE_RCLONE_LOG"] = str(self.dir / "rclone.log")
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
        files = [p for p in sorted((ROOT / "examples").glob("*.yaml"))
                 if p.name != "defaults.yaml"]   # defaults are not a task
        assert files, "no example configs found"
        for path in files:
            env.run("-c", path, "--check-config",
                    "--defaults", ROOT / "examples" / "defaults.yaml",
                    extra_env=secret_env)
    finally:
        env.cleanup()


def test_defaults_file_layer():
    env = Env()
    try:
        defaults = env.dir / "defaults.yaml"
        defaults.write_text(f"""
version: 1
log: {{level: warning}}
rclone:
  binary: {env.bin / "rclone"}
  remote: fake
  args: ["--s3-no-check-bucket"]
database:
  type: mysql
  compressor: zstd
  compressor_args: ["-1"]
  retention:
    daily: {{keep: 1}}
""", encoding="utf-8")
        task = env.dir / "task.yaml"
        task.write_text("""
version: 1
project: layered
rclone:
  args:
    - "--s3-access-key-id=KEY1"
    - "--s3-secret-access-key=SECRET1"
database:
  enabled: true
  target: "fake:/DB"
  databases: [db1]
""", encoding="utf-8")
        base = ["-c", task, "--defaults", defaults]

        out = env.run(*base, "--check-config")
        assert "defaults.yaml" in out.stdout, out.stdout
        assert "keep 1" in out.stdout, out.stdout

        env.run(*base, "-d")
        log = (env.dir / "rclone.log").read_text()
        assert "--s3-no-check-bucket" in log, log           # from defaults
        assert "--s3-access-key-id=KEY1" in log, log        # from the task, appended
        assert "--s3-secret-access-key=SECRET1" in log, log

        env.run("-c", task, "--defaults", env.dir / "missing.yaml",
                "--check-config", expect=2)
    finally:
        env.cleanup()


def test_builtin_defaults_only():
    """Without /etc/kwbackup/defaults.yaml the built-in defaults apply."""
    env = Env()
    try:
        env.base_config(extra="""
database:
  enabled: true
  target: "fake:/DB"
  databases: [db1]
  retention:
    daily: {keep: 2}
""")
        out = env.run("-c", env.config, "--check-config")
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
        assert ver.stdout.strip() == "kwbackup 9.9.9", ver.stdout
    finally:
        env.cleanup()


def _artifact():
    """Import the built single-file tool as a module (main() is guarded)."""
    import importlib.util
    from importlib.machinery import SourceFileLoader
    loader = SourceFileLoader("kwbackup_built", str(DIST))
    spec = importlib.util.spec_from_loader("kwbackup_built", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


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
    raw = kw.yaml_loads((ROOT / "examples" / "defaults.yaml").read_text(encoding="utf-8"))
    missing = _key_paths(kw.BUILTIN_DEFAULTS) - _key_paths(raw)
    assert not missing, f"examples/defaults.yaml misses built-in keys: {sorted(missing)}"
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
  target: "fake:/DB"
  databases: [db1]
  retention: {daily: {keep: 2}}
archive:
  enabled: true
  mode: copy
  name: site
  root: /tmp
  target: "fake:/ARCHIVE"
""")
        out = env.run("-c", env.config, "--check-config")
        assert "mode copy" in out.stdout, out.stdout
    finally:
        env.cleanup()


def test_mode_sync_rejected_in_config():
    env = Env()
    try:
        base = """
version: 1
project: mirror
rclone: {binary: %s, remote: fake}
""" % (env.bin / "rclone")
        task = env.dir / "task.yaml"
        task.write_text(base + """
database:
  enabled: true
  mode: sync
  target: "fake:/DB"
  databases: [db1]
  retention: {daily: {keep: 2}}
""", encoding="utf-8")
        res = env.run("-c", task, "--check-config", expect=2)
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
  target: "fake:/ARCHIVE"
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
  target: "fake:/DB"
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
  target: "fake:/S"
  sources: [{src}]
""")
        out = env.run("-c", env.config)
        assert "No one backup action(s) requested. Allowed:" in out.stdout, out.stdout
        assert "(use --storage  option)  enabled" in out.stdout, out.stdout
        assert "(use --database option)  not configured" in out.stdout, out.stdout
        assert "(use --archive  option)  not configured" in out.stdout, out.stdout
        assert env.cloud_tree() == [], "nothing must be uploaded"

        # a section that exists but is switched off is "disabled", not "not configured"
        env.config.write_text(env.config.read_text(encoding="utf-8") + """
database:
  enabled: false
  target: "fake:/DB"
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
  target: "fake:/DB"
  databases: [db1]
  retention: {daily: {keep: 2}}
""")
        rlog = env.dir / "rclone.log"

        res = env.run("-c", env.config, "-d")
        assert "--progress" not in rlog.read_text(), "quiet by default"
        assert "run:" not in res.stderr, "command lines are debug-only"

        rlog.unlink()
        res = env.run("-c", env.config, "-d", "--verbose")
        assert "--progress" in rlog.read_text(), "--verbose must reach rclone"
        assert "run:" not in res.stderr, "--verbose must not turn on debug logging"

        rlog.unlink()
        res = env.run("-c", env.config, "-d", "--debug")
        assert "--progress" in rlog.read_text(), "--debug implies verbose for rclone"
        assert "run:" in res.stderr, "--debug prints every command"
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
         test_verbose_reaches_rclone]


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
