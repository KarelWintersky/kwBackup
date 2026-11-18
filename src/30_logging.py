# ============================================================================
# LOGGING
# ============================================================================

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _colorize(text, code):
    if os.environ.get("NO_COLOR") is not None or not sys.stderr.isatty():
        return text
    return f"\033[{code}m{text}\033[0m"


def hl(value):
    """Yellow numbers/sizes in terminal log lines; plain in files."""
    return _colorize(str(value), "33")


def good(value):
    """Green success markers (green is reserved for success, red for errors)."""
    return _colorize(str(value), "32")


class _PlainFmt(logging.Formatter):
    """File and syslog formatter: never keep ANSI escapes in a log line."""

    def format(self, record):
        return ANSI_RE.sub("", super().format(record))


class _Fmt(logging.Formatter):
    COLORS = {logging.DEBUG: "\033[90m", logging.INFO: "",
              logging.WARNING: "\033[33m", logging.ERROR: "\033[31m"}

    def format(self, record):
        msg = super().format(record)
        color = self.COLORS.get(record.levelno, "")
        return f"{color}{msg}\033[0m" if color and sys.stderr.isatty() else msg


SYSLOG_SOCKETS = ("/dev/log", "/var/run/syslog")   # the second one is macOS/BSD


def syslog_address(spec):
    """Resolve log.syslog: True -> system socket, a string -> that path.

    Returns (address for SysLogHandler, human readable destination).
    """
    if spec is not True:
        return spec, str(spec)
    for path in SYSLOG_SOCKETS:
        if Path(path).exists():
            return path, path
    return ("localhost", 514), "localhost:514 (udp)"


class _SysLog(logging.handlers.SysLogHandler):
    """SysLogHandler that goes silent instead of breaking the run.

    Python 3.12+ connects lazily, so a missing or closed socket only shows up
    on the first record; a traceback in the middle of a backup is not useful.
    """

    def __init__(self, address, facility, shown):
        super().__init__(address=address, facility=facility)
        self.shown = shown
        self.failed = False

    def emit(self, record):
        if not self.failed:
            super().emit(record)

    def handleError(self, record):
        """SysLogHandler.emit reports failures here; print one warning instead."""
        if self.failed:
            return
        self.failed = True
        exc = sys.exc_info()[1]
        LOG.warning("syslog %s: %s; syslog is off for this run", self.shown, exc)


def _add_syslog(cfg, level):
    address, shown = syslog_address(cfg.log_syslog)
    # A filesystem socket must already exist; it never gets created by us.
    if isinstance(address, str) and not Path(address).exists():
        LOG.warning("syslog socket %s not found; logging to syslog is off", shown)
        return
    try:
        handler = _SysLog(
            address=address,
            facility=logging.handlers.SysLogHandler.facility_names[cfg.log_facility],
            shown=shown)
    except (OSError, ValueError, TypeError) as exc:
        LOG.warning("syslog %s unavailable: %s", shown, exc)
        return
    # syslog adds the timestamp; the tag keeps SYSLOG_IDENTIFIER = kwbackup
    # so that `journalctl -t kwbackup` and rsyslog rules work.
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(
        f"{PROG}[%(process)d]: %(levelname)s %(message)s"))
    LOG.addHandler(handler)


def setup_logging(cfg, verbose=False, debug=False, dry_run=False):
    """log.level/log.file/log.syslog control the sinks, verbose/debug the console.

    quiet (default)  terminal: warnings and errors, sinks: log.level
    verbose          terminal: info (plus rclone --progress), sinks: log.level
    debug            terminal: debug (every command; implies verbose)
    dry-run          terminal: info, because the plan itself is the output
    """
    base = getattr(logging, cfg.log_level.upper())
    file_level = base
    if debug:
        console_level = logging.DEBUG
    elif verbose or dry_run:
        console_level = min(base, logging.INFO)
    else:
        console_level = max(base, logging.WARNING)

    LOG.handlers.clear()
    LOG.propagate = False
    LOG.setLevel(logging.DEBUG)
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(console_level)
    console.setFormatter(_Fmt("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
    LOG.addHandler(console)
    if cfg.log_file:
        try:
            Path(cfg.log_file).parent.mkdir(parents=True, exist_ok=True)
            fh = logging.FileHandler(cfg.log_file, encoding="utf-8")
            fh.setLevel(file_level)
            fh.setFormatter(_PlainFmt("%(asctime)s %(levelname)-7s %(message)s"))
            LOG.addHandler(fh)
        except OSError as exc:
            LOG.warning("log file %s: %s", cfg.log_file, exc)
    if cfg.log_syslog:
        _add_syslog(cfg, file_level)


def fmt_bytes(size):
    step = 1024.0
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < step:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= step
    return f"{size:.1f} PiB"


