# ============================================================================
# RCLONE
# ============================================================================

@dataclass
class RemoteFile:
    name: str
    size: int
    ts: dt.datetime | None        # parsed from the filename, used by retention
    mtime: dt.datetime | None = None   # from the remote, display only


class Rclone:
    def __init__(self, cfg: RcloneCfg):
        self.cfg = cfg
        self.env = dict(os.environ)
        self.env.update(cfg.env)

    def _base(self):
        cmd = [self.cfg.binary]
        if self.cfg.config:
            cmd += ["--config", self.cfg.config]
        if VERBOSE:
            # --verbose / --debug also make rclone talk: transfer stats and
            # a progress line. Without the flag rclone stays quiet in cron.
            cmd.append("--progress")
        return cmd + list(self.cfg.args)

    def run(self, *args, check=True, capture=None):
        """Rclone talks directly to the terminal when --verbose is on,
        except for commands whose output we have to parse (lsf)."""
        if capture is None:
            capture = not VERBOSE
        return run(self._base() + list(args), env=self.env, check=check,
                   capture=capture)

    def lsf(self, path):
        """List files under remote:path as RemoteFile objects (empty if missing)."""
        if DRY:
            return []
        res = self.run("lsf", "--files-only", "--format", "pst",
                       path.rstrip("/") + "/", check=False, capture=True)
        if res.returncode != 0:
            if "directory not found" in (res.stderr or "").lower():
                return []
            raise CommandError(f"rclone lsf {path}", res.returncode, res.stderr)
        files = []
        for line in res.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(";")
            name = parts[0]
            size = int(parts[1]) if len(parts) > 1 and parts[1].strip() else 0
            mtime = None
            if len(parts) > 2 and parts[2].strip():
                try:
                    mtime = dt.datetime.strptime(parts[2].strip()[:19], "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    mtime = None
            files.append(RemoteFile(name=name, size=size, ts=parse_ts(name), mtime=mtime))
        return files

    def copyto(self, src, dst):
        return self.run("copyto", src, dst, check=True)

    def deletefile(self, path):
        return self.run("deletefile", path, check=True)

    def remote_path(self, *parts):
        return "/".join([self.cfg.remote.rstrip("/")] + [p.strip("/") for p in parts if p])




def upload_file(rclone, src_path, remote_dir, mode, workdir):
    """Put one finished file into remote_dir.

    copy - rclone copyto: the file is added/updated, older copies stay.
    sync - mirror the directory: the file is uploaded and EVERYTHING else in
           remote_dir is deleted. Reachable only through an explicit -m sync,
           never through the config (the validator allows `copy` only).
    """
    src_path = Path(src_path)
    if mode == "copy":
        rclone.copyto(str(src_path), f"{remote_dir}/{src_path.name}")
        return
    if DRY:
        LOG.info("dry-run: rclone sync (mirror) %s -> %s", src_path.name, remote_dir)
        return
    staging = Path(tempfile.mkdtemp(prefix=".sync-", dir=str(workdir)))
    try:
        staged = staging / src_path.name
        try:
            os.link(src_path, staged)          # cheap when temp_dir == workdir
        except OSError:
            shutil.copy2(src_path, staged)
        rclone.run("sync", str(staging), remote_dir)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
