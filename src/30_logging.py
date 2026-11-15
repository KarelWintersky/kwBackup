# ============================================================================
# LOGGING
# ============================================================================

class _Fmt(logging.Formatter):
    COLORS = {logging.DEBUG: "\033[90m", logging.INFO: "",
              logging.WARNING: "\033[33m", logging.ERROR: "\033[31m"}

    def format(self, record):
        msg = super().format(record)
        color = self.COLORS.get(record.levelno, "")
        return f"{color}{msg}\033[0m" if color and sys.stderr.isatty() else msg


def setup_logging(cfg, verbose=False, debug=False, dry_run=False):
    """log.level/log.file control the file, verbose/debug control the console.

    quiet (default)  terminal: warnings and errors, file: log.level
    verbose          terminal: info (plus rclone --progress), file: log.level
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
            fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
            LOG.addHandler(fh)
        except OSError as exc:
            LOG.warning("log file %s: %s", cfg.log_file, exc)


def fmt_bytes(size):
    step = 1024.0
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < step:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= step
    return f"{size:.1f} PiB"


