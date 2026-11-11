# ============================================================================
# RETENTION
# ============================================================================

TS_RE = re.compile(r"_(\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2})(?:\.|$)")


def parse_ts(name):
    m = TS_RE.search(name)
    if not m:
        return None
    try:
        return dt.datetime.strptime(m.group(1), TS_FORMAT)
    except ValueError:
        return None


def plan_prune(files, retention, now=None):
    """Return names to delete. Files without a parseable timestamp are never
    touched: they were not created by us as far as we can tell."""
    now = now or dt.datetime.now()
    dated = [(f.ts, f.name) for f in files if f.ts]
    dated.sort(key=lambda item: item[0], reverse=True)
    if retention.keep is not None:
        return [name for _, name in dated[retention.keep:]]
    cutoff = now - retention.max_age
    return [name for ts, name in dated if ts < cutoff]


def period_covered(files, kind, now=None):
    """Is there already a copy for the current week/month?"""
    now = now or dt.datetime.now()
    for f in files:
        if not f.ts:
            continue
        if kind == "week" and f.ts.isocalendar()[:2] == now.isocalendar()[:2]:
            return True
        if kind == "month" and (f.ts.year, f.ts.month) == (now.year, now.month):
            return True
    return False


def class_due(cls, files, catch_up, now=None):
    """Should this class get a copy on this run?"""
    now = now or dt.datetime.now()
    if cls == "daily":
        return True
    kind = "week" if cls == "weekly" else "month"
    if catch_up:
        return not period_covered(files, kind, now)
    if cls == "weekly":
        return now.isoweekday() == 1
    return now.day == 1


def missing_days(files, now=None):
    """Days without a DAILY copy between the oldest and newest one."""
    days = sorted({f.ts.date() for f in files if f.ts})
    if len(days) < 2:
        return []
    gaps = []
    day = days[0]
    known = set(days)
    while day <= days[-1]:
        if day not in known:
            gaps.append(day)
        day += dt.timedelta(days=1)
    return gaps


