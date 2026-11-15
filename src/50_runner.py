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


def run(cmd, env=None, check=True, timeout=None, capture=True, banner=False):
    """Run one command.

    capture=True  - stdout/stderr are collected (needed to parse lsf output).
    capture=False - the child writes straight to our terminal: this is how
                    rclone --progress becomes visible under --verbose.
    banner=True   - put an empty line before and after the streamed output,
                    so it does not glue to the script's log lines.
    """
    if DRY:
        LOG.info("dry-run: %s", _fmt_cmd(cmd))
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    LOG.debug("run: %s", _fmt_cmd(cmd))
    if capture:
        res = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=timeout)
        stderr = res.stderr or res.stdout
    else:
        if banner:
            print(file=sys.stderr)
        res = subprocess.run(cmd, env=env, timeout=timeout)
        if banner:
            print(file=sys.stderr)
        stderr = ""
    if check and res.returncode != 0:
        raise CommandError(_fmt_cmd(cmd), res.returncode, stderr)
    return res


PROGRESS_INTERVAL = 1.0


def progress_line(verb, size, rate, percent, name):
    """'  packing 1.2 GiB, 84 MiB/s, 35% (site_...tar.zst)'."""
    parts = [fmt_bytes(size), f"{fmt_bytes(rate)}/s"]
    if percent is not None:
        parts.append(f"{percent:.0f}%")
    return f"  {verb} {', '.join(parts)} ({name})"


def _file_size(path):
    try:
        return Path(path).stat().st_size
    except OSError:
        return None


def _read_bytes(pid):
    """Bytes read by the process, from /proc/<pid>/io (Linux).

    For 'tar | zstd' this is the uncompressed tar stream, so it can be
    compared with the source size to get a percentage.
    """
    try:
        with open(f"/proc/{pid}/io", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("rchar:"):
                    return int(line.split()[1])
    except (OSError, ValueError):
        pass
    return None


def _wait_with_progress(procs, out_path, verb, total):
    """Wait for the pipeline, redrawing a progress line once a second.

    The size comes from the output file itself, so no data passes through
    Python; the percentage (debug mode only) compares the bytes the last
    stage has read with the precomputed source size.
    """
    last = procs[-1]
    name = Path(out_path).name
    shown, size = 0, 0
    previous = time.monotonic()
    while True:
        try:
            last.wait(timeout=PROGRESS_INTERVAL)
            break
        except subprocess.TimeoutExpired:
            now = time.monotonic()
            current = _file_size(out_path) or 0
            read = _read_bytes(last.pid) if total else None
            percent = min(99.0, 100.0 * read / total) if (total and read) else None
            rate = (current - size) / (now - previous) if now > previous else 0
            line = progress_line(verb, current, rate, percent, name)
            sys.stderr.write("\r" + line)
            sys.stderr.flush()
            shown, size, previous = len(line), current, now
    if shown:
        sys.stderr.write("\r" + " " * shown + "\r")
        sys.stderr.flush()
    return [p.wait() for p in procs]


def run_pipeline(stages, out_path, env=None, last_writes_file=False,
                 progress=None, total=None):
    """Run a chain of commands connected by pipes; the last one writes out_path.

    last_writes_file=True is for tools that create the archive themselves
    (rar), instead of streaming to stdout.

    progress="packing" draws a live line on the terminal (only under
    --verbose/debug and only with a tty); total is the uncompressed source
    size and enables the percentage in debug mode.
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
        if progress and VERBOSE and sys.stderr.isatty():
            codes = _wait_with_progress(procs, out_path, progress, total)
        else:
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


