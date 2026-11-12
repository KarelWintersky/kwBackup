# ============================================================================
# ARCHIVE  (tar stream -> compressor -> cloud, with retention)
# ============================================================================

ARCHIVE_EXT = {"zstd": ".tar.zst", "gzip": ".tar.gz", "pigz": ".tar.gz",
               "rar": ".rar", "none": ".tar"}


def tar_command(cfg, root):
    """tar to stdout; include patterns are relative to root (-C).

    With the default include list ["*"] the whole root is archived as ".",
    which also picks up dotfiles (.env, .git, ...) at the top level.
    """
    ar = cfg.archive
    cmd = ["tar", "-C", str(root), "-cf", "-"]
    for pattern in ar.exclude:
        if pattern:
            cmd.append(f"--exclude={pattern}")
    includes = [p for p in ar.include if p]
    if not includes or includes == ["*"]:
        cmd.append(".")
        return cmd
    # tar does not expand globs on create: do it here, relative to root.
    paths = []
    for pattern in includes:
        matches = glob.glob(str(Path(root) / pattern), recursive=True)
        if not matches:
            LOG.warning("archive: include pattern matched nothing: %s", pattern)
            continue
        paths.extend(os.path.relpath(m, str(root)) for m in matches)
    paths = sorted(dict.fromkeys(paths))
    if not paths:
        raise ActionFailed("archive: no files matched the include list")
    if len(paths) > 5000:
        LOG.warning("archive: include list expanded to %d paths", len(paths))
    return cmd + paths


def action_archive(cfg, args):
    ar = cfg.archive
    if not ar.enabled and not args.force:
        LOG.warning("archive backup is disabled in config (use --force to override)")
        return {"skipped": True}

    if not ar.root or not Path(ar.root).is_dir():
        raise ActionFailed(f"archive: root is not a directory: {ar.root!r}")

    mode = args.mode or ar.mode
    if mode == "sync":
        LOG.debug("archive: -m sync: %s is mirrored, previous archives are deleted",
                  ar.target)

    rclone = Rclone(cfg.rclone)
    cfg.temp_dir.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now()
    stamp = now.strftime(TS_FORMAT)
    out_name = f"{ar.name}_{stamp}{ARCHIVE_EXT[ar.compressor]}"
    out_path = cfg.temp_dir / out_name
    stats = {"uploaded": 0, "deleted": 0, "bytes": 0}
    remote_dir = ar.target.rstrip("/")

    try:
        with task_lock(f"{cfg.project}.archive", cfg.path):
            LOG.info("archive: packing %s -> %s", ar.root, out_name)
            comp, writes_file = compress_stage(ar.compressor, ar.compressor_args,
                                               str(out_path), out_name)
            stages = [tar_command(cfg, ar.root)]
            if comp:
                comp_cmd = list(comp)
                if writes_file:
                    comp_cmd[-1] = str(out_path)
                stages.append(comp_cmd)
            run_pipeline(stages, str(out_path), last_writes_file=writes_file)
            if DRY:
                rclone.copyto(str(out_path), f"{remote_dir}/{out_name}")
                return stats

            size = out_path.stat().st_size
            if size == 0:
                raise CommandError("archive is empty", 1)
            stats["bytes"] = size
            LOG.info("archive: packed %s", fmt_bytes(size))

            upload_file(rclone, out_path, remote_dir, mode, cfg.temp_dir)
            stats["uploaded"] = 1
            LOG.info("archive: uploaded -> %s/%s%s", remote_dir, out_name,
                     " (mirror)" if mode == "sync" else "")

            if ar.retention and mode == "copy":
                files = rclone.lsf(remote_dir)
                for stale in plan_prune(files, ar.retention, now):
                    rclone.deletefile(f"{remote_dir}/{stale}")
                    stats["deleted"] += 1
                    LOG.info("archive: pruned %s", stale)
    finally:
        if not cfg.keep_temp and out_path.exists():
            out_path.unlink()
    return stats
