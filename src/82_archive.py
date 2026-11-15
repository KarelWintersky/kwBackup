# ============================================================================
# ARCHIVE  (tar stream -> compressor -> cloud, with retention)
# ============================================================================

ARCHIVE_EXT = {"zstd": ".tar.zst", "gzip": ".tar.gz", "pigz": ".tar.gz",
               "rar": ".rar", "none": ".tar"}


def _excluded(rel, patterns):
    for pattern in patterns:
        if pattern and (fnmatch.fnmatch(rel, pattern)
                        or fnmatch.fnmatch("/" + rel, pattern)
                        or fnmatch.fnmatch(os.path.basename(rel), pattern)):
            return True
    return False


def source_bytes(root, include, exclude):
    """How much tar is going to read, for the percentage in debug mode.

    Mirrors tar_args: the default include list walks the whole root, an
    explicit list is expanded with glob. --exclude patterns are matched
    approximately (fnmatch on the relative path and the basename).
    """
    root = Path(root)
    patterns = [p for p in (exclude or []) if p]
    includes = [p for p in (include or []) if p]
    if includes and includes != ["*"]:
        targets = []
        for pattern in includes:
            targets.extend(glob.glob(str(root / pattern), recursive=True))
    else:
        targets = [str(root)]

    total, seen = 0, set()
    for target in targets:
        path = Path(target)
        if path.is_file():
            rel = os.path.relpath(path, root)
            if str(path) not in seen and not _excluded(rel, patterns):
                seen.add(str(path))
                try:
                    total += path.lstat().st_size
                except OSError:
                    pass
            continue
        for dirpath, dirnames, filenames in os.walk(path):
            rel_dir = os.path.relpath(dirpath, root)
            dirnames[:] = [d for d in dirnames
                           if not _excluded(os.path.join(rel_dir, d), patterns)]
            for name in filenames:
                full = Path(dirpath) / name
                if str(full) in seen or _excluded(os.path.relpath(full, root), patterns):
                    continue
                seen.add(str(full))
                try:
                    st = full.lstat()
                except OSError:
                    continue
                if stat.S_ISREG(st.st_mode):
                    total += st.st_size
    return total


def tar_args(root, include, exclude):
    """tar to stdout; include patterns are relative to root (-C)."""
    cmd = ["tar", "-C", str(root), "-cf", "-"]
    for pattern in exclude:
        if pattern:
            cmd.append(f"--exclude={pattern}")
    includes = [p for p in include if p]
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


def tar_command(cfg, root):
    """Archive command for the archive task (same rules as tar_args)."""
    return tar_args(root, cfg.archive.include, cfg.archive.exclude)


def action_archive(cfg, args):
    ar = cfg.archive
    if not ar.enabled and not args.force:
        LOG.warning("archive backup is disabled in config (use --force to override)")
        return {"skipped": True}

    if not ar.root or not Path(ar.root).is_dir():
        raise ActionFailed(f"archive: root is not a directory: {ar.root!r}")

    mode = args.mode or ar.mode
    cfg.temp_dir.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now()
    stamp = now.strftime(TS_FORMAT)
    out_name = f"{ar.name}_{stamp}{ARCHIVE_EXT[ar.compressor]}"
    out_path = cfg.temp_dir / out_name
    stats = {"uploaded": 0, "deleted": 0, "bytes": 0}
    failures = []

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
            total = None
            if cfg.debug or getattr(args, "debug", False):
                total = source_bytes(ar.root, ar.include, ar.exclude)
            run_pipeline(stages, str(out_path), last_writes_file=writes_file,
                         progress="packing", total=total)

            if not DRY:
                size = out_path.stat().st_size
                if size == 0:
                    raise CommandError("archive is empty", 1)
                stats["bytes"] = size
                LOG.info("archive: packed %s", fmt_bytes(size))

            for target in ar.targets:
                upstream = target.partition(":")[0]
                remote_dir = target.rstrip("/")
                if mode == "sync":
                    LOG.debug("archive: -m sync: %s is mirrored, previous archives "
                              "are deleted", remote_dir)
                try:
                    rclone = rclone_for(cfg, upstream, ar)
                    upload_file(rclone, out_path, remote_dir, mode, cfg.temp_dir)
                    stats["uploaded"] += 1
                    LOG.info("archive: %s -> %s/%s%s",
                             "would upload" if DRY else "uploaded",
                             remote_dir, out_name,
                             " (mirror)" if mode == "sync" else "")
                    if ar.retention and mode == "copy":
                        files = rclone.lsf(remote_dir)
                        for stale in plan_prune(files, ar.retention, now,
                                                prefix=f"{ar.name}_"):
                            rclone.deletefile(f"{remote_dir}/{stale}")
                            stats["deleted"] += 1
                            LOG.info("archive: pruned %s", stale)
                except (CommandError, OSError) as exc:
                    LOG.error("archive: upstream %s: %s", upstream, exc)
                    failures.append(f"{upstream}: {exc}")
    finally:
        if not cfg.keep_temp and out_path.exists():
            out_path.unlink()
    if failures:
        raise ActionFailed("archive backup failed: " + "; ".join(failures))
    return stats
