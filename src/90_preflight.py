# ============================================================================
# PREFLIGHT
# ============================================================================

def preflight_problems(cfg, actions):
    """Everything that would make the requested actions fail, collected up front."""
    problems = []
    wanted = []

    if "database" in actions:
        db = cfg.database
        wanted.append(_dump_binary(db))
        if db.compressor != "none":
            wanted.append(db.compressor)
        if db.password_env and db.password_env not in os.environ:
            problems.append(f"environment variable {db.password_env} is not set")
    if "storage" in actions:
        wanted.append("rclone")
        for src, _dest in cfg.storage.sources:
            if not Path(src).exists():
                problems.append(f"storage source does not exist: {src}")
            elif not os.access(src, os.R_OK):
                problems.append(f"storage source is not readable: {src}")
    if "archive" in actions:
        wanted += ["rclone", "tar"]
        root = Path(cfg.archive.root or "")
        if not root.is_dir():
            problems.append(f"archive root is not a directory: {cfg.archive.root}")
        elif not os.access(root, os.R_OK | os.X_OK):
            problems.append(f"archive root is not readable: {cfg.archive.root}")
        if cfg.archive.compressor != "none":
            wanted.append(cfg.archive.compressor)

    for binary in dict.fromkeys(wanted):
        if binary and shutil.which(binary) is None:
            problems.append(f"binary not found in PATH: {binary}")

    try:
        cfg.temp_dir.mkdir(parents=True, exist_ok=True)
        probe = cfg.temp_dir / f".probe-{os.getpid()}"
        probe.write_bytes(b"x")
        probe.unlink()
    except OSError as exc:
        problems.append(f"temp dir {cfg.temp_dir}: {exc.strerror}")

    for upstream in cfg.upstreams.values():
        if upstream.config and not Path(upstream.config).exists():
            problems.append(f"rclone config for upstream {upstream.name!r} "
                            f"not found: {upstream.config}")
    return problems


def preflight(cfg, actions):
    problems = preflight_problems(cfg, actions)
    for problem in problems:
        LOG.error("preflight: %s", problem)
    return not problems


def _dump_binary(db):
    if db.type in ("mysql", "mariadb"):
        return "mariadb-dump" if db.type == "mariadb" else "mysqldump"
    if db.type in ("postgres", "pgsql"):
        return "pg_dump"
    return "sqlite3"
