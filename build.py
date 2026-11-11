#!/usr/bin/env python3
"""Concatenate src/*.py into one executable file: dist/kwbackup.

    python3 build.py            # build
    python3 build.py --test     # build + py_compile + --selftest

Source files are ordered by their numeric prefix. All imports live in
src/00_header.py: a module must not import anything itself, otherwise the
build fails. This is the only rule that makes plain concatenation safe.
"""

import py_compile
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
OUT = ROOT / "dist" / "kwbackup"
SHEBANG = "#!/usr/bin/env python3\n"
BANNER = "# {sep}\n# {name}\n# {sep}\n"

IMPORT_RE = re.compile(r"^(import |from )")


def build():
    files = sorted(SRC.glob("*.py"))
    if not files:
        sys.exit("build: no sources in src/")
    chunks = [SHEBANG, ""]
    for path in files:
        text = path.read_text(encoding="utf-8")
        if path.name == "00_header.py":
            chunks.append(BANNER.format(sep="=" * 76, name=f"src/{path.name}"))
            chunks.append(text.rstrip() + "\n")
            continue
        for no, line in enumerate(text.splitlines(), 1):
            if IMPORT_RE.match(line):
                sys.exit(f"build: {path.name}:{no}: imports must live in "
                         f"src/00_header.py, found: {line}")
        chunks.append(BANNER.format(sep="=" * 76, name=f"src/{path.name}"))
        chunks.append(text.rstrip() + "\n")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(chunks), encoding="utf-8")
    OUT.chmod(0o755)
    return OUT


def main():
    out = build()
    print(f"build: {out} ({out.stat().st_size} bytes, "
          f"{len(out.read_text(encoding='utf-8').splitlines())} lines)")
    if "--test" in sys.argv:
        with tempfile.TemporaryDirectory() as tmp:
            py_compile.compile(str(out), cfile=str(Path(tmp) / "kwbackup.pyc"), doraise=True)
        print("build: py_compile ok")
        rc = subprocess.run([sys.executable, str(out), "--selftest"]).returncode
        if rc != 0:
            sys.exit(f"build: selftest failed ({rc})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
