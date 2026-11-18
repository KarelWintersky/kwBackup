# ============================================================================
# RESTORE  (--restore --to DIR: copy/download only, never touches a live DB)
# ============================================================================

def _download(uploader, remote_dir, rf, workdir):
    local = workdir / rf.name
    LOG.info("downloading %s/%s (%s)", remote_dir, rf.name, fmt_bytes(rf.size))
    uploader.copyto(f"{remote_dir}/{rf.name}", str(local))
    return local


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


def _chosen_target(section, from_name, kind):
    """Restore always reads from exactly one target directory."""
    if from_name:
        for target in section.targets:
            if target.partition(":")[0] == from_name:
                return target
        raise ActionFailed(f"restore: {kind} has no target on upstream {from_name!r}")
    if not section.targets:
        raise ActionFailed(f"restore: {kind} has no targets configured")
    return section.targets[0]


def _select_database_copy(uploader, cfg, dbname, at, target):
    multi = len(cfg.database.databases) > 1
    candidates = []
    for cls in RETENTION_CLASSES:
        remote_dir = target_join(target, dbname if multi else None, cls.upper())
        for rf in owned(uploader.lsf(remote_dir), f"{dbname}_"):
            if not rf.ts:
                continue
            if at and at not in rf.name:
                continue
            candidates.append((rf.ts, cls, remote_dir, rf))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[-1]


def _incremental_plan(uploader, inc, target, at):
    """Newest full of the chain + the increments up to `at`."""
    remote_dir = target.rstrip("/")
    entries = inc_entries(uploader.lsf(remote_dir), inc.name)
    if not entries:
        raise ActionFailed("restore: no incremental copies found")
    chains = sorted({chain for chain, _, _, _ in entries})

    chain, limit = None, None
    if at:
        match = re.match(r"^(\d{4})-(\d{2})(?:-(\d{2}))?", at)
        if not match:
            raise ActionFailed(f"restore: bad --at {at!r}, expected YYYY-MM or YYYY-MM-DD")
        chain = f"{match.group(1)}-{match.group(2)}"
        if match.group(3):
            limit = dt.datetime.strptime(f"{chain}-{match.group(3)}", "%Y-%m-%d")
    if chain is None:
        chain = chains[-1]
    if chain not in chains:
        raise ActionFailed(f"restore: chain {chain} not found (available: {', '.join(chains)})")

    in_chain = [entry for entry in entries if entry[0] == chain]
    fulls = sorted((ts, rf) for _, kind, ts, rf in in_chain if kind == "full" and ts)
    if not fulls:
        raise ActionFailed(f"restore: chain {chain} has no full dump")
    incs = sorted((ts, rf) for _, kind, ts, rf in in_chain if kind == "inc" and ts)
    if limit:
        incs = [item for item in incs if item[0].date() <= limit.date()]
    return chain, fulls[-1][1], [rf for _, rf in incs]


def _extract_incremental(local, dest_dir):
    dest_dir.mkdir(parents=True, exist_ok=True)
    run(["tar", "-x", "-g", "/dev/null", "-C", str(dest_dir), "-f", str(local)])


def action_restore(cfg, args):
    what = [name for name, flag in (("database", args.database),
                                    ("storage", args.storage),
                                    ("archive", args.archive),
                                    ("incremental", args.incremental),
                                    ("vault", args.vault)) if flag]
    if not what:
        what = [name for name, enabled in (("database", cfg.database.enabled),
                                           ("storage", cfg.storage.enabled),
                                           ("archive", cfg.archive.enabled),
                                           ("incremental", cfg.incremental.enabled),
                                           ("vault", cfg.vault.enabled))
                if enabled]
    if not what:
        raise ActionFailed("restore: nothing selected (-d/-s/-a) and nothing enabled in config")
    if not args.to:
        raise ActionFailed("restore: --to DIR is required")
    for name in what:
        section = getattr(cfg, name)
        if not section.restore.enabled and not args.force:
            raise ActionFailed(
                f"restore: {name} restore is disabled in the config "
                f"({name}.restore.enabled: false); add --force to override")

    cfg.temp_dir.mkdir(parents=True, exist_ok=True)
    dest_dir = Path(args.to)
    stats = {"files": 0, "bytes": 0}

    if "database" in what:
        target = _chosen_target(cfg.database, args.from_upstream, "database")
        uploader = rclone_for(cfg, target.partition(":")[0], cfg.database)
        for dbname in cfg.database.databases:
            picked = _select_database_copy(uploader, cfg, dbname, args.at, target)
            if picked is None:
                raise ActionFailed(f"restore: no copy found for {dbname}"
                                   + (f" matching {args.at!r}" if args.at else ""))
            ts, cls, remote_dir, rf = picked
            dest_dir.mkdir(parents=True, exist_ok=True)
            out = dest_dir / rf.name
            LOG.info("restore: %s from %s/%s (%s)", dbname, cls.upper(),
                     rf.name, f"{ts:%Y-%m-%d %H:%M:%S}")
            uploader.copyto(f"{remote_dir}/{rf.name}", str(out))
            stats["files"] += 1
            stats["bytes"] += out.stat().st_size
            LOG.info("restore: written %s", out)

    if "archive" in what:
        target = _chosen_target(cfg.archive, args.from_upstream, "archive")
        uploader = rclone_for(cfg, target.partition(":")[0], cfg.archive)
        remote_dir = target.rstrip("/")
        files = sorted((f for f in owned(uploader.lsf(remote_dir),
                                         f"{cfg.archive.name}_")
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

    if "incremental" in what:
        inc = cfg.incremental
        target = _chosen_target(inc, args.from_upstream, "incremental")
        uploader = rclone_for(cfg, target.partition(":")[0], inc)
        remote_dir = target.rstrip("/")
        chain, full, incs = _incremental_plan(uploader, inc, target, args.at)
        LOG.info("restore: chain %s: full + %d increment(s)", chain, len(incs))
        for rf in [full] + incs:
            local = _download(uploader, remote_dir, rf, cfg.temp_dir)
            try:
                stats["files"] += 1
                stats["bytes"] += local.stat().st_size
                LOG.info("restore: extracting %s", rf.name)
                _extract_incremental(local, dest_dir)
            finally:
                local.unlink(missing_ok=True)

    if "vault" in what:
        vault_stats = vault_restore(cfg, args)
        stats["files"] += vault_stats["files"]
        stats["bytes"] += vault_stats["bytes"]

    if "storage" in what:
        target = _chosen_target(cfg.storage, args.from_upstream, "storage")
        uploader = rclone_for(cfg, target.partition(":")[0], cfg.storage)
        for src, dest in cfg.storage.sources:
            sub = source_dest(src, dest)
            remote_dir = target_join(target, sub)
            local_dir = dest_dir / (sub or ".")
            LOG.info("restore: %s -> %s", remote_dir, local_dir)
            uploader.run("copy", remote_dir, str(local_dir), banner=True)
            stats["files"] += 1

    return stats
