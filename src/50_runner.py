# ============================================================================
# RUNNER
# ============================================================================

class ActionFailed(Exception):
    """An action finished with errors; the message is already user-facing."""


class CommandError(Exception):
    def __init__(self, stage, returncode, stderr=""):
        self.stage, self.returncode, self.stderr = stage, returncode, stderr
        super().__init__(f"{stage} failed with exit code {returncode}"
                         + (f": {stderr.strip()[:2000]}" if stderr.strip() else ""))


def _fmt_cmd(cmd):
    return " ".join(cmd)


def run(cmd, env=None, check=True, timeout=None):
    if DRY:
        LOG.info("dry-run: %s", _fmt_cmd(cmd))
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    LOG.debug("run: %s", _fmt_cmd(cmd))
    res = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=timeout)
    if check and res.returncode != 0:
        raise CommandError(_fmt_cmd(cmd), res.returncode, res.stderr or res.stdout)
    return res


def run_pipeline(stages, out_path, env=None, last_writes_file=False):
    """Run a chain of commands connected by pipes; the last one writes out_path.

    last_writes_file=True is for tools that create the archive themselves
    (rar), instead of streaming to stdout.
    """
    if DRY:
        for cmd in stages:
            LOG.info("dry-run: %s", _fmt_cmd(cmd))
        LOG.info("dry-run: output -> %s", out_path)
        return
    err_files, procs, out_fh, prev = [], [], None, None
    try:
        for i, cmd in enumerate(stages):
            last = i == len(stages) - 1
            ef = tempfile.TemporaryFile()
            err_files.append(ef)
            if last and not last_writes_file:
                out_fh = open(out_path, "wb")
                stdout = out_fh
            elif last:
                stdout = subprocess.DEVNULL
            else:
                stdout = subprocess.PIPE
            LOG.debug("run: %s", _fmt_cmd(cmd))
            proc = subprocess.Popen(
                cmd, stdin=prev if prev is not None else subprocess.DEVNULL,
                stdout=stdout, stderr=ef, env=env,
            )
            if prev is not None:
                prev.close()
            prev = None if last else proc.stdout
            procs.append(proc)
        codes = [p.wait() for p in procs]
    finally:
        if out_fh:
            out_fh.close()
        for ef in err_files:
            ef.close()
    for i, (code, cmd) in enumerate(zip(codes, stages)):
        if code != 0:
            err_files_off = None
            raise CommandError(_fmt_cmd(cmd), code, "")
    return


