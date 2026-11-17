# ============================================================================
# DATABASE
# ============================================================================

def dump_command(cfg, dbname, binary=None):
    db = cfg.database
    kind = db.type
    if kind == "sqlite":
        return [binary or "sqlite3", db.host, ".dump"]
    if kind in ("mysql", "mariadb"):
        exe = binary or ("mariadb-dump" if kind == "mariadb" else "mysqldump")
        cmd = [exe, "--single-transaction", "--quick", "--no-tablespaces",
               "--skip-lock-tables"] + list(db.args or [])
        for table in db.ignore_table or []:
            name = table if "." in table else f"{dbname}.{table}"
            cmd.append(f"--ignore-table={name}")
        cmd.append(f"--host={db.host}")
        if db.port:
            cmd.append(f"--port={db.port}")
        if db.user:
            cmd.append(f"--user={db.user}")
        return cmd + [dbname]
    if kind in ("postgres", "pgsql"):
        cmd = ["pg_dump", "--no-owner", "--no-privileges"] + list(db.args or [])
        for table in db.ignore_table or []:
            cmd.append(f"--exclude-table={table}")
        cmd += ["--host", db.host]
        if db.port:
            cmd += ["--port", str(db.port)]
        if db.user:
            cmd += ["--username", db.user]
        return cmd + ["--dbname", dbname]
    raise CommandError(f"dump: unsupported db type {kind}", 1)


def compress_stage(cfg_compressor, cfg_args, out_path, inner_name):
    """Return (stage_cmd, last_writes_file) or (None, False) for no compression."""
    comp = cfg_compressor
    args = list(cfg_args)
    if comp == "zstd":
        return (["zstd", "--quiet"] + args + ["-c"], False)
    if comp in ("gzip", "pigz"):
        exe = "pigz" if comp == "pigz" else "gzip"
        return ([exe] + args + ["-c"], False)
    if comp == "rar":
        return (["rar", "a", f"-si{inner_name}", "-inul"] + args + [out_path], True)
    return (None, False)


COMPRESSOR_EXT = {"zstd": ".zst", "gzip": ".gz", "pigz": ".gz", "rar": ".rar", "none": ""}


def action_database(cfg, args):
    """Dump every database once, upload it to every target, prune each target."""
    db = cfg.database
    if not db.enabled and not args.force:
        LOG.warning("database backup is disabled in config (use --force to override)")
        return {"skipped": True}

    cfg.temp_dir.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now()
    stamp = now.strftime(TS_FORMAT)
    ext = COMPRESSOR_EXT[db.compressor]
    multi = len(db.databases) > 1
    total = {"databases": 0, "uploaded": 0, "deleted": 0, "bytes": 0}
    failures, details = [], []

    for dbname in db.databases:
        out_name = f"{dbname}_{stamp}.sql{ext}"
        out_path = cfg.temp_dir / out_name
        try:
            with task_lock(f"{cfg.project}.db.{dbname}", cfg.path):
                LOG.info("[%s] dumping %s (%s)", dbname, dbname, db.type)
                env = dict(os.environ)
                if db.password_env:
                    env["MYSQL_PWD"] = os.environ.get(db.password_env, "")
                    if db.type in ("postgres", "pgsql"):
                        env["PGPASSWORD"] = os.environ.get(db.password_env, "")
                stages = [dump_command(cfg, dbname)]
                comp, writes_file = compress_stage(db.compressor, db.compressor_args,
                                                   str(out_path), out_name)
                if comp:
                    stages.append(comp)
                run_pipeline(stages, str(out_path), env=env, last_writes_file=writes_file,
                             progress="dumping")
                size = 0
                if not DRY:
                    size = out_path.stat().st_size
                    if size == 0:
                        raise CommandError("dump produced an empty file", 1)
                    LOG.info("[%s] dump ready: %s", dbname, fmt_bytes(size))

                mode = args.mode or db.mode
                for target in db.targets:
                    upstream = target.partition(":")[0]
                    try:
                        uploader = rclone_for(cfg, upstream, db)
                        for cls in RETENTION_CLASSES:
                            retention = db.retention.get(cls)
                            if retention is None:
                                continue
                            remote_dir = target_join(
                                target, dbname if multi else None, cls.upper())
                            existing = owned(uploader.lsf(remote_dir), f"{dbname}_")
                            if not class_due(cls, existing, db.catch_up, now):
                                continue
                            if mode == "sync":
                                LOG.debug("[%s] -m sync: %s is mirrored, older copies "
                                          "are deleted", dbname, remote_dir)
                            upload_file(uploader, out_path, remote_dir, mode, cfg.temp_dir)
                            total["uploaded"] += 1
                            LOG.info("[%s] %s -> %s/%s (%s)", dbname,
                                     "would upload" if DRY else "uploaded", remote_dir,
                                     out_name, "mirror" if mode == "sync"
                                     else retention.describe())
                            if mode == "sync":
                                continue
                            files = uploader.lsf(remote_dir)
                            for stale in plan_prune(files, retention, now,
                                                    prefix=f"{dbname}_"):
                                uploader.deletefile(f"{remote_dir}/{stale}")
                                total["deleted"] += 1
                                LOG.info("[%s] pruned %s/%s", dbname, cls.upper(), stale)
                    except (CommandError, OSError) as exc:
                        LOG.error("[%s] upstream %s: %s", dbname, upstream, exc)
                        reason = failure_reason(exc)
                        failures.append(f"{dbname}@{upstream}: {reason}")
                        details.append({"action": "database",
                                        "entity": f"{dbname}@{upstream}",
                                        "reason": reason})
                total["databases"] += 1
                total["bytes"] += size
        except Locked:
            raise
        except (CommandError, OSError) as exc:
            LOG.error("[%s] %s", dbname, exc)
            reason = failure_reason(exc)
            failures.append(f"{dbname}: {reason}")
            details.append({"action": "database", "entity": dbname, "reason": reason})
        finally:
            if not cfg.keep_temp and out_path.exists():
                out_path.unlink()
    if failures:
        raise ActionFailed("database backup failed: " + "; ".join(failures),
                           details)
    return total
