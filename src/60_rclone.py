# ============================================================================
# RCLONE
# ============================================================================

@dataclass
class RemoteFile:
    name: str
    size: int
    ts: dt.datetime | None        # parsed from the filename, used by retention
    mtime: dt.datetime | None = None   # from the remote, display only


def rclone_for(cfg, upstream, section=None):
    """An Rclone instance for one upstream.

    Precedence of S3 flags: rclone global -> upstream -> section.
    """
    up = cfg.upstreams[upstream]
    options = dict(cfg.rclone.s3_options)
    options.update(up.s3_options)
    if section is not None:
        options.update(getattr(section, "s3_options", {}) or {})
    options = {key: value for key, value in options.items() if value is not None}
    return Rclone(cfg.rclone, config=up.config,
                  args=list(cfg.rclone.args) + list(up.args),
                  env=up.env, s3_options=options, bwlimit=up.bwlimit)


class Rclone:
    def __init__(self, cfg: RcloneCfg, config=None, args=None, env=None,
                 s3_options=None, bwlimit=0):
        self.binary = cfg.binary
        self.bwlimit = bwlimit
        self.config = config
        self.args = list(cfg.args if args is None else args)
        merged_env = dict(os.environ)
        merged_env.update(cfg.env)
        if env:
            merged_env.update(env)
        self.env = merged_env
        self.s3_options = dict(s3_options or {})

    def _base(self):
        cmd = [self.binary]
        if self.config:
            cmd += ["--config", self.config]
        if VERBOSE:
            # --verbose / --debug also make rclone talk: transfer stats and
            # a progress line. Without the flag rclone stays quiet in cron.
            cmd.append("--progress")
        if self.bwlimit:
            cmd.append(f"--bwlimit={self.bwlimit}")
        cmd += list(self.args)
        # Backend flags go last: a section's s3-custom-options override both
        # rclone.args and the upstream's s3-custom-options for the same flag.
        for key, value in self.s3_options.items():
            if isinstance(value, bool):
                cmd.append(f"--{key}" if value else f"--{key}=false")
            else:
                cmd.append(f"--{key}={value}")
        return cmd

    def run(self, *args, check=True, capture=None, banner=False):
        """Rclone talks directly to the terminal when --verbose is on,
        except for commands whose output we have to parse (lsf)."""
        if capture is None:
            capture = not VERBOSE
        return run(self._base() + list(args), env=self.env, check=check,
                   capture=capture, banner=banner)

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
        return self.run("copyto", src, dst, check=True, banner=True)

    def deletefile(self, path):
        return self.run("deletefile", path, check=True)


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
        rclone.run("sync", str(staging), remote_dir, banner=True)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
