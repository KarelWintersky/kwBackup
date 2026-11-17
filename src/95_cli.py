# ============================================================================
# CLI
# ============================================================================

def build_parser():
    p = argparse.ArgumentParser(
        prog=PROG,
        description="Backup tool over rclone: database dumps, file storage, archives.",
        epilog="Exit codes: 0 ok, 1 error, 2 config error, 3 usage, 4 locked, 5 verify failed.\n"
               "Subcommands: migrate OLD.conf [-o OUT.yaml]  (convert the legacy bash config)",
    )
    p.add_argument("-c", "--config", metavar="FILE", help="config file (YAML)")
    # Hidden: the system defaults always come from /etc/kwbackup/defaults.yaml.
    # The flag exists for tests and for running the examples in place.
    p.add_argument("--defaults", metavar="FILE", help=argparse.SUPPRESS)
    p.add_argument("-d", "--database", action="store_true", help="backup databases")
    p.add_argument("-s", "--storage", action="store_true", help="backup file storage")
    p.add_argument("-a", "--archive", action="store_true", help="backup file archive")
    p.add_argument("-i", "--incremental", action="store_true",
                   help="incremental tar backup (full monthly + daily increments)")
    p.add_argument("--vault", action="store_true",
                   help="versioned file store: TAR layers + manifests")
    p.add_argument("--full", action="store_true",
                   help="vault: start a new base chain (full scan)")
    p.add_argument("--reconcile", action="store_true",
                   help="vault: full scan without a new base (correction layer)")
    p.add_argument("-m", "--mode", choices=("copy", "sync"),
                   help="override the upload algorithm (storage, database, "
                        "archive); sync mirrors the target directory")
    p.add_argument("-f", "--force", action="store_true",
                   help="run actions even if disabled in config")
    p.add_argument("--dry-run", action="store_true", help="show what would be done")
    p.add_argument("--check", action="store_true",
                   help="validate the config and show what will run")
    p.add_argument("--list", action="store_true", help="show what is stored in the cloud")
    p.add_argument("--verify", action="store_true", help="download and check the newest copy")
    p.add_argument("--all", action="store_true", help="with --verify: check every stored copy")
    p.add_argument("--restore", action="store_true", help="download a copy from the cloud")
    p.add_argument("--to", metavar="DIR", help="restore target directory")
    p.add_argument("--at", metavar="TS", help="pick a copy by date prefix, e.g. 2026-11-09")
    p.add_argument("--from", dest="from_upstream", metavar="NAME",
                   help="with --list/--verify/--restore: use only this upstream")
    p.add_argument("--into-db", metavar="NAME", help="with --restore: load the dump into "
                                                    "a live database (needs --yes)")
    p.add_argument("--yes", action="store_true", help="confirm destructive operations")
    p.add_argument("--verbose", action="store_true",
                   help="info messages on the terminal and rclone --progress "
                        "(same as runtime.verbose: true)")
    p.add_argument("--debug", action="store_true",
                   help="debug messages, print every command; implies --verbose "
                        "(same as runtime.debug: true)")
    p.add_argument("--selftest", action="store_true", help="run internal tests and exit")
    p.add_argument("--version", action="store_true", help="print version and exit")
    return p


def main(argv=None):
    global DRY, VERBOSE
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "migrate":
        return migrate_main(argv[1:])
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()
    if args.version:
        print(f"{APP} (Version: {VERSION})")
        return EXIT_OK
    if (args.full or args.reconcile) and not args.vault:
        print("ERROR: --full and --reconcile require --vault", file=sys.stderr)
        return EXIT_USAGE
    if not args.config:
        print_banner(sys.stderr)
        print(file=sys.stderr)
        parser.print_usage(sys.stderr)
        print(file=sys.stderr)
        print("ERROR: -c/--config is required", file=sys.stderr)
        return EXIT_USAGE

    # Disabled sections are parsed but not enforced. They are enforced by
    # --check and by runs that would actually execute them (--force, --restore).
    requested = [name for name in ACTIONS if getattr(args, name)]
    strict = args.check or bool(requested and (args.force or args.restore))
    try:
        cfg = load_config(args.config, defaults=args.defaults, strict=strict)
    except ConfigError as exc:
        print(f"{APP}: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    DRY = args.dry_run
    verbose = args.verbose or cfg.verbose
    debug = args.debug or cfg.debug
    if debug:
        verbose = True                 # debug implies verbose
    VERBOSE = verbose
    setup_logging(cfg, args.verbose or cfg.verbose, args.debug or cfg.debug,
                  dry_run=args.dry_run)
    if not args.check:
        for warning in cfg.warnings:
            LOG.warning("%s", warning)
    LOG.debug("config: %s (project %s)", cfg.path, cfg.project)
    LOG.debug("defaults: %s", cfg.defaults_path or "none (built-in)")
    for message in cfg.deferred_errors:
        LOG.info("config: %s [section disabled: not enforced, see --check]", message)

    if args.check:
        started = dt.datetime.now()
        rc, problems = check_config(cfg, args)
        if problems:
            # The report goes to stdout; keep one line in the log and notify.
            LOG.error("check: %s", "; ".join(problems))
            return finish(cfg, "check", rc,
                          [f"check: {len(problems)} preflight problem(s)"],
                          started,
                          [{"action": "preflight", "reason": p} for p in problems])
        return rc

    if args.list:
        return action_list(cfg, args)
    if args.verify:
        started = dt.datetime.now()
        rc = action_verify(cfg, args)
        state = "ok" if rc == EXIT_OK else "problems found"
        failures = ([{"action": "verify", "reason": "problems found"}]
                    if rc != EXIT_OK else None)
        return finish(cfg, "verify", rc, [f"verify: {state}"], started, failures)
    if args.restore:
        started = dt.datetime.now()
        try:
            stats = action_restore(cfg, args)
        except Locked as exc:
            LOG.error("%s", exc)
            return EXIT_LOCKED
        except (ActionFailed, CommandError, OSError) as exc:
            LOG.error("%s", exc)
            return finish(cfg, "restore", EXIT_ERROR, [], started,
                          failure_entries(exc, "restore"))
        LOG.info("restore: %d file(s), %s", stats["files"], fmt_bytes(stats["bytes"]))
        summary = f"restore: {stats['files']} file(s), {fmt_bytes(stats['bytes'])}"
        return finish(cfg, "restore", EXIT_OK, [summary], started)

    actions = requested_actions(args)
    if not actions:
        show_available_actions(cfg)
        return EXIT_OK

    started = dt.datetime.now()
    problems = preflight_problems(cfg, actions)
    if problems:
        for problem in problems:
            LOG.error("preflight: %s", problem)
        return finish(cfg, "backup", EXIT_ERROR, [], started,
                      [{"action": "preflight", "reason": p} for p in problems])

    rc = EXIT_OK
    details = []
    handlers = {
        "database": (action_database, "{databases} db(s), {uploaded} uploaded, {deleted} pruned, {size}"),
        "storage": (action_storage, "{sources} source(s), {remote_files} file(s) in cloud"),
        "archive": (action_archive, "{uploaded} uploaded, {deleted} pruned, {size}"),
        "incremental": (action_incremental, "{uploaded} uploaded, {deleted} pruned, {size}"),
        "vault": (action_vault, "{layers} layer(s), {files} file(s), {deleted} deleted, {size}"),
    }
    try:
        for name in actions:
            handler, template = handlers[name]
            stats = run_action(cfg, name, args, handler)
            if stats.get("skipped"):
                continue
            fields = dict(stats)
            fields["size"] = fmt_bytes(stats.get("bytes", 0))
            LOG.info("%s: %s", name, template.format(**fields))
            details.append(f"{name}: " + template.format(**fields))
    except Locked as exc:
        LOG.error("%s", exc)
        return EXIT_LOCKED
    except (ActionFailed, CommandError, ConfigError, OSError) as exc:
        LOG.error("%s", exc)
        failed = actions[len(details)] if len(details) < len(actions) else "run"
        return finish(cfg, "backup", EXIT_ERROR, details, started,
                      failure_entries(exc, failed))
    LOG.info("done in %.1fs, exit %d", (dt.datetime.now() - started).total_seconds(), rc)
    return finish(cfg, "backup", rc, details, started)


def finish(cfg, kind, code, details, started, failures=None):
    """Notify about the run; a delivery problem never changes the exit code."""
    Notifier.run(cfg, kind, code, details, started, failures)
    return code


ACTIONS = ("database", "storage", "archive", "incremental", "vault")


def requested_actions(args):
    """Only what was asked on the command line. Nothing is implicit: a run
    without an action flag must never start a backup."""
    return [name for name in ACTIONS if getattr(args, name)]


def enabled_actions(cfg):
    return [name for name in ACTIONS if getattr(cfg, name).enabled]


def _ansi(code):
    """ANSI colour, disabled for non-TTY and when NO_COLOR is set."""
    if os.environ.get("NO_COLOR") is not None or not sys.stdout.isatty():
        return ""
    return code


def print_banner(stream=sys.stdout):
    print(f"{APP}: Backup tool over rclone: database dumps, file storage, archives "
          f"(Version: {VERSION})", file=stream)


def deferred_for(cfg, name):
    """Config problems of a disabled section, if any."""
    prefix = f"config.{name}"
    return [m for m in cfg.deferred_errors
            if m.startswith(prefix + ".") or m.startswith(prefix + ":")]


def show_available_actions(cfg):
    """Same shape as the legacy shell script: one line per action, the
    option name highlighted, plus whether the action is usable in this file."""
    print_banner()
    print()
    yellow = _ansi("\033[33m")
    green = _ansi("\033[32m")
    dim = _ansi("\033[2m")
    reset = _ansi("\033[0m")
    print("No one backup action(s) requested. Allowed:")
    for name in ACTIONS:
        bad = deferred_for(cfg, name)
        if getattr(cfg, name).enabled:
            state = f"{green}enabled{reset}"
        elif name in cfg.present_sections:
            state = f"{dim}disabled{reset}"
            if bad:
                state += f", {yellow}invalid config{reset} (see --check)"
        else:
            state = f"{dim}not configured{reset}"
        option = f"{yellow}{('--' + name):<10}{reset}"
        print(f"... {name:<9} (use {option} option)  {state}")
    print()
    print("Nothing has been done.")
    print(f"Validate the config with: kwbackup --config {cfg.path} --check")


def _color(text, code):
    """ANSI colour, silently ignored when the output is not a terminal."""
    prefix = _ansi(code)
    return f"{prefix}{text}{_ansi(chr(27) + '[0m')}" if prefix else text


def _label(text, width=0):
    """A field name in yellow, padded to `width` visible characters."""
    return _color(f"{text:<{width}}", chr(27) + "[33m")


def _chunks(items, width=58):
    """Wrap a list of names into lines of about `width` characters."""
    lines, current = [], ""
    for item in items:
        piece = item if not current else ", " + item
        if len(current) + len(piece) > width:
            lines.append(current)
            current = item
        else:
            current += piece
    if current:
        lines.append(current)
    return lines


def _s3_overrides(cfg, section):
    """What this section changes in rclone.s3-custom-options. Values are never
    printed: they may be credentials."""
    base = cfg.rclone.s3_options
    changed = []
    for key, value in section.s3_options.items():
        if value is None:
            changed.append(f"-{key}")
        elif key not in base or base[key] != value:
            changed.append(f"+{key}")
    return sorted(changed)


def _print_section(cfg, name, section):
    def row(label, value):
        print(f"    {_label(label, 10)} {value}")

    if name == "vault":
        row("root", section.root)
        row("state_dir", section.state_dir)
        row("volume_size", fmt_bytes(section.volume_size))
        row("checksum", section.checksum)
        if section.journal and section.journal.type == "txt":
            row("journal", f"txt, {section.journal.path}")
            keep = section.journal.keep_done
            row("keep_done", "forever" if keep is None else f"{keep.days}d")
        else:
            row("journal", "scan (full every run)")
        row("targets", ", ".join(section.targets))
        order = section.restore_order
        row("restore", f"{order.direction}, priority: "
                       f"{', '.join(order.priority) or 'none'}")
    elif name == "database":
        row("mode", section.mode)
        row("targets", ", ".join(section.targets))
        row("databases", ", ".join(section.databases) or "(none)")
        row("compressor", section.compressor)
        for cls in RETENTION_CLASSES:
            ret = section.retention.get(cls)
            row(cls, ret.describe() if ret else "off")
        if section.catch_up:
            row("catch_up", "on")
    elif name == "incremental":
        row("name", section.name)
        row("root", section.root)
        row("initial_day", section.initial_day)
        row("snapshot", section.snapshot)
        row("targets", ", ".join(section.targets))
        row("retention", section.retention.describe() if section.retention
            else "none (never pruned)")
        row("include", ", ".join(section.include) or "(none)")
        row("exclude", ", ".join(section.exclude) or "(none)")
    elif name == "storage":
        row("targets", ", ".join(section.targets))
        row("sources", len(section.sources))
        for src, dest in section.sources:
            print(f"      {src}  ->  {source_dest(src, dest) or '.'}")
    else:
        row("name", section.name)
        row("root", section.root)
        row("mode", section.mode)
        row("targets", ", ".join(section.targets))
        row("retention", section.retention.describe() if section.retention
            else "none (never pruned)")
        row("include", ", ".join(section.include) or "(none)")
        row("exclude", ", ".join(section.exclude) or "(none)")

    for phase in ("before", "after"):
        commands = getattr(section, phase, None) or []
        if commands:
            row(phase, f"{len(commands)} hook(s)")
    overrides = _s3_overrides(cfg, section)
    if overrides:
        colored = " ".join(_color(part, chr(27) + "[33m") for part in overrides)
        row("s3", colored + "  (override rclone)")


def check_config(cfg, args):
    green = chr(27) + "[32m"
    red = chr(27) + "[31m"
    dim = chr(27) + "[2m"

    print_banner()
    print()
    print(f"  {_label('config', 10)} {cfg.path}")
    print(f"  {_label('defaults', 10)} {cfg.defaults_path or 'none (built-in defaults only)'}")
    print(f"  {_label('project', 10)} {cfg.project}")
    console = ("debug" if (args.debug or cfg.debug)
               else "verbose" if (args.verbose or cfg.verbose)
               else "quiet (warnings and errors)")
    if cfg.log_syslog:
        where = "system socket" if cfg.log_syslog is True else cfg.log_syslog
        syslog = f"syslog {cfg.log_facility} ({where})"
    else:
        syslog = "syslog off"
    print(f"  {_label('log', 10)} level {cfg.log_level}, "
          f"file {cfg.log_file or 'none'}, {syslog}")
    print(f"  {_label('console', 10)} {console}")
    print()

    print(f"  {_label('rclone')}")
    print(f"    {_label('binary', 10)} {cfg.rclone.binary}")
    if cfg.rclone.args:
        print(f"    {_label('args', 10)} {len(cfg.rclone.args)} flag(s) passed as-is")
    if cfg.rclone.s3_options:
        keys = sorted(cfg.rclone.s3_options)
        print(f"    {_label('s3', 10)} {len(keys)} flag(s)")
        for chunk in _chunks(keys):
            colored = ", ".join(_color(key, chr(27) + "[33m") for key in chunk.split(", "))
            print(f"    {'':<10} {colored}")
    print(f"  {_label('upstreams')}")
    for name, up in cfg.upstreams.items():
        flags = f", {len(up.s3_options)} s3 flag(s)" if up.s3_options else ""
        print(f"    {_label(name, 10)} {up.config or 'default rclone config'}{flags}")
    print()

    for name in ACTIONS:
        section = getattr(cfg, name)
        if not section.enabled:
            state = "disabled" if name in cfg.present_sections else "not configured"
            print(f"  {_label(name, 9)} {_color(state, dim)}")
        else:
            detail = {"database": lambda: section.type,
                      "storage": lambda: section.mode,
                      "archive": lambda: section.compressor,
                      "incremental": lambda: f"day {section.initial_day}",
                      "vault": lambda: (section.journal.type
                                        if section.journal else "scan")}[name]()
            print(f"  {_label(name, 9)} {_color('enabled', green)} "
                  f"{_color('(' + detail + ')', dim)}")
            _print_section(cfg, name, section)
        print()

    notify = cfg.notify
    if notify and notify.channels():
        channels = notify.channels()
        print(f"  {_label('notify')}")
        print(f"    {_label('on', 10)} {', '.join(notify.on) or 'off'}")
        print(f"    {_label('retries', 10)} {notify.retries}, "
              f"delay {notify.retry_delay}s, timeout {notify.timeout}s")
        if notify.mail:
            tls = ", tls" if notify.mail.tls else ""
            print(f"    {_label('mail', 10)} {', '.join(notify.mail.to)} "
                  f"via {notify.mail.smtp_host}:{notify.mail.port}{tls}")
        if notify.webhook:
            parts = urllib.parse.urlsplit(notify.webhook.url)
            headers = f", {len(notify.webhook.headers)} header(s)" if notify.webhook.headers else ""
            print(f"    {_label('webhook', 10)} {parts.scheme}://{parts.netloc}{headers}")
        if notify.telegram:
            print(f"    {_label('telegram', 10)} chat {notify.telegram.chat_id}, "
                  f"token from {notify.telegram.token_env}")
        print()

    if cfg.warnings:
        print(f"  {_label('warnings', 9)} {_color(str(len(cfg.warnings)), chr(27) + '[33m')}")
        for warning in cfg.warnings:
            print(f"    {_color('!', chr(27) + '[33m')} {warning}")
        print()
    problems = preflight_problems(cfg, enabled_actions(cfg))
    if problems:
        print(f"  {_label('preflight', 9)} {_color('problems', red)}")
        for problem in problems:
            print(f"    {_color('!', red)} {problem}")
    else:
        print(f"  {_label('preflight', 9)} {_color('ok', green)}")
    return (EXIT_ERROR if problems else EXIT_OK), problems
