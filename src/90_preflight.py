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

    if "vault" in actions:
        vault = cfg.vault
        wanted.append("tar")
        root = Path(vault.root or "")
        if not root.is_dir():
            problems.append(f"vault root is not a directory: {vault.root}")
        elif not os.access(root, os.R_OK | os.X_OK):
            problems.append(f"vault root is not readable: {vault.root}")
        state = Path(vault.state_dir)
        try:
            state.mkdir(parents=True, exist_ok=True)
            probe = state / f".probe-{os.getpid()}"
            probe.write_bytes(b"x")
            probe.unlink()
        except OSError as exc:
            problems.append(f"vault state_dir {state}: {exc.strerror}")
        journal = vault.journal
        if journal and journal.type == "txt":
            jdir = Path(journal.path)
            if not jdir.is_dir():
                problems.append(f"vault journal path is not a directory: {journal.path}")
            elif not os.access(jdir, os.R_OK | os.X_OK | os.W_OK):
                problems.append(f"vault journal path is not readable/writable: "
                                f"{journal.path}")
        for target in vault.targets:
            name = target.partition(":")[0]
            upstream = cfg.upstreams.get(name)
            if upstream is None:
                continue
            if upstream.type == "rclone":
                wanted.append(cfg.rclone.binary)
            elif upstream.type == "rsync":
                wanted += ["rsync", "ssh"]
        problems.extend(vault_path_problems(cfg))
    if any(action != "vault" for action in actions) and cfg.rclone.binary:
        wanted.append(cfg.rclone.binary)

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


def _dump_binary(db):
    if db.type in ("mysql", "mariadb"):
        return "mariadb-dump" if db.type == "mariadb" else "mysqldump"
    if db.type in ("postgres", "pgsql"):
        return "pg_dump"
    return "sqlite3"
