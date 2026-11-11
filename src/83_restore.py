# ============================================================================
# RESTORE  (--restore --to DIR, optional --into-db with --yes)
# ============================================================================

def _download(uploader, remote_dir, rf, workdir):
    local = workdir / rf.name
    LOG.info("downloading %s/%s (%s)", remote_dir, rf.name, fmt_bytes(rf.size))
    uploader.copyto(f"{remote_dir}/{rf.name}", str(local))
    return local


def _decompress_to(local, dest_path):
    """Unpack local archive into dest_path (a file). Returns the result path."""
    name = local.name.lower()
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    if name.endswith((".zst", ".zstd")):
        run_pipeline([["zstd", "--quiet", "-dc", str(local)]], str(dest_path))
    elif name.endswith(".gz"):
        run_pipeline([["gzip", "-dc", str(local)]], str(dest_path))
    elif name.endswith(".rar"):
        run(["rar", "e", "-y", str(local), str(dest_path.parent) + "/"])
        dest_path = dest_path.parent / dest_path.name
    else:
        shutil.copy2(local, dest_path)
    return dest_path


def _extract_archive(local, dest_dir):
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = local.name.lower()
    if name.endswith(".rar"):
        run(["rar", "x", "-y", str(local), str(dest_dir) + "/"])
    elif name.endswith((".zst", ".zstd")):
        run_pipeline([["zstd", "--quiet", "-dc", str(local)],
                      ["tar", "-xf", "-", "-C", str(dest_dir)]],
                     str(dest_dir), last_writes_file=True)
    elif name.endswith(".gz"):
        run_pipeline([["gzip", "-dc", str(local)],
                      ["tar", "-xf", "-", "-C", str(dest_dir)]],
                     str(dest_dir), last_writes_file=True)
    else:
        run(["tar", "-xf", str(local), "-C", str(dest_dir)])


def _load_into_db(cfg, dbname, sql_path):
    """Stream a plain SQL dump into a live database. Destructive."""
    db = cfg.database
    env = _db_env(cfg)
    if db.type in ("mysql", "mariadb"):
        exe = "mariadb" if db.type == "mariadb" else "mysql"
        cmd = [exe, f"--host={db.host}"]
        if db.port:
            cmd.append(f"--port={db.port}")
        if db.user:
            cmd.append(f"--user={db.user}")
        cmd.append(dbname)
        with open(sql_path, "rb") as fh:
            if DRY:
                LOG.info("dry-run: %s < %s", _fmt_cmd(cmd), sql_path)
                return
            res = subprocess.run(cmd, stdin=fh, capture_output=True, text=True, env=env)
        if res.returncode != 0:
            raise CommandError(_fmt_cmd(cmd), res.returncode, res.stderr)
    elif db.type in ("postgres", "pgsql"):
        cmd = ["psql", "-h", db.host]
        if db.port:
            cmd += ["-p", str(db.port)]
        if db.user:
            cmd += ["-U", db.user]
        cmd += ["-d", dbname, "-v", "ON_ERROR_STOP=1", "-f", str(sql_path)]
        run(cmd, env=env)
    elif db.type == "sqlite":
        with open(sql_path, "rb") as fh:
            if DRY:
                LOG.info("dry-run: sqlite3 %s < %s", db.host, sql_path)
                return
            res = subprocess.run(["sqlite3", db.host], stdin=fh,
                                 capture_output=True, text=True, env=env)
        if res.returncode != 0:
            raise CommandError("sqlite3", res.returncode, res.stderr)
    else:
        raise ActionFailed(f"restore: unsupported database type {db.type!r}")


def _db_env(cfg):
    env = dict(os.environ)
    if cfg.database.password_env:
        value = os.environ.get(cfg.database.password_env, "")
        env["MYSQL_PWD"] = value
        env["PGPASSWORD"] = value
    return env


def _select_database_copy(uploader, cfg, dbname, at):
    candidates = []
    for cls in RETENTION_CLASSES:
        remote_dir = class_path(cfg, dbname, cls)
        for rf in uploader.lsf(remote_dir):
            if not rf.ts:
                continue
            if at and at not in rf.name:
                continue
            candidates.append((rf.ts, cls, remote_dir, rf))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[-1]


def action_restore(cfg, args):
    what = [name for name, flag in (("database", args.database),
                                    ("storage", args.storage),
                                    ("archive", args.archive)) if flag]
    if not what:
        what = [name for name, enabled in (("database", cfg.database.enabled),
                                           ("storage", cfg.storage.enabled),
                                           ("archive", cfg.archive.enabled)) if enabled]
    if not what:
        raise ActionFailed("restore: nothing selected (-d/-s/-a) and nothing enabled in config")
    if args.into_db:
        if "database" not in what:
            raise ActionFailed("restore: --into-db requires -d")
        if not args.yes:
            raise ActionFailed("restore: --into-db writes into a live database, "
                               "add --yes to confirm")
    if not args.to and not args.into_db:
        raise ActionFailed("restore: --to DIR is required")

    cfg.temp_dir.mkdir(parents=True, exist_ok=True)
    uploader = Rclone(cfg.rclone)
    dest_dir = Path(args.to) if args.to else None
    stats = {"files": 0, "bytes": 0}

    if "database" in what:
        for dbname in cfg.database.databases:
            picked = _select_database_copy(uploader, cfg, dbname, args.at)
            if picked is None:
                raise ActionFailed(f"restore: no copy found for {dbname}"
                                   + (f" matching {args.at!r}" if args.at else ""))
            ts, cls, remote_dir, rf = picked
            local = _download(uploader, remote_dir, rf, cfg.temp_dir)
            try:
                stats["files"] += 1
                stats["bytes"] += local.stat().st_size
                LOG.info("restore: %s from %s/%s (%s)", dbname, cls.upper(),
                         rf.name, f"{ts:%Y-%m-%d %H:%M:%S}")
                if args.into_db:
                    target = args.into_db if args.into_db is not True else dbname
                    sql = _decompress_to(local, cfg.temp_dir / (rf.name.split(".sql")[0] + ".sql"))
                    LOG.warning("restore: loading %s into database %s", sql.name, target)
                    _load_into_db(cfg, target, sql)
                    sql.unlink(missing_ok=True)
                else:
                    out = _decompress_to(local, dest_dir / _sql_name(rf.name))
                    LOG.info("restore: written %s", out)
            finally:
                local.unlink(missing_ok=True)

    if "archive" in what:
        remote_dir = cfg.archive.target.rstrip("/")
        files = sorted((f for f in uploader.lsf(remote_dir)
                        if f.ts and (not args.at or args.at in f.name)),
                       key=lambda f: f.ts)
        if not files:
            raise ActionFailed("restore: no archive found"
                               + (f" matching {args.at!r}" if args.at else ""))
        rf = files[-1]
        local = _download(uploader, remote_dir, rf, cfg.temp_dir)
        try:
            stats["files"] += 1
            stats["bytes"] += local.stat().st_size
            LOG.info("restore: extracting %s into %s", rf.name, dest_dir)
            _extract_archive(local, dest_dir)
        finally:
            local.unlink(missing_ok=True)

    if "storage" in what:
        for src, dest in cfg.storage.sources:
            sub = source_dest(src, dest)
            remote_dir = cfg.storage.target.rstrip("/") + (f"/{sub}" if sub else "")
            local_dir = dest_dir / (sub or ".")
            LOG.info("restore: %s -> %s", remote_dir, local_dir)
            uploader.run("copy", remote_dir, str(local_dir))
            stats["files"] += 1

    return stats


def _sql_name(name):
    for ext in (".zst", ".zstd", ".gz", ".rar"):
        if name.lower().endswith(ext):
            return name[: -len(ext)]
    return name
