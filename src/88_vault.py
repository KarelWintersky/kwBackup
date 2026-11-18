# ============================================================================
# VAULT  (versioned file store: TAR layers + per-volume manifests + journal)
#
# One layer:
#   <utc>.manifest.json                 top: base, date, volumes
#   <utc>-part-001.tar                  changed files, whole files only
#   <utc>-part-001.manifest.json        files of that volume (+ offsets)
#   <utc>-deleted.manifest.json         deleted paths (always present)
#
# The top manifest is the completion marker: it is uploaded last. The journal
# is only a hint to avoid a daily full scan; every path is still stat-compared
# with the local index (state_dir/index.sqlite), so replayed events are safe.
# ============================================================================

VAULT_TS_FORMAT = "%Y-%m-%dT%H%M%SZ"
VAULT_LAYER_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{6}Z\.manifest\.json$")
VAULT_DELETED_SUFFIX = "-deleted.manifest.json"


def vault_snapshot_date(now=None):
    """A 02:00 run represents yesterday's state; a daytime run - today's."""
    now = now or dt.datetime.now()
    return (now - dt.timedelta(hours=12)).date()


def vault_ts(now=None, offset=0):
    now = (now or dt.datetime.now(dt.timezone.utc)) + dt.timedelta(seconds=offset)
    return now.strftime(VAULT_TS_FORMAT)


# ---------------------------------------------------------------- index
class VaultIndex:
    """Local cache of the last known state; the manifests stay authoritative."""

    def __init__(self, state_dir):
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "index.sqlite"
        self.db = sqlite3.connect(self.path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS files (
            path TEXT PRIMARY KEY,
            size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            mode INTEGER NOT NULL,
            uid INTEGER NOT NULL,
            gid INTEGER NOT NULL,
            layer TEXT NOT NULL)""")

    def close(self):
        self.db.close()

    def empty(self):
        return self.db.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0

    def get(self, path):
        return self.db.execute(
            "SELECT size, mtime_ns, mode, uid, gid FROM files WHERE path = ?",
            (path,)).fetchone()

    def all_paths(self):
        return {row[0] for row in self.db.execute("SELECT path FROM files")}

    def clear(self):
        with self.db:
            self.db.execute("DELETE FROM files")

    def upsert(self, entries, layer):
        with self.db:
            for entry in entries:
                self.db.execute(
                    "INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?)",
                    (entry["path"], entry["size"], entry["mtime_ns"],
                     entry["mode"], entry["uid"], entry["gid"], layer))

    def delete_paths(self, paths):
        with self.db:
            for path in paths:
                self.db.execute("DELETE FROM files WHERE path = ?", (path,))

    def apply(self, changes, deleted, layer):
        with self.db:
            for path, st in changes.items():
                self.db.execute(
                    "INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?)",
                    (path, st.st_size, st.st_mtime_ns, st.st_mode,
                     st.st_uid, st.st_gid, layer))
            for path in deleted:
                self.db.execute("DELETE FROM files WHERE path = ?", (path,))

    def replace_all(self, entries, layer):
        with self.db:
            self.db.execute("DELETE FROM files")
            self.db.executemany(
                "INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?)",
                [(path, st.st_size, st.st_mtime_ns, st.st_mode,
                  st.st_uid, st.st_gid, layer) for path, st in entries.items()])


def vault_excluded(path, patterns):
    """fnmatch against the relative path, the basename and every parent."""
    path = str(path).replace(os.sep, "/").strip("/")
    if not path or path == ".":
        return False
    parts = [part for part in path.split("/") if part]
    candidates = {path, parts[-1]}
    for index in range(1, len(parts)):
        candidates.add("/".join(parts[:index]))
        candidates.add(parts[index - 1])
    for pattern in patterns or ():
        pattern = str(pattern).replace(os.sep, "/").strip("/")
        if pattern and any(fnmatch.fnmatch(candidate, pattern)
                           for candidate in candidates):
            return True
    return False


def vault_scan(root, warnings, exclude=()):
    """relative path -> os.stat_result for regular files (symlinks skipped)."""
    root = Path(root)
    entries, links = {}, 0
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as items:
                for item in items:
                    rel = str(Path(item.path).relative_to(root))
                    try:
                        if item.is_dir(follow_symlinks=False):
                            if not vault_excluded(rel, exclude):
                                stack.append(Path(item.path))
                        elif item.is_file(follow_symlinks=False):
                            if not vault_excluded(rel, exclude):
                                entries[rel] = item.stat(follow_symlinks=False)
                        elif item.is_symlink() and not vault_excluded(rel, exclude):
                            links += 1
                    except OSError as exc:
                        warnings.append(f"{item.path}: {exc.strerror or exc}")
        except OSError as exc:
            warnings.append(f"{current}: {exc.strerror or exc}")
    if links:
        warnings.append(f"vault: {links} symlink(s) skipped (only regular files)")
    return entries


def _vault_changed(index, path, st):
    row = index.get(path)
    return row is None or (row[0], row[1], row[2], row[3], row[4]) != (
        st.st_size, st.st_mtime_ns, st.st_mode, st.st_uid, st.st_gid)


def vault_diff_scan(root, index, warnings, exclude=()):
    entries = vault_scan(root, warnings, exclude)
    changes, modified, same = {}, set(), 0
    for path, st in entries.items():
        if _vault_changed(index, path, st):
            changes[path] = st
            if index.get(path) is not None:
                modified.add(path)
        else:
            same += 1
    deleted = []
    for path in index.all_paths():
        if path in entries:
            continue
        row = index.get(path)
        deleted.append({"path": path, "size": row[0] if row else 0})
    return changes, deleted, modified, same


def vault_changes_from_events(cfg, events, index, warnings):
    """Stat every path from the journal; the event type is only a hint."""
    changes, deleted, modified, missing = {}, [], set(), 0
    root = Path(cfg.vault.root)
    for path in dict.fromkeys(event.path for event in events):
        if vault_excluded(path, cfg.vault.exclude):
            continue
        full = root / path
        try:
            st = full.lstat()
        except FileNotFoundError:
            row = index.get(path)
            if row is not None:
                deleted.append({"path": path, "size": row[0]})
            continue
        except OSError as exc:
            warnings.append(f"{path}: {exc.strerror or exc}")
            continue
        if not stat.S_ISREG(st.st_mode):
            continue
        if _vault_changed(index, path, st):
            changes[path] = st
            if index.get(path) is not None:
                modified.add(path)
        else:
            missing += 1
    return changes, deleted, modified, missing


# ---------------------------------------------------------------- layers
def _vault_volumes(changes, volume_size):
    groups, current, size = [], [], 0
    for path in sorted(changes):
        file_size = changes[path].st_size
        if current and size + file_size > volume_size:
            groups.append(current)
            current, size = [], 0
        current.append(path)
        size += file_size
    if current or not groups:
        groups.append(current)
    return groups


def _vault_tar(volume, root, paths, sizes, show_files):
    """Build one TAR volume; tar writes to stdout, run_pipeline to the file."""
    list_file = None
    if paths:
        list_file = Path(str(volume) + ".list")
        list_file.write_bytes(
            b"\0".join(path.encode("utf-8", "surrogateescape")
                       for path in paths) + b"\0")
        cmd = ["tar", "-C", str(root), "-cf", "-", "--null",
               "--no-recursion", "--verbatim-files-from", "-T", str(list_file)]
    else:
        cmd = ["tar", "-C", str(root), "-cf", "-", "-T", "/dev/null"]
    try:
        run_pipeline([cmd], str(volume), progress="packing",
                     progress_files=sizes, show_files=show_files)
    finally:
        if list_file is not None:
            list_file.unlink(missing_ok=True)


def _vault_volume_index(volume):
    """member name -> (offset, size) from the TAR headers."""
    out = {}
    with tarfile.open(volume, "r:") as archive:
        for member in archive:
            if member.isfile():
                out[member.name] = (member.offset_data, member.size)
    return out


def _vault_sha256(path, block=1024 * 1024):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(block)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _vault_json(data, pretty=False):
    if pretty:
        return json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _vault_deleted_name(layer_name):
    return layer_name[:-len(".manifest.json")] + VAULT_DELETED_SUFFIX


def vault_build_layer(cfg, changes, deleted, source_date, base=False,
                      modified=None, stamp=None, show_files=False,
                      pretty=False):
    """Create TAR volumes and manifests in cfg.temp_dir; return files."""
    vault = cfg.vault
    workdir = Path(tempfile.mkdtemp(prefix="vault-", dir=str(cfg.temp_dir)))
    stamp = stamp or vault_ts()
    modified = modified or set()
    groups = _vault_volumes(changes, vault.volume_size)
    volumes, upload = [], []
    try:
        for number, paths in enumerate(groups, 1):
            tar_name = f"{stamp}-part-{number:03d}.tar"
            manifest_name = f"{stamp}-part-{number:03d}.manifest.json"
            volume = workdir / tar_name
            sizes = [(path, changes[path].st_size) for path in paths]
            LOG.info("vault: packing volume %d/%d (%s, %d file(s))",
                     number, len(groups),
                     fmt_bytes(sum(size for _, size in sizes)), len(paths))
            _vault_tar(volume, vault.root, paths, sizes, show_files)
            locations = _vault_volume_index(volume)
            files = []
            for path in paths:
                offset, size = locations[path]
                entry = {
                    "status": "MODIFIED" if path in modified else "ADDED",
                    "path": path,
                    "size": size,
                    "mtime_ns": changes[path].st_mtime_ns,
                    "mode": changes[path].st_mode,
                    "uid": changes[path].st_uid,
                    "gid": changes[path].st_gid,
                    "offset": offset,
                }
                if vault.checksum == "sha256":
                    entry["sha256"] = _vault_sha256(Path(vault.root) / path)
                files.append(entry)
            manifest_path = workdir / manifest_name
            manifest_path.write_text(_vault_json({"files": files}, pretty),
                                     encoding="utf-8")
            volumes.append({"name": tar_name, "manifest": manifest_name,
                            "size": volume.stat().st_size, "files": len(files)})
            upload += [volume, manifest_path]

        deleted_entries = [{"status": "DELETED", "path": item["path"],
                            "size": item.get("size", 0)} for item in deleted]
        deleted_path = workdir / f"{stamp}{VAULT_DELETED_SUFFIX}"
        deleted_path.write_text(
            _vault_json({"files": deleted_entries}, pretty), encoding="utf-8")

        manifest = {"base": bool(base), "created_utc": stamp,
                    "date": str(source_date), "volumes": volumes}
        manifest_path = workdir / f"{stamp}.manifest.json"
        manifest_path.write_text(_vault_json(manifest, pretty), encoding="utf-8")
        # The top manifest goes last: it is the completion marker.
        upload += [deleted_path, manifest_path]
        return workdir, upload, manifest
    except Exception:
        shutil.rmtree(workdir, ignore_errors=True)
        raise


# ---------------------------------------------------------------- stores
class VaultStore:
    name = "store"

    def __init__(self, path):
        self.path = path.rstrip("/")

    def put(self, files):
        raise NotImplementedError

    def list(self):
        raise NotImplementedError

    def get(self, name, local):
        raise NotImplementedError

    def delete(self, name):
        raise NotImplementedError


class RcloneStore(VaultStore):
    name = "rclone"

    def __init__(self, cfg, upstream, path):
        super().__init__(path)
        self.upstream = upstream
        self.remote = f"{upstream}:{self.path}"
        self.rclone = rclone_for(cfg, upstream, cfg.vault)

    def put(self, files):
        if DRY:
            for path in files:
                LOG.info("dry-run: rclone copyto %s -> %s/%s", path,
                         self.remote, Path(path).name)
            return
        for path in files:
            self.rclone.copyto(str(path), f"{self.remote}/{Path(path).name}")

    def list(self):
        return {file.name: file.size
                for file in self.rclone.lsf(self.remote, force=True)}

    def get(self, name, local):
        self.rclone.run("copyto", f"{self.remote}/{name}", str(local),
                        check=True, force=True)

    def delete(self, name):
        self.rclone.run("deletefile", f"{self.remote}/{name}", check=False)


class RsyncStore(VaultStore):
    name = "rsync"

    def __init__(self, upstream, path):
        super().__init__(path)
        self.upstream = upstream
        self.host = upstream.host
        self.remote = f"{upstream.host}:{self.path}"
        self.options = []
        if upstream.ssh_options:
            self.options += ["-e", "ssh " + " ".join(
                shlex.quote(part) for part in upstream.ssh_options)]
        if upstream.bwlimit:
            self.options.append(f"--bwlimit={max(1, upstream.bwlimit // 1024)}")

    def _ssh(self, command):
        run(["ssh"] + list(self.upstream.ssh_options) + [self.host, command])

    def put(self, files):
        if DRY:
            for path in files:
                LOG.info("dry-run: rsync %s -> %s/%s", path, self.remote,
                         Path(path).name)
            return
        self._ssh(f"mkdir -p -- {shlex.quote(self.path)}")
        run(["rsync", "-a"] + self.options + [str(path) for path in files]
            + [self.remote + "/"])

    def list(self):
        res = run(["rsync", "--list-only"] + self.options + [self.remote + "/"],
                  check=False, capture=True, force=True)
        if res.returncode not in (0, 23, 24):
            raise CommandError(f"rsync --list-only {self.remote}",
                               res.returncode, res.stderr)
        out = {}
        for line in res.stdout.splitlines():
            parts = line.split(None, 4)
            if len(parts) == 5 and parts[0].startswith("-"):
                try:
                    out[parts[4]] = int(parts[1])
                except ValueError:
                    continue
        return out

    def get(self, name, local):
        Path(local).parent.mkdir(parents=True, exist_ok=True)
        run(["rsync", "-a"] + self.options + [f"{self.remote}/{name}", str(local)],
            force=True)

    def delete(self, name):
        self._ssh(f"rm -f -- {shlex.quote(self.path + '/' + name)}")


class LocalStore(VaultStore):
    """Internal transport for tests; not exposed in the config yet."""

    name = "local"

    def __init__(self, path):
        super().__init__(path)
        self.base = Path(path)
        self.base.mkdir(parents=True, exist_ok=True)

    def put(self, files):
        for path in files:
            shutil.copy2(path, self.base / Path(path).name)

    def list(self):
        return {path.name: path.stat().st_size for path in self.base.iterdir()
                if path.is_file()}

    def get(self, name, local):
        Path(local).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.base / name, local)

    def delete(self, name):
        (self.base / name).unlink(missing_ok=True)


def vault_store(cfg, target):
    name, _, path = target.partition(":")
    upstream = cfg.upstreams[name]
    if upstream.type == "rclone":
        return RcloneStore(cfg, name, path)
    if upstream.type == "rsync":
        return RsyncStore(upstream, path)
    raise ActionFailed(f"vault: unsupported upstream type {upstream.type!r}")


# ---------------------------------------------------------------- manifests
def _vault_read_manifest(store, name):
    with tempfile.TemporaryDirectory(prefix="vault-manifest-") as tmp:
        local = Path(tmp) / name
        store.get(name, local)
        try:
            return json.loads(local.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ActionFailed(f"vault: bad manifest {name}: {exc}") from None


def vault_layer_names(store):
    return sorted(name for name in store.list() if VAULT_LAYER_RE.match(name))


def vault_read_layer(store, name):
    return _vault_read_manifest(store, name)


def vault_read_files(store, name, manifest):
    """[(volume name, entry)] plus deleted entries of one layer."""
    entries = []
    for volume in manifest.get("volumes", []):
        volume_name = volume.get("manifest")
        if not volume_name:
            volume_name = volume["name"][:-len(".tar")] + ".manifest.json"
        volume_manifest = _vault_read_manifest(store, volume_name)
        for entry in volume_manifest.get("files", []):
            entries.append((volume["name"], entry))
    deleted = _vault_read_manifest(store, _vault_deleted_name(name)).get("files", [])
    return entries, deleted


def vault_latest_chain(manifests):
    last_base = None
    for index, (_name, manifest) in enumerate(manifests):
        if manifest.get("base"):
            last_base = index
    return manifests[last_base:] if last_base is not None else []


# ---------------------------------------------------------------- action
def _vault_stats(changes, deleted):
    return {"layers": 1, "files": len(changes), "deleted": len(deleted),
            "bytes": sum(st.st_size for st in changes.values())}


def _vault_active_names(stores):
    names = set()
    for store in stores:
        names.update(store.list())
    return names


def _vault_stamp(names, offset=0):
    base = dt.datetime.now(dt.timezone.utc)
    for step in range(3600):
        stamp = (base + dt.timedelta(seconds=offset + step)).strftime(
            VAULT_TS_FORMAT)
        if not any(name.startswith(stamp + ".") for name in names):
            names.add(f"{stamp}.manifest.json")
            return stamp
    raise ActionFailed("vault: cannot allocate a unique layer timestamp")


def _vault_rebuild_index(stores, index, warnings):
    for store in stores:
        try:
            names = vault_layer_names(store)
        except (CommandError, OSError) as exc:
            warnings.append(f"vault: cannot list {store.name} {store.path}: {exc}")
            continue
        if not names:
            continue
        LOG.info("vault: rebuilding index from %d manifest(s) on %s %s",
                 len(names), store.name, store.path)
        for name in names:
            manifest = vault_read_layer(store, name)
            if manifest.get("base"):
                index.clear()
            entries, deleted = vault_read_files(store, name, manifest)
            index.upsert([entry for _volume, entry in entries],
                         manifest.get("date") or name)
            index.delete_paths([item["path"] for item in deleted])
        return True
    return False


def _vault_upload(stores, files):
    failures, details = [], []
    for store in stores:
        try:
            store.put(files)
        except (CommandError, OSError) as exc:
            reason = failure_reason(exc)
            failures.append(f"{store.name} {store.path}: {reason}")
            details.append({"action": "vault", "entity": store.name,
                            "reason": reason})
    if failures:
        # The top manifest is written last: remove every manifest from all
        # targets so no chain is left half-published. Volumes become orphans.
        manifests = [Path(name).name for name in files
                     if Path(name).name.endswith(".manifest.json")]
        for store in stores:
            for name in manifests:
                try:
                    store.delete(name)
                except (CommandError, OSError) as exc:
                    LOG.warning("vault: cannot roll back %s on %s: %s",
                                name, store.name, exc)
        raise ActionFailed("vault upload failed: " + "; ".join(failures), details)


def _vault_commit(cfg, stores, index, changes, deleted, modified,
                  source_date, base=False, stamp=None, show_files=False,
                  pretty=False):
    workdir, files, _manifest = vault_build_layer(
        cfg, changes, deleted, source_date, base=base,
        modified=modified, stamp=stamp, show_files=show_files, pretty=pretty)
    try:
        _vault_upload(stores, files)
        if base:
            index.replace_all(changes, str(source_date))
        else:
            index.apply(changes, [item["path"] for item in deleted],
                        str(source_date))
    finally:
        if cfg.keep_temp:
            LOG.info("vault: keeping %s", workdir)
        else:
            shutil.rmtree(workdir, ignore_errors=True)
    return _vault_stats(changes, deleted)


def _vault_finish_journals(cfg):
    journal = cfg.vault.journal
    if not journal or journal.type != "txt":
        return
    today = dt.date.today()
    for _day, path in pending_journals(journal.path, today):
        mark_done(path)
    cleanup_done(journal.path, journal.keep_done, today)


def vault_path_problems(cfg):
    """Paths that would be backed up together with the index/journal itself."""
    vault = cfg.vault
    if not vault.root:
        return []
    try:
        root = Path(vault.root).resolve()
    except OSError:
        return []
    checks = [("STATE_DIR", vault.state_dir), ("TEMP_DIR", str(cfg.temp_dir))]
    if vault.journal and vault.journal.type == "txt":
        checks.append(("JOURNAL_PATH", vault.journal.path))
    problems, seen = [], set()
    for label, value in checks:
        if not value:
            continue
        try:
            path = Path(value).resolve()
        except OSError:
            continue
        if path == root or root not in path.parents:
            continue
        if path in seen:
            continue
        seen.add(path)
        try:
            rel = str(path.relative_to(root))
        except ValueError:
            continue
        if vault_excluded(rel, vault.exclude):
            continue
        problems.append(f"{label} {value} IS INSIDE VAULT.ROOT AND IS NOT "
                        f"EXCLUDED; ADD IT TO vault.exclude")
    return problems


def action_vault(cfg, args):
    vault = cfg.vault
    if not vault.enabled and not args.force:
        LOG.warning("vault is disabled in config (use --force to override)")
        return {"skipped": True}
    root = Path(vault.root or "")
    if not root.is_dir():
        raise ActionFailed(f"vault: root is not a directory: {vault.root!r}")
    stats = {"layers": 0, "files": 0, "deleted": 0, "bytes": 0}
    if DRY:
        LOG.info("dry-run: vault would process %s -> %s",
                 vault.root, ", ".join(vault.targets))
        return stats
    cfg.temp_dir.mkdir(parents=True, exist_ok=True)
    state_dir = Path(vault.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    stores = [vault_store(cfg, target) for target in vault.targets]
    warnings = []
    debug = bool(cfg.debug or getattr(args, "debug", False))
    show_files = debug
    index = VaultIndex(state_dir)
    try:
        names = _vault_active_names(stores)
        has_layers = any(VAULT_LAYER_RE.match(name) for name in names)
        if index.empty() and has_layers and not args.full:
            _vault_rebuild_index(stores, index, warnings)

        if args.full or (index.empty() and not has_layers):
            source = vault_snapshot_date()
            changes = vault_scan(root, warnings, vault.exclude)
            stats = _vault_commit(cfg, stores, index, changes, [], set(),
                                  source, base=True, stamp=_vault_stamp(names),
                                  show_files=show_files, pretty=debug)
            _vault_finish_journals(cfg)
            LOG.info("vault: base layer %s (%d file(s))", source, len(changes))
        elif args.reconcile:
            source = vault_snapshot_date()
            changes, deleted, modified, same = vault_diff_scan(
                root, index, warnings, vault.exclude)
            stats = _vault_commit(cfg, stores, index, changes, deleted,
                                  modified, source, stamp=_vault_stamp(names),
                                  show_files=show_files, pretty=debug)
            _vault_finish_journals(cfg)
            LOG.info("vault: reconcile %s: %d changed, %d deleted, %d same",
                     source, len(changes), len(deleted), same)
        elif vault.journal and vault.journal.type == "txt":
            journal = vault.journal
            if not Path(journal.path).is_dir():
                raise ActionFailed("vault: journal path is not a directory: "
                                   f"{journal.path!r} (use journal.type: scan)")
            today = dt.date.today()
            pending = pending_journals(journal.path, today)
            days = {day: path for day, path in pending}
            yesterday = today - dt.timedelta(days=1)
            if yesterday.isoformat() not in days:
                days[yesterday.isoformat()] = None
            totals = {"layers": 0, "files": 0, "deleted": 0, "bytes": 0}
            for offset, day in enumerate(sorted(days)):
                path = days[day]
                events = read_journal(path, warnings) if path else []
                changes, deleted, modified, same = vault_changes_from_events(
                    cfg, events, index, warnings)
                layer = _vault_commit(
                    cfg, stores, index, changes, deleted, modified, day,
                    stamp=_vault_stamp(names, offset=offset),
                    show_files=show_files, pretty=debug)
                for key in totals:
                    totals[key] += layer.get(key, 0)
                if path is not None:
                    mark_done(path)
                LOG.info("vault: %s: %d changed, %d deleted, %d unchanged",
                         day, len(changes), len(deleted), same)
            stats = totals
            cleanup_done(journal.path, journal.keep_done, today)
        else:
            source = vault_snapshot_date()
            changes, deleted, modified, same = vault_diff_scan(
                root, index, warnings, vault.exclude)
            stats = _vault_commit(cfg, stores, index, changes, deleted,
                                  modified, source, stamp=_vault_stamp(names),
                                  show_files=show_files, pretty=debug)
            _vault_finish_journals(cfg)
            LOG.info("vault: scan %s: %d changed, %d deleted, %d same",
                     source, len(changes), len(deleted), same)
    finally:
        index.close()
        for warning in warnings:
            LOG.warning("vault: %s", warning)
    return stats


# ---------------------------------------------------------------- restore
def _vault_restore_target(cfg, name):
    if name:
        for target in cfg.vault.targets:
            if target.partition(":")[0] == name:
                return target
        raise ActionFailed(f"vault restore: unknown target {name!r} "
                           f"(configured: {', '.join(cfg.vault.targets)})")
    return cfg.vault.targets[0]


def _vault_restore_safe(path):
    return bool(path) and not path.startswith("/") and ".." not in path.split("/")


def _vault_manifest_date(name, manifest):
    return manifest.get("date") or name[:10]


def vault_restore(cfg, args):
    destination = Path(args.to)
    target = _vault_restore_target(cfg, args.from_upstream)
    store = vault_store(cfg, target)
    names = vault_layer_names(store)
    if not names:
        raise ActionFailed(f"vault restore: no manifests found in {target}")

    at = (args.at or "").strip()
    if at:
        if not re.match(r"^\d{4}-\d{2}-\d{2}", at):
            raise ActionFailed(f"vault restore: bad --at {at!r}, expected "
                               "YYYY-MM-DD")
        at = at[:10]

    state, layers, seen_base = {}, [], False
    for name in names:
        manifest = vault_read_layer(store, name)
        date = _vault_manifest_date(name, manifest)
        if at and date > at:
            continue
        if manifest.get("base"):
            state.clear()
            seen_base = True
        elif not seen_base:
            continue
        entries, deleted = vault_read_files(store, name, manifest)
        for volume, entry in entries:
            state[entry["path"]] = (name, volume, entry)
        for item in deleted:
            state.pop(item["path"], None)
        layers.append((name, manifest))
    if not seen_base:
        raise ActionFailed("vault restore: no base layer found"
                           + (f" before {at}" if at else ""))

    stats = {"files": len(state),
             "bytes": sum(item["size"] for _, _, item in state.values())}
    if DRY:
        LOG.info("dry-run: vault restore %d file(s) from %s", len(state), target)
        return stats
    stats["files"] = 0

    destination.mkdir(parents=True, exist_ok=True)
    cfg.temp_dir.mkdir(parents=True, exist_ok=True)
    order = {name: index for index, (name, _manifest) in enumerate(layers)}
    priority = {name: index for index, name in
                enumerate(cfg.vault.restore.priority)}
    descending = cfg.vault.restore.order == "desc"

    groups = {}
    for path, (name, volume, entry) in state.items():
        if not _vault_restore_safe(path):
            raise ActionFailed(f"vault restore: unsafe path in manifest: {path!r}")
        groups.setdefault((name, volume), []).append(entry)

    def group_key(item):
        (layer, _volume), entries = item
        rank = min((priority.get(entry["path"].split("/")[0], len(priority))
                    for entry in entries), default=len(priority))
        index = order.get(layer, len(order))
        return ((len(order) - index) if descending else index, rank)

    workdir = Path(tempfile.mkdtemp(prefix="vault-restore-", dir=str(cfg.temp_dir)))
    try:
        for (layer, volume), entries in sorted(groups.items(), key=group_key):
            local = workdir / volume
            store.get(volume, local)
            members = [entry["path"] for entry in entries]
            list_file = workdir / (volume + ".list")
            list_file.write_bytes(
                b"\0".join(path.encode("utf-8", "surrogateescape")
                           for path in members) + b"\0")
            try:
                run(["tar", "-x", "-p", "-f", str(local), "-C", str(destination),
                     "--null", "--verbatim-files-from", "-T", str(list_file)])
            finally:
                list_file.unlink(missing_ok=True)
                local.unlink(missing_ok=True)
            for entry in entries:
                path = destination / entry["path"]
                if not path.exists():
                    LOG.warning("vault restore: %s was not extracted", path)
                    continue
                try:
                    os.chmod(path, stat.S_IMODE(entry["mode"]))
                    if cfg.vault.restore.chown and os.geteuid() == 0:
                        os.chown(path, entry["uid"], entry["gid"])
                    os.utime(path, ns=(entry["mtime_ns"], entry["mtime_ns"]))
                except OSError as exc:
                    LOG.warning("vault restore: metadata %s: %s", path,
                                exc.strerror or exc)
                stats["files"] += 1
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return stats
