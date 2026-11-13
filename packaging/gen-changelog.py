#!/usr/bin/env python3
"""Generate debian/changelog from the git history.

Every commit subject is a version (0.99.0, 0.99.1, ...), so the changelog is
written one entry per commit, newest first. The commit body becomes the entry
text.

    python3 packaging/gen-changelog.py
    python3 packaging/gen-changelog.py --distribution stable
    python3 packaging/gen-changelog.py --pending-version 0.99.15 \\
        --pending-message "docs: debian packaging"   # for the commit being made

The file is tracked in git: the distribution field decides which part of the
repository the resulting .deb belongs to, so it is a deliberate edit, not a
side effect of a build.

Bootstrap only: the generator assigns the same distribution to every
historical entry (git does not remember it). Once released, maintain
debian/changelog by hand or with dch; do not regenerate it.
"""

import argparse
import datetime as dt
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = "kwbackup"
MAINTAINER = "Karel Wintersky <karel.wintersky@gmail.com>"
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


def git(*args):
    return subprocess.check_output(["git", "-C", str(ROOT), *args],
                                   text=True, stderr=subprocess.DEVNULL)


def history():
    """[(version, text, date)] oldest first."""
    out = []
    for sha in git("rev-list", "--reverse", "HEAD").split():
        version = git("log", "-1", "--format=%s", sha).strip()
        if not VERSION_RE.match(version):
            print(f"gen-changelog: skipping {sha[:7]} ({version!r})", file=sys.stderr)
            continue
        body = git("log", "-1", "--format=%b", sha).rstrip()
        date = git("log", "-1", "--format=%aD", sha).strip()
        out.append((version, body, date))
    return out


def format_entry(version, text, date, distribution):
    lines = [f"{PACKAGE} ({version}) {distribution}; urgency=medium", ""]
    for i, line in enumerate(text.splitlines()):
        if not line.strip():
            lines.append("")
        elif i == 0:
            lines.append(f"  * {line}")
        else:
            lines.append(f"    {line}")
    lines.append("")
    lines.append(f" -- {MAINTAINER}  {date}")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--distribution", default="unstable",
                    help="suite of the generated entries (default: unstable)")
    ap.add_argument("-o", "--output", default=str(ROOT / "debian" / "changelog"))
    ap.add_argument("--pending-version",
                    help="prepend an entry for a commit that does not exist yet")
    ap.add_argument("--pending-message", default="",
                    help="text of the pending entry")
    ap.add_argument("--pending-distribution", default=None,
                    help="suite of the pending entry (default: --distribution); "
                         "used to mark the release entry stable")
    args = ap.parse_args(argv)

    entries = history()
    if not entries:
        sys.exit("gen-changelog: no versioned commits found")

    if args.pending_version:
        if not VERSION_RE.match(args.pending_version):
            sys.exit(f"gen-changelog: bad pending version {args.pending_version!r}")
        stamp = dt.datetime.now().astimezone().strftime("%a, %d %b %Y %H:%M:%S %z")
        entries.append((args.pending_version, args.pending_message, stamp))

    pending_dist = args.pending_distribution or args.distribution
    rendered = []
    for version, text_body, date in reversed(entries):
        dist = pending_dist if version == args.pending_version else args.distribution
        rendered.append(format_entry(version, text_body, date, dist))
    text = "\n\n".join(rendered)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text + "\n", encoding="utf-8")
    print(f"gen-changelog: {out} — {len(entries)} entries, "
          f"top {entries[-1][0]}, distribution {args.distribution}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
