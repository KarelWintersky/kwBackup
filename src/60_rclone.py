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
        return cmd + list(self.cfg.args)

    def run(self, *args, check=True):
        return run(self._base() + list(args), env=self.env, check=check)

    def lsf(self, path):
        """List files under remote:path as RemoteFile objects (empty if missing)."""
        if DRY:
            return []
        res = self.run("lsf", "--files-only", "--format", "pst", path.rstrip("/") + "/", check=False)
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


