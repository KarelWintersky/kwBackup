# ============================================================================
# REPORT  (--list and --verify)
# ============================================================================

def action_list(cfg, args):
    uploader = Rclone(cfg.rclone)
    now = dt.datetime.now()
    rc = EXIT_OK

    if cfg.database.enabled or args.force:
        for dbname in cfg.database.databases:
            print(f"\nDATABASE {dbname}  ->  {cfg.database.target}")
            for cls in RETENTION_CLASSES:
                retention = cfg.database.retention.get(cls)
                remote_dir = class_path(cfg, dbname, cls)
                label = cls.upper().ljust(7)
                if not retention:
                    print(f"  {label} disabled")
                    continue
                files = owned([f for f in uploader.lsf(remote_dir) if f.ts],
                              f"{dbname}_")
                files.sort(key=lambda f: f.ts)
                if not files:
                    print(f"  {label} 0 files  ⚠ empty  ({retention.describe()})")
                    rc = EXIT_VERIFY
                    continue
                first, last = files[0].ts, files[-1].ts
                print(f"  {label} {len(files):>3} files  {first:%Y-%m-%d} .. {last:%Y-%m-%d}"
                      f"  last {fmt_bytes(files[-1].size)}  ({retention.describe()})")
                if cls == "daily":
                    age = (now.date() - last.date()).days
                    if age > 1:
                        print(f"  {'':7} ⚠ newest DAILY copy is {age} days old")
                        rc = EXIT_VERIFY
                    for gap in missing_days(files)[:5]:
                        print(f"  {'':7} ⚠ gap: {gap:%Y-%m-%d}")

    if cfg.storage.enabled or args.force:
        print(f"\nSTORAGE  ->  {cfg.storage.target}  ({cfg.storage.mode})")
        for src, dest in cfg.storage.sources:
            sub = source_dest(src, dest)
            remote_dir = cfg.storage.target.rstrip("/") + (f"/{sub}" if sub else "")
            files = uploader.lsf(remote_dir)
            total = sum(f.size for f in files)
            newest = max((f.mtime for f in files if f.mtime), default=None)
            when = f", newest {newest:%Y-%m-%d %H:%M}" if newest else ""
            print(f"  {src}")
            print(f"    {len(files):>5} files, {fmt_bytes(total)}{when}  ->  {remote_dir}")
            if not files:
                print("           ⚠ nothing in the cloud for this source")
                rc = EXIT_VERIFY

    if cfg.archive.enabled or args.force:
        ar = cfg.archive
        remote_dir = ar.target.rstrip("/")
        print(f"\nARCHIVE  ->  {remote_dir}  ({ar.compressor})")
        files = owned([f for f in uploader.lsf(remote_dir) if f.ts],
                      f"{ar.name}_")
        files.sort(key=lambda f: f.ts)
        if not files:
            print("  0 files  ⚠ empty")
            rc = EXIT_VERIFY
        else:
            first, last = files[0].ts, files[-1].ts
            retention = ar.retention.describe() if ar.retention else "no retention"
            print(f"  {len(files):>3} files  {first:%Y-%m-%d} .. {last:%Y-%m-%d}  "
                  f"last {fmt_bytes(files[-1].size)}  ({retention})")
            for f in files[-3:]:
                print(f"    {f.name}  {fmt_bytes(f.size)}")

    print()
    return rc


def _peek_decompressed(path, n=4096):
    """First n bytes of the uncompressed stream."""
    name = str(path).lower()
    if name.endswith((".zst", ".zstd")):
        cmd = ["zstd", "--quiet", "-dc", str(path)]
    elif name.endswith(".gz"):
        cmd = ["gzip", "-dc", str(path)]
    elif name.endswith(".rar"):
        return b""
    else:
        cmd = ["cat", str(path)]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        return proc.stdout.read(n) if proc.stdout else b""
    finally:
        proc.kill()
        proc.wait()


MARKERS = (b"MySQL dump", b"MariaDB dump", b"PostgreSQL database dump",
           b"SQLite format", b"BEGIN TRANSACTION", b"PRAGMA")


def _test_compression(path):
    name = str(path).lower()
    if name.endswith((".zst", ".zstd")):
        run(["zstd", "--quiet", "-t", str(path)])
    elif name.endswith(".gz"):
        run(["gzip", "-t", str(path)])
    elif name.endswith(".rar"):
        run(["rar", "t", "-inul", str(path)])


def _verify_one(uploader, remote_dir, rf, workdir, kind):
    """Download one remote file, check size, integrity and shape. Returns None
    on success or a short problem description."""
    local = workdir / rf.name
    try:
        uploader.copyto(f"{remote_dir}/{rf.name}", str(local))
        got = local.stat().st_size
        if got != rf.size:
            return f"size mismatch: got {got}, expected {rf.size}"
        _test_compression(local)
        if kind == "database":
            head = _peek_decompressed(local)
            if head and not any(m in head for m in MARKERS):
                return "decompresses, but no SQL header found"
        elif kind == "archive":
            head = _peek_decompressed(local, 1024)
            if head and not head.startswith(b"\x00") and not _tar_readable(local):
                return "archive is not a readable tar stream"
        return None
    except (CommandError, OSError) as exc:
        return str(exc)
    finally:
        if local.exists():
            local.unlink()


def _tar_readable(path):
    name = str(path).lower()
    if name.endswith((".zst", ".zstd")):
        unzip = ["zstd", "--quiet", "-dc", str(path)]
    elif name.endswith(".gz"):
        unzip = ["gzip", "-dc", str(path)]
    elif name.endswith(".rar"):
        return True          # rar was already tested by `rar t`
    else:
        unzip = ["cat", str(path)]
    reader, writer = os.pipe()
    proc_unzip = subprocess.Popen(unzip, stdout=writer, stderr=subprocess.DEVNULL)
    os.close(writer)
    proc_tar = subprocess.Popen(["tar", "-tf", "-"], stdin=reader,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.close(reader)
    proc_unzip.wait()
    return proc_tar.wait() == 0


def action_verify(cfg, args):
    uploader = Rclone(cfg.rclone)
    cfg.temp_dir.mkdir(parents=True, exist_ok=True)
    rc = EXIT_OK
    checked = 0

    def report(label, problem):
        nonlocal rc
        if problem:
            print(f"  ✗ {label}: {problem}")
            rc = EXIT_VERIFY
        else:
            print(f"  ✓ {label}")

    if (cfg.database.enabled or args.force) and (cfg.database.databases):
        for dbname in cfg.database.databases:
            for cls in RETENTION_CLASSES:
                remote_dir = class_path(cfg, dbname, cls)
                files = sorted(owned([f for f in uploader.lsf(remote_dir) if f.ts],
                                     f"{dbname}_"), key=lambda f: f.ts)
                if not files:
                    continue
                picked = files if args.all else files[-1:]
                for rf in picked:
                    label = f"{dbname} {cls.upper()}/{rf.name}"
                    LOG.info("verifying %s (%s)", label, fmt_bytes(rf.size))
                    report(label, _verify_one(uploader, remote_dir, rf, cfg.temp_dir, "database"))
                    checked += 1

    if (cfg.archive.enabled or args.force) and cfg.archive.target:
        remote_dir = cfg.archive.target.rstrip("/")
        files = sorted(owned([f for f in uploader.lsf(remote_dir) if f.ts],
                             f"{cfg.archive.name}_"), key=lambda f: f.ts)
        if files:
            for rf in (files if args.all else files[-1:]):
                label = f"archive/{rf.name}"
                LOG.info("verifying %s (%s)", label, fmt_bytes(rf.size))
                report(label, _verify_one(uploader, remote_dir, rf, cfg.temp_dir, "archive"))
                checked += 1

    if not checked:
        print("nothing to verify: no copies found")
        return EXIT_VERIFY
    return rc
