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


def owned(files, prefix):
    """Only files that belong to this config: named <prefix><timestamp>..."""
    if not prefix:
        return list(files)
    return [f for f in files if f.name.startswith(prefix)]


def plan_prune(files, retention, now=None, prefix=None):
    """Return names to delete. Files without a parseable timestamp are never
    touched, and with `prefix` only our own files are considered: a shared
    target directory must not lose another project's archives."""
    now = now or dt.datetime.now()
    files = owned(files, prefix)
    dated = [(f.ts, f.name) for f in files if f.ts]
    dated.sort(key=lambda item: item[0], reverse=True)
    if retention.keep is not None:
        return [name for _, name in dated[retention.keep:]]
    cutoff = now - retention.max_age
    return [name for ts, name in dated if ts < cutoff]


CHAIN_RE = re.compile(
    r"^(?P<base>.+)_(?P<chain>\d{4}-\d{2})_(?P<kind>full|inc)_"
    r"(?P<ts>\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2})\.tar$")


def chain_month(now, initial_day):
    """The chain a run belongs to: current month from initial_day, else previous."""
    if now.day >= initial_day:
        return f"{now:%Y-%m}"
    previous = now.replace(day=1) - dt.timedelta(days=1)
    return f"{previous:%Y-%m}"


def inc_entries(files, base):
    """[(chain, kind, ts, RemoteFile)] for files named <base>_<YYYY-MM>_<full|inc>_<ts>.tar"""
    out = []
    for f in files:
        match = CHAIN_RE.match(f.name)
        if match and match.group("base") == base:
            out.append((match.group("chain"), match.group("kind"), f.ts, f))
    return out


def plan_chain_prune(files, retention, base, now=None):
    """Delete whole chains, never a single increment. Returns file names."""
    now = now or dt.datetime.now()
    entries = inc_entries(files, base)
    groups = {}
    for chain, _kind, ts, f in entries:
        groups.setdefault(chain, []).append((ts, f.name))
    if not groups:
        return []

    def chain_time(chain):
        stamps = [ts for ts, _ in groups[chain] if ts]
        if stamps:
            return max(stamps)
        return dt.datetime.strptime(chain + "-01", "%Y-%m-%d")

    ordered = sorted(groups, key=chain_time, reverse=True)
    if retention.keep is not None:
        dropped = ordered[retention.keep:]
    else:
        cutoff = now - retention.max_age
        dropped = [chain for chain in ordered if chain_time(chain) < cutoff]
    return [name for chain in dropped for _, name in groups[chain]]


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


