# ============================================================================
# HOOKS  (per-task commands before and after the work)
#
# before: [...]  -- run in order, the first failure aborts the task
# after:  [...]  -- run always (also after a failed before), failures are
#                   warnings and do not change the exit code
# Skipped for --dry-run and for a task that is disabled without --force.
# Each command is /bin/sh -c with KWBACKUP_* in the environment.
# ============================================================================

def _hook_env(cfg, action, phase, status):
    env = dict(os.environ)
    env.update({
        "KWBACKUP_PROJECT": cfg.project,
        "KWBACKUP_ACTION": action,
        "KWBACKUP_PHASE": phase,
        "KWBACKUP_STATUS": status,
        "KWBACKUP_TASK_FILE": str(cfg.path),
    })
    return env


def _hook_output(text, limit=1000):
    text = " ".join((text or "").split())
    return text if len(text) <= limit else "..." + text[-limit:]


def run_hook(cfg, command, action, phase, status):
    timeout = max(1, cfg.hook_timeout or 60)
    LOG.debug("hook %s %s: %s", phase, action, command)
    proc = subprocess.Popen(
        ["/bin/sh", "-c", command], env=_hook_env(cfg, action, phase, status),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        errors="replace", start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)      # the whole shell group
        proc.communicate()
        raise ActionFailed(
            f"{action}: {phase} hook timed out after {timeout}s: {command}",
            [{"action": action,
              "reason": f"{phase} hook timed out after {timeout}s"}]) from None
    if out.strip():
        LOG.info("hook %s %s: %s", phase, action, _hook_output(out))
    if err.strip():
        LOG.debug("hook %s %s stderr: %s", phase, action, _hook_output(err))
    if proc.returncode != 0:
        detail = _hook_output(err or out)
        raise ActionFailed(
            f"{action}: {phase} hook failed (exit {proc.returncode}): {command}"
            + (f": {detail}" if detail else ""),
            [{"action": action,
              "reason": f"{phase} hook failed (exit {proc.returncode})"}])


def run_hooks(section, cfg, action, phase, status):
    for command in getattr(section, phase, None) or []:
        try:
            run_hook(cfg, command, action, phase, status)
        except ActionFailed as exc:
            if phase == "before":
                raise
            LOG.warning("%s", exc)


def run_action(cfg, action, args, handler):
    """Run one task with its hooks.

    A before failure aborts the task; after runs always and its failures stay
    warnings. When the lock is busy nothing was done, so after is skipped.
    """
    section = getattr(cfg, action)
    if DRY or (not section.enabled and not args.force):
        return handler(cfg, args)
    status, skip_after = "failed", False
    try:
        run_hooks(section, cfg, action, "before", "starting")
        stats = handler(cfg, args)
        status = "ok"
        return stats
    except Locked:
        skip_after = True
        raise
    finally:
        if not skip_after:
            run_hooks(section, cfg, action, "after", status)
