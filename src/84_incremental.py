# ============================================================================
# INCREMENTAL  (GNU tar chains: monthly full dump + daily increments)
#
# Chain = YYYY-MM, embedded in the file name:
#   <name>_<YYYY-MM>_full_<ts>.tar
#   <name>_<YYYY-MM>_inc_<ts>.tar
#
# A run belongs to the current month from `initial_day`, otherwise to the
# previous month. The first run of a chain is full (tar --level=0), the rest
# are increments (-g snapshot). A missing snapshot forces a full dump; restore
# uses the newest full in the chain.
# ============================================================================

def _snapshot_path(inc):
    path = Path(inc.snapshot)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    return path


def action_incremental(cfg, args):
    inc = cfg.incremental
    if not inc.enabled and not args.force:
        LOG.warning("incremental backup is disabled in config (use --force to override)")
        return {"skipped": True}

    root = Path(inc.root or "")
    if not root.is_dir():
        raise ActionFailed(f"incremental: root is not a directory: {inc.root!r}")

    cfg.temp_dir.mkdir(parents=True, exist_ok=True)
    snapshot = _snapshot_path(inc)
    now = dt.datetime.now()
    stamp = now.strftime(TS_FORMAT)

    # The chain state lives on the first target; an unreachable target forces
    # a full dump (safe: a duplicate full is still restorable).
    first = inc.targets[0]
    try:
        probe = rclone_for(cfg, first.partition(":")[0], inc)
        entries = inc_entries(probe.lsf(first.rstrip("/")), inc.name)
        chains_with_full = {chain for chain, kind, _, _ in entries if kind == "full"}
    except (CommandError, OSError) as exc:
        LOG.warning("incremental: cannot inspect %s (%s); forcing a full dump",
                    first, exc)
        chains_with_full = set()

    chain = chain_month(now, inc.initial_day)
    if not chains_with_full:
        chain = f"{now:%Y-%m}"          # very first run: start the current month
    is_full = not snapshot.exists() or chain not in chains_with_full
    kind = "full" if is_full else "inc"
    out_name = f"{inc.name}_{chain}_{kind}_{stamp}.tar"
    out_path = cfg.temp_dir / out_name
    stats = {"uploaded": 0, "deleted": 0, "bytes": 0, "kind": kind, "chain": chain}
    failures = []

    try:
        with task_lock(f"{cfg.project}.incremental", cfg.path):
            LOG.info("incremental: %s dump of %s -> %s", kind, root, out_name)
            base = tar_args(root, inc.include, inc.exclude)
            extra = ["-g", str(snapshot)]
            if is_full:
                extra.append("--level=0")
            cmd = [base[0]] + extra + base[1:]
            total = None
            if cfg.debug or getattr(args, "debug", False):
                total = source_bytes(root, inc.include, inc.exclude)
            run_pipeline([cmd], str(out_path), progress="packing", total=total)

            if not DRY:
                size = out_path.stat().st_size
                if size == 0:
                    raise CommandError("incremental archive is empty", 1)
                stats["bytes"] = size
                LOG.info("incremental: packed %s", fmt_bytes(size))

            for target in inc.targets:
                upstream = target.partition(":")[0]
                remote_dir = target.rstrip("/")
                try:
                    rclone = rclone_for(cfg, upstream, inc)
                    upload_file(rclone, out_path, remote_dir, "copy", cfg.temp_dir)
                    stats["uploaded"] += 1
                    LOG.info("incremental: %s -> %s/%s",
                             "would upload" if DRY else "uploaded", remote_dir, out_name)
                    if inc.retention:
                        files = rclone.lsf(remote_dir)
                        for stale in plan_chain_prune(files, inc.retention,
                                                      inc.name, now):
                            rclone.deletefile(f"{remote_dir}/{stale}")
                            stats["deleted"] += 1
                            LOG.info("incremental: pruned chain file %s", stale)
                except (CommandError, OSError) as exc:
                    LOG.error("incremental: upstream %s: %s", upstream, exc)
                    failures.append(f"{upstream}: {exc}")
    finally:
        if not cfg.keep_temp and out_path.exists():
            out_path.unlink()
    if failures:
        raise ActionFailed("incremental backup failed: " + "; ".join(failures))
    return stats
