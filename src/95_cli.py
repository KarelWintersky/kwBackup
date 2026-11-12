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
    p.add_argument("-m", "--mode", choices=("copy", "sync"),
                   help="override the upload algorithm (storage, database, "
                        "archive); sync mirrors the target directory")
    p.add_argument("-f", "--force", action="store_true",
                   help="run actions even if disabled in config")
    p.add_argument("--dry-run", action="store_true", help="show what would be done")
    p.add_argument("--check-config", action="store_true", help="validate config and exit")
    p.add_argument("--list", action="store_true", help="show what is stored in the cloud")
    p.add_argument("--verify", action="store_true", help="download and check the newest copy")
    p.add_argument("--all", action="store_true", help="with --verify: check every stored copy")
    p.add_argument("--restore", action="store_true", help="download a copy from the cloud")
    p.add_argument("--to", metavar="DIR", help="restore target directory")
    p.add_argument("--at", metavar="TS", help="pick a copy by date prefix, e.g. 2026-11-09")
    p.add_argument("--into-db", metavar="NAME", help="with --restore: load the dump into "
                                                    "a live database (needs --yes)")
    p.add_argument("--yes", action="store_true", help="confirm destructive operations")
    p.add_argument("--verbose", action="store_true",
                   help="info messages on the terminal and rclone --progress")
    p.add_argument("--debug", action="store_true", help="debug messages, print every command")
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
        print(f"{APP} {VERSION}")
        return EXIT_OK
    if not args.config:
        print(f"{APP}: Backup tool over rclone: database dumps, file storage, archives",
              file=sys.stderr)
        print(f"Version: {VERSION}", file=sys.stderr)
        print(file=sys.stderr)
        parser.print_usage(sys.stderr)
        print(file=sys.stderr)
        print("ERROR: -c/--config is required", file=sys.stderr)
        return EXIT_USAGE

    try:
        cfg = load_config(args.config, defaults=args.defaults)
    except ConfigError as exc:
        print(f"{APP}: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    DRY = args.dry_run
    VERBOSE = args.verbose or args.debug
    setup_logging(cfg, args.verbose, args.debug, dry_run=args.dry_run)
    LOG.debug("config: %s (project %s)", cfg.path, cfg.project)
    LOG.debug("defaults: %s", cfg.defaults_path or "none (built-in)")

    if args.check_config:
        return check_config(cfg, args)

    if args.list:
        return action_list(cfg, args)
    if args.verify:
        return action_verify(cfg, args)
    if args.restore:
        try:
            stats = action_restore(cfg, args)
        except Locked as exc:
            LOG.error("%s", exc)
            return EXIT_LOCKED
        except (ActionFailed, CommandError, OSError) as exc:
            LOG.error("%s", exc)
            return EXIT_ERROR
        LOG.info("restore: %d file(s), %s", stats["files"], fmt_bytes(stats["bytes"]))
        return EXIT_OK

    actions = requested_actions(args)
    if not actions:
        show_available_actions(cfg)
        return EXIT_OK

    if not preflight(cfg, actions):
        return EXIT_ERROR

    rc = EXIT_OK
    started = dt.datetime.now()
    handlers = {
        "database": (action_database, "{databases} db(s), {uploaded} uploaded, {deleted} pruned, {size}"),
        "storage": (action_storage, "{sources} source(s), {remote_files} file(s) in cloud"),
        "archive": (action_archive, "{uploaded} uploaded, {deleted} pruned, {size}"),
    }
    try:
        for name in actions:
            handler, template = handlers[name]
            stats = handler(cfg, args)
            if stats.get("skipped"):
                continue
            fields = dict(stats)
            fields["size"] = fmt_bytes(stats.get("bytes", 0))
            LOG.info("%s: %s", name, template.format(**fields))
    except Locked as exc:
        LOG.error("%s", exc)
        return EXIT_LOCKED
    except (ActionFailed, CommandError, ConfigError, OSError) as exc:
        LOG.error("%s", exc)
        return EXIT_ERROR
    LOG.info("done in %.1fs, exit %d", (dt.datetime.now() - started).total_seconds(), rc)
    return rc


ACTIONS = ("database", "storage", "archive")


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


def show_available_actions(cfg):
    """Same shape as the legacy shell script: one line per action, the
    option name highlighted, plus whether the action is usable in this file."""
    yellow = _ansi("\033[33m")
    green = _ansi("\033[32m")
    dim = _ansi("\033[2m")
    reset = _ansi("\033[0m")
    print("No one backup action(s) requested. Allowed:")
    for name in ACTIONS:
        if getattr(cfg, name).enabled:
            state = f"{green}enabled{reset}"
        elif name in cfg.present_sections:
            state = f"{dim}disabled{reset}"
        else:
            state = f"{dim}not configured{reset}"
        option = f"{yellow}{('--' + name):<10}{reset}"
        print(f"... {name:<9} (use {option} option)  {state}")
    print()
    print("Nothing has been done.")


def check_config(cfg, args):
    print(f"config OK: {cfg.path}")
    print(f"  defaults: {cfg.defaults_path or 'none (built-in defaults only)'}")
    print(f"  project: {cfg.project}")
    print(f"  rclone:  {cfg.rclone.remote} via {cfg.rclone.config or 'default config'}")
    if cfg.database.enabled:
        print(f"  database: {cfg.database.type}, {len(cfg.database.databases)} db(s), "
              f"compressor {cfg.database.compressor}, mode {cfg.database.mode}, "
              f"target {cfg.database.target}")
        for cls in RETENTION_CLASSES:
            ret = cfg.database.retention.get(cls)
            print(f"    {cls:8} {ret.describe() if ret else 'disabled'}")
        if cfg.database.catch_up:
            print("    catch_up: on (missing weekly/monthly copies are created on the next run)")
    if cfg.storage.enabled:
        print(f"  storage: {cfg.storage.mode}, {len(cfg.storage.sources)} source(s), "
              f"target {cfg.storage.target}")
        for src, dest in cfg.storage.sources:
            print(f"    {src} -> {source_dest(src, dest) or '.'}")
    if cfg.archive.enabled:
        ret = cfg.archive.retention
        print(f"  archive: {cfg.archive.compressor}, name {cfg.archive.name}, "
              f"root {cfg.archive.root}, mode {cfg.archive.mode}, "
              f"target {cfg.archive.target}, "
              f"retention {ret.describe() if ret else 'none'}")
    problems = preflight_problems(cfg, enabled_actions(cfg))
    if problems:
        print("  preflight: warnings")
        for problem in problems:
            print(f"    ! {problem}")
    else:
        print("  preflight: ok")
    return EXIT_OK
