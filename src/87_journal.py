# ============================================================================
# JOURNAL  (daily TXT change log for the vault task)
#
# The application writes one file per day:
#
#   2026-11-17T03:14:22Z<TAB>ADDED<TAB>photos/2026/11/IMG_001.jpg
#   2026-11-17T03:14:23Z<TAB>DELETED<TAB>photos/2026/10/old.jpg
#
# The vault task reads yesterday's file (the current day is still being
# written) and renames it to <name>.done after the layer is uploaded.
# The journal is only a hint: every path is still stat-compared with the
# current chain by the caller, so duplicate or replayed events are harmless.
# ============================================================================

JOURNAL_SUFFIX = ".journal.txt"
JOURNAL_DONE_SUFFIX = ".journal.txt.done"
JOURNAL_NAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.journal\.txt$")
JOURNAL_DONE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.journal\.txt\.done$")
JOURNAL_OPS = ("ADDED", "MODIFIED", "DELETED")
JOURNAL_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")


@dataclass
class JournalEvent:
    day: str            # YYYY-MM-DD, from the file name
    ts: str             # ISO8601 UTC, from the record
    op: str             # ADDED | MODIFIED | DELETED
    path: str           # relative to vault.root


def journal_day(path):
    match = JOURNAL_NAME_RE.match(Path(path).name)
    return match.group(1) if match else None


def pending_journals(directory, today=None):
    """[(day, Path)] of *.journal.txt with day < today, sorted by day."""
    directory = Path(directory)
    if not directory.is_dir():
        return []
    today = (today or dt.date.today()).isoformat()
    out = []
    for entry in directory.iterdir():
        if not entry.is_file():
            continue
        day = journal_day(entry)
        if day and day < today:
            out.append((day, entry))
    return sorted(out, key=lambda item: (item[0], item[1].name))


def _journal_path_ok(name):
    if not name or name.startswith("/") or "\x00" in name:
        return False
    parts = name.split("/")
    return ".." not in parts


def read_journal(path, warnings=None):
    """Parse one journal file. Only complete lines; bad records are skipped.

    A trailing line without a newline is treated as incomplete and ignored.
    """
    path = Path(path)

    def note(message):
        if warnings is None:
            LOG.warning("%s", message)
        else:
            warnings.append(message)

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        note(f"{path.name}: {exc.strerror or exc}")
        return []

    lines = text.split("\n")
    if text and not text.endswith("\n"):
        lines = lines[:-1]
    day = journal_day(path) or ""
    events = []
    for number, line in enumerate(lines, 1):
        line = line.rstrip("\r")
        if not line or line.lstrip().startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) != 3:
            note(f"{path.name}:{number}: expected 3 tab-separated fields")
            continue
        ts, op, name = fields
        if not JOURNAL_TS_RE.match(ts):
            note(f"{path.name}:{number}: bad timestamp {ts!r}")
            continue
        op = op.strip().upper()
        if op not in JOURNAL_OPS:
            note(f"{path.name}:{number}: unknown operation {op!r}")
            continue
        if not _journal_path_ok(name):
            note(f"{path.name}:{number}: bad path {name!r}")
            continue
        events.append(JournalEvent(day=day, ts=ts, op=op, path=name))
    return events


def read_pending(directory, today=None, warnings=None):
    """(events, files): all events of pending journals and the files they came from."""
    files, events = [], []
    for _day, path in pending_journals(directory, today):
        events.extend(read_journal(path, warnings))
        files.append(path)
    return events, files


def mark_done(path):
    """Rename a processed journal to <name>.done (atomic, same directory)."""
    path = Path(path)
    done = path.with_name(path.name + ".done")
    os.replace(path, done)
    return done


def done_journals(directory):
    """[(day, Path)] of *.journal.txt.done, sorted by day."""
    directory = Path(directory)
    if not directory.is_dir():
        return []
    out = []
    for entry in directory.iterdir():
        match = JOURNAL_DONE_RE.match(entry.name)
        if match and entry.is_file():
            out.append((match.group(1), entry))
    return sorted(out, key=lambda item: (item[0], item[1].name))


def cleanup_done(directory, keep=None, today=None):
    """Delete .done files older than `keep` (timedelta); None keeps them all."""
    if keep is None:
        return []
    today = today or dt.date.today()
    cutoff = (today - keep).isoformat()
    removed = []
    for day, path in done_journals(directory):
        if day < cutoff:
            try:
                path.unlink()
                removed.append(path)
            except OSError as exc:
                LOG.warning("journal: cannot remove %s: %s", path, exc.strerror or exc)
    return removed
