# ============================================================================
# CLI
# ============================================================================

def build_parser():
    p = argparse.ArgumentParser(
        prog=PROG, description="Backup tool: database dumps, file storage, archives.",
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
    p.add_argument("--verbose", action="store_true", help="more messages")
    p.add_argument("--debug", action="store_true", help="debug messages, print every command")
    p.add_argument("--selftest", action="store_true", help="run internal tests and exit")
    p.add_argument("--version", action="store_true", help="print version and exit")
    return p


def main(argv=None):
    global DRY
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "migrate":
        return migrate_main(argv[1:])
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()
    if args.version:
        print(f"{PROG} {VERSION}")
        return EXIT_OK
    if not args.config:
        parser.print_usage()
        print(f"{PROG}: error: -c/--config is required", file=sys.stderr)
        return EXIT_USAGE

    try:
        cfg = load_config(args.config, defaults=args.defaults)
    except ConfigError as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    setup_logging(cfg, args.verbose, args.debug)
    DRY = args.dry_run
    LOG.debug("config: %s (project %s)", cfg.path, cfg.project)
    LOG.debug("defaults: %s", cfg.defaults_path or "none (built-in)")
    for warning in secret_warnings(cfg):
        LOG.warning("%s", warning)

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

    actions = requested_actions(cfg, args)
    if not actions:
        print("Nothing to do. Enable an action in config or pass -d/-s/-a.")
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


def requested_actions(cfg, args, silent=False):
    actions = [name for name, flag in (("database", args.database),
                                       ("storage", args.storage),
                                       ("archive", args.archive)) if flag]
    if actions:
        return actions
    actions = [name for name, enabled in (("database", cfg.database.enabled),
                                          ("storage", cfg.storage.enabled),
                                          ("archive", cfg.archive.enabled)) if enabled]
    if actions and not silent:
        LOG.info("no action requested, running enabled ones: %s", ", ".join(actions))
    return actions


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
        print(f"  archive: {cfg.archive.compressor}, root {cfg.archive.root}, "
              f"mode {cfg.archive.mode}, target {cfg.archive.target}, "
              f"retention {ret.describe() if ret else 'none'}")
    problems = preflight_problems(cfg, requested_actions(cfg, args, silent=True))
    if problems:
        print("  preflight: warnings")
        for problem in problems:
            print(f"    ! {problem}")
    else:
        print("  preflight: ok")
    check = cfg.security.check_excessive_access_rights
    print(f"  security: permissions check {'on' if check else 'off'}")
    for warning in secret_warnings(cfg):
        print(f"    ! {warning}")
    return EXIT_OK
