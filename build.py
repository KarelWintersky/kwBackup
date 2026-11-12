#!/usr/bin/env python3
"""Concatenate src/*.py into one executable file: dist/kwbackup.

    python3 build.py                    # version from the current git commit
    python3 build.py --version 0.0.14   # explicit version
    python3 build.py --test             # build + py_compile + --selftest

Source files are ordered by their numeric prefix. All imports live in
src/00_header.py: a module must not import anything itself, otherwise the
build fails. This is the only rule that makes plain concatenation safe.

The version is the first line of the current commit message (the project
numbers every commit, e.g. "0.0.14"). It replaces VERSION = "LOCALDEV" in
src/00_header.py; if the placeholder disappears, the build stops.
"""

import os
import py_compile
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
OUT = ROOT / "dist" / "kwbackup"

# -B: the tool must never leave __pycache__ next to itself.
SHEBANG = "#!/usr/bin/python3 -B\n"
BANNER = "# {sep}\n# {name}\n# {sep}\n"

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
VERSION_PLACEHOLDER = 'VERSION = "LOCALDEV"'

IMPORT_RE = re.compile(r"^(import |from )")


def detect_version(explicit=None):
    """--version, then KWBACKUP_VERSION, then the current commit subject."""
    if explicit:
        return explicit
    env = os.environ.get("KWBACKUP_VERSION")
    if env:
        return env
    try:
        subject = subprocess.check_output(
            ["git", "-C", str(ROOT), "log", "-1", "--format=%s"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    return subject if VERSION_RE.match(subject) else None


def substitute_version(text, version, where):
    if text.count(VERSION_PLACEHOLDER) != 1:
        sys.exit(f"build: {where}: expected exactly one {VERSION_PLACEHOLDER!r}")
    return text.replace(VERSION_PLACEHOLDER, f'VERSION = "{version}"')


def build(version=None, out_path=None):
    files = sorted(SRC.glob("*.py"))
    if not files:
        sys.exit("build: no sources in src/")
    out_path = Path(out_path) if out_path else OUT
    chunks = [SHEBANG, ""]
    for path in files:
        text = path.read_text(encoding="utf-8")
        if path.name == "00_header.py":
            if version:
                text = substitute_version(text, version, path.name)
            chunks.append(BANNER.format(sep="=" * 76, name=f"src/{path.name}"))
            chunks.append(text.rstrip() + "\n")
            continue
        for no, line in enumerate(text.splitlines(), 1):
            if IMPORT_RE.match(line):
                sys.exit(f"build: {path.name}:{no}: imports must live in "
                         f"src/00_header.py, found: {line}")
        chunks.append(BANNER.format(sep="=" * 76, name=f"src/{path.name}"))
        chunks.append(text.rstrip() + "\n")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(chunks), encoding="utf-8")
    out_path.chmod(0o755)
    return out_path


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    version = explicit = out = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-v", "--version"):
            explicit = argv[i + 1]
            i += 2
        elif arg in ("-o", "--output"):
            out = argv[i + 1]
            i += 2
        elif arg == "--test":
            i += 1
        else:
            sys.exit(f"build: unknown argument {arg!r}")

    version = detect_version(explicit)
    if version and not VERSION_RE.match(version):
        sys.exit(f"build: bad version {version!r}, expected X.Y.Z")
    out_path = build(version, out)
    shown = version or "LOCALDEV"
    print(f"build: {out_path} ({out_path.stat().st_size} bytes, "
          f"{len(out_path.read_text(encoding='utf-8').splitlines())} lines, "
          f"version {shown})")
    if not version:
        print("build: warning: no version found (not a git repo or the commit "
              'subject is not X.Y.Z); VERSION stays "LOCALDEV"')

    if "--test" in argv:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            py_compile.compile(str(out_path),
                               cfile=str(Path(tmp) / "kwbackup.pyc"), doraise=True)
        print("build: py_compile ok")
        rc = subprocess.run([sys.executable, str(out_path), "--selftest"]).returncode
        if rc != 0:
            sys.exit(f"build: selftest failed ({rc})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
