# ============================================================================
# STORAGE  (rclone sync/copy of local directories into the cloud)
# ============================================================================

def source_dest(path, dest):
    """Directory name inside the target container for one source."""
    if dest is not None:
        return dest.strip("/")
    return Path(path.rstrip("/")).name


def action_storage(cfg, args):
    st = cfg.storage
    if not st.enabled and not args.force:
        LOG.warning("storage backup is disabled in config (use --force to override)")
        return {"skipped": True}

    mode = args.mode or st.mode
    if mode not in ("sync", "copy"):
        raise ActionFailed(f"storage: unsupported mode {mode!r}, expected sync or copy")

    stats = {"sources": 0, "remote_files": 0}
    failures, details = [], []

    with task_lock(f"{cfg.project}.storage", cfg.path):
        for target in st.targets:
            upstream = target.partition(":")[0]
            try:
                rclone = rclone_for(cfg, upstream, st)
                for path, dest in st.sources:
                    sub = source_dest(path, dest)
                    remote_dir = target_join(target, sub)
                    LOG.info("storage: %s %s -> %s", mode, path, remote_dir)
                    rclone.run(mode, "--copy-links", "--update", path, remote_dir,
                               banner=True)
                    files = rclone.lsf(remote_dir)
                    stats["sources"] += 1
                    stats["remote_files"] += len(files)
                    LOG.info("storage: %s ok, %d file(s) in %s", path, len(files),
                             remote_dir)
            except (CommandError, OSError) as exc:
                LOG.error("storage: upstream %s: %s", upstream, exc)
                reason = failure_reason(exc)
                failures.append(f"{upstream}: {reason}")
                details.append({"action": "storage", "entity": upstream,
                                "reason": reason})
    if failures:
        raise ActionFailed("storage backup failed: " + "; ".join(failures),
                           details)
    return stats
