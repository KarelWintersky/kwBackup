# ============================================================================
# REPORT  (--list and --verify)
# ============================================================================

def _wanted(target, from_name):
    return not from_name or target.partition(":")[0] == from_name


def action_list(cfg, args):
    now = dt.datetime.now()
    rc = EXIT_OK
    multi = len(cfg.database.databases) > 1

    if cfg.database.enabled or args.force:
        for dbname in cfg.database.databases:
            say(f"\nDATABASE {dbname}")
            for target in cfg.database.targets:
                if not _wanted(target, args.from_upstream):
                    continue
                uploader = rclone_for(cfg, target.partition(":")[0], cfg.database)
                say(f"  {target}")
                for cls in RETENTION_CLASSES:
                    retention = cfg.database.retention.get(cls)
                    remote_dir = target_join(
                        target, dbname if multi else None, cls.upper())
                    label = cls.upper().ljust(7)
                    if not retention:
                        say(f"    {label} disabled")
                        continue
                    files = owned([f for f in uploader.lsf(remote_dir) if f.ts],
                                  f"{dbname}_")
                    files.sort(key=lambda f: f.ts)
                    if not files:
                        say(f"    {label} 0 files  ⚠ empty  ({retention.describe()})")
                        rc = EXIT_VERIFY
                        continue
                    first, last = files[0].ts, files[-1].ts
                    say(f"    {label} {len(files):>3} files  "
                          f"{first:%Y-%m-%d} .. {last:%Y-%m-%d}"
                          f"  last {fmt_bytes(files[-1].size)}  ({retention.describe()})")
                    if cls == "daily":
                        age = (now.date() - last.date()).days
                        if age > 1:
                            say(f"    {'':7} ⚠ newest DAILY copy is {age} days old")
                            rc = EXIT_VERIFY
                        for gap in missing_days(files)[:5]:
                            say(f"    {'':7} ⚠ gap: {gap:%Y-%m-%d}")

    if cfg.storage.enabled or args.force:
        say(f"\nSTORAGE  ({cfg.storage.mode})")
        for target in cfg.storage.targets:
            if not _wanted(target, args.from_upstream):
                continue
            uploader = rclone_for(cfg, target.partition(":")[0], cfg.storage)
            say(f"  {target}")
            for src, dest in cfg.storage.sources:
                sub = source_dest(src, dest)
                remote_dir = target_join(target, sub)
                files = uploader.lsf(remote_dir)
                total = sum(f.size for f in files)
                newest = max((f.mtime for f in files if f.mtime), default=None)
                when = f", newest {newest:%Y-%m-%d %H:%M}" if newest else ""
                say(f"    {src}")
                say(f"      {len(files):>5} files, {fmt_bytes(total)}{when}"
                      f"  ->  {remote_dir}")
                if not files:
                    say("             ⚠ nothing in the cloud for this source")
                    rc = EXIT_VERIFY

    if cfg.archive.enabled or args.force:
        ar = cfg.archive
        say(f"\nARCHIVE  ({ar.compressor})")
        for target in ar.targets:
            if not _wanted(target, args.from_upstream):
                continue
            uploader = rclone_for(cfg, target.partition(":")[0], ar)
            remote_dir = target.rstrip("/")
            say(f"  {remote_dir}")
            files = owned([f for f in uploader.lsf(remote_dir) if f.ts],
                          f"{ar.name}_")
            files.sort(key=lambda f: f.ts)
            if not files:
                say("    0 files  ⚠ empty")
                rc = EXIT_VERIFY
            else:
                first, last = files[0].ts, files[-1].ts
                retention = ar.retention.describe() if ar.retention else "no retention"
                say(f"    {len(files):>3} files  {first:%Y-%m-%d} .. "
                      f"{last:%Y-%m-%d}  last {fmt_bytes(files[-1].size)}  ({retention})")
                for f in files[-3:]:
                    say(f"      {f.name}  {fmt_bytes(f.size)}")

    if cfg.incremental.enabled or args.force:
        inc = cfg.incremental
        say(f"\nINCREMENTAL  (day {inc.initial_day})")
        for target in inc.targets:
            if not _wanted(target, args.from_upstream):
                continue
            uploader = rclone_for(cfg, target.partition(":")[0], inc)
            remote_dir = target.rstrip("/")
            say(f"  {remote_dir}")
            groups = {}
            for chain, kind, ts, f in inc_entries(uploader.lsf(remote_dir), inc.name):
                groups.setdefault(chain, {"full": [], "inc": []})[kind].append((ts, f))
            if not groups:
                say("    no chains")
                rc = EXIT_VERIFY
                continue
            for chain in sorted(groups):
                group = groups[chain]
                fulls = sorted(ts for ts, _ in group["full"] if ts)
                incs = sorted(ts for ts, _ in group["inc"] if ts)
                total = sum(f.size for _, f in group["full"] + group["inc"])
                if not fulls:
                    say(f"    {chain}  {len(incs)} inc without a full")
                    rc = EXIT_VERIFY
                    continue
                last = max(incs + fulls)
                say(f"    {chain}  full {fulls[0]:%Y-%m-%d %H:%M}, "
                      f"{len(incs)} inc, last {last:%Y-%m-%d}, {fmt_bytes(total)}")

    if cfg.vault.enabled or args.force:
        vault = cfg.vault
        say("\nVAULT")
        for target in vault.targets:
            if not _wanted(target, args.from_upstream):
                continue
            store = vault_store(cfg, target)
            say(f"  {target}")
            names = vault_layer_names(store)
            if not names:
                say("    no layers  ⚠ empty")
                rc = EXIT_VERIFY
                continue
            chain, dates = [], []
            for name in names:
                try:
                    manifest = vault_read_layer(store, name)
                except (ActionFailed, CommandError, OSError) as exc:
                    say(f"    ✗ {exc}")
                    rc = EXIT_VERIFY
                    continue
                if manifest.get("base") and chain:
                    _vault_gap_report(dates)
                    chain, dates = [], []
                stamp = name[:-len(".manifest.json")]
                volumes = manifest.get("volumes", [])
                size = sum(volume.get("size", 0) for volume in volumes)
                count = sum(volume.get("files", 0) for volume in volumes)
                try:
                    deleted = len(vault_read_layer(
                        store, _vault_deleted_name(name)).get("files", []))
                except (ActionFailed, CommandError, OSError):
                    deleted = 0
                    rc = EXIT_VERIFY
                label = "FULL" if manifest.get("base") else "    "
                say(f"    {label} {stamp}  date {manifest.get('date', '?')}"
                      f"  {count} file(s)  {deleted} deleted"
                      f"  {len(volumes)} volume(s)  {fmt_bytes(size)}")
                dates.append(manifest.get("date"))
            _vault_gap_report(dates)

    say()
    return rc


def _vault_gap_report(dates):
    days = []
    for value in dates:
        if not value:
            continue
        try:
            day = dt.date.fromisoformat(str(value)[:10])
        except ValueError:
            continue
        if day not in days:
            days.append(day)
    if len(days) < 2:
        return
    missing = (days[-1] - days[0]).days + 1 - len(days)
    if missing > 0:
        say(f"    {'':7} ⚠ {missing} day(s) without a layer")


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
        elif kind == "incremental":
            run(["tar", "-tf", str(local)])
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
    cfg.temp_dir.mkdir(parents=True, exist_ok=True)
    rc = EXIT_OK
    checked = 0
    multi = len(cfg.database.databases) > 1

    def report(label, problem):
        nonlocal rc
        if problem:
            say(f"  ✗ {label}: {problem}")
            rc = EXIT_VERIFY
        else:
            say(f"  ✓ {label}")

    if (cfg.database.enabled or args.force) and cfg.database.databases:
        for dbname in cfg.database.databases:
            for target in cfg.database.targets:
                if not _wanted(target, args.from_upstream):
                    continue
                uploader = rclone_for(cfg, target.partition(":")[0], cfg.database)
                for cls in RETENTION_CLASSES:
                    remote_dir = target_join(
                        target, dbname if multi else None, cls.upper())
                    files = sorted(owned([f for f in uploader.lsf(remote_dir) if f.ts],
                                         f"{dbname}_"), key=lambda f: f.ts)
                    if not files:
                        continue
                    for rf in (files if args.all else files[-1:]):
                        label = f"{remote_dir}/{rf.name}"
                        LOG.info("verifying %s (%s)", label, fmt_bytes(rf.size))
                        report(label, _verify_one(uploader, remote_dir, rf,
                                                  cfg.temp_dir, "database"))
                        checked += 1

    if (cfg.archive.enabled or args.force) and cfg.archive.targets:
        for target in cfg.archive.targets:
            if not _wanted(target, args.from_upstream):
                continue
            uploader = rclone_for(cfg, target.partition(":")[0], cfg.archive)
            remote_dir = target.rstrip("/")
            files = sorted(owned([f for f in uploader.lsf(remote_dir) if f.ts],
                                 f"{cfg.archive.name}_"), key=lambda f: f.ts)
            for rf in (files if args.all else files[-1:]):
                label = f"{remote_dir}/{rf.name}"
                LOG.info("verifying %s (%s)", label, fmt_bytes(rf.size))
                report(label, _verify_one(uploader, remote_dir, rf,
                                          cfg.temp_dir, "archive"))
                checked += 1

    if (cfg.incremental.enabled or args.force) and cfg.incremental.targets:
        inc = cfg.incremental
        for target in inc.targets:
            if not _wanted(target, args.from_upstream):
                continue
            uploader = rclone_for(cfg, target.partition(":")[0], inc)
            remote_dir = target.rstrip("/")
            groups = {}
            for chain, kind, _ts, rf in inc_entries(uploader.lsf(remote_dir), inc.name):
                groups.setdefault(chain, {"full": [], "inc": []})[kind].append(rf)
            chains = sorted(groups)
            for chain in (chains if args.all else chains[-1:]):
                group = groups[chain]
                if not group["full"]:
                    continue
                fulls = sorted(group["full"], key=lambda f: f.ts or dt.datetime.min)
                incs = sorted(group["inc"], key=lambda f: f.ts or dt.datetime.min)
                for rf in [fulls[-1]] + ([incs[-1]] if incs else []):
                    label = f"{remote_dir}/{rf.name}"
                    LOG.info("verifying %s (%s)", label, fmt_bytes(rf.size))
                    report(label, _verify_one(uploader, remote_dir, rf,
                                              cfg.temp_dir, "incremental"))
                    checked += 1

    if (cfg.vault.enabled or args.force) and cfg.vault.targets:
        vault = cfg.vault
        say("\nVAULT")
        for target in vault.targets:
            if not _wanted(target, args.from_upstream):
                continue
            store = vault_store(cfg, target)
            names = vault_layer_names(store)
            if not names:
                report(f"{target}: no layers", "empty")
                continue
            manifests, broken = [], 0
            for name in names:
                try:
                    manifests.append((name, vault_read_layer(store, name)))
                except (ActionFailed, CommandError, OSError) as exc:
                    say(f"  ✗ {exc}")
                    broken += 1
            if broken:
                rc = EXIT_VERIFY
            selected = manifests if args.all else vault_latest_chain(manifests)
            if not selected:
                report(f"{target}: no base layer", "not restorable")
                continue
            sizes = store.list()
            problems, volumes = [], set()
            for name, manifest in selected:
                volume_list = manifest.get("volumes", [])
                by_name = {volume["name"]: volume for volume in volume_list}
                for volume in volume_list:
                    volumes.add(volume["name"])
                    if volume["name"] not in sizes:
                        problems.append(f"{name}: missing volume {volume['name']}")
                    elif sizes[volume["name"]] != volume.get("size"):
                        problems.append(f"{name}: size mismatch {volume['name']}")
                    if volume.get("manifest") not in sizes:
                        problems.append(f"{name}: missing manifest "
                                        f"{volume.get('manifest')}")
                try:
                    entries, _deleted = vault_read_files(store, name, manifest)
                except (ActionFailed, CommandError, OSError) as exc:
                    problems.append(f"{name}: {exc}")
                    continue
                for volume_name, entry in entries:
                    volume = by_name.get(volume_name)
                    if volume is None:
                        problems.append(f"{name}: bad volume for {entry.get('path')}")
                        continue
                    if entry.get("offset", 0) + entry.get("size", 0) > volume.get("size", 0):
                        problems.append(f"{name}: offset out of range for {entry.get('path')}")
            if problems:
                for problem in problems[:20]:
                    say(f"  ✗ {problem}")
                if len(problems) > 20:
                    say(f"  ✗ ... and {len(problems) - 20} more")
                rc = EXIT_VERIFY
            else:
                say(f"  ✓ {target}: {len(selected)} layer(s), {len(volumes)} volume(s)")
            checked += len(selected)

    if not checked:
        say("nothing to verify: no copies found")
        return EXIT_VERIFY
    return rc
