# ============================================================================
# LOCK
# ============================================================================

class Locked(Exception):
    pass


def lock_dir():
    candidates = [Path("/run/kwbackup"), Path(tempfile.gettempdir()) / f"kwbackup-{os.getuid()}"]
    for cand in candidates:
        try:
            cand.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue
        if os.access(cand, os.W_OK | os.X_OK):
            return cand
    raise Locked("no writable directory for lock files")


@contextlib.contextmanager
def task_lock(name, config_path):
    """flock-based lock: the kernel releases it if the process dies."""
    path = lock_dir() / f"{name}.lock"
    fh = open(path, "a+", encoding="utf-8")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.seek(0)
        holder = fh.read().strip()
        fh.close()
        raise Locked(f"{name}: already running" + (f" ({holder})" if holder else "")) from None
    try:
        fh.seek(0)
        fh.truncate()
        fh.write(f"pid={os.getpid()} since={dt.datetime.now():%Y-%m-%d %H:%M:%S} "
                 f"config={config_path}\n")
        fh.flush()
        yield path
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


