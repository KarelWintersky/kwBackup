# ============================================================================
# SELFTEST
# ============================================================================

def selftest():
    failures = []

    def check(name, cond, extra=""):
        if not cond:
            failures.append(f"{name}: {extra}")

    # yaml-lite
    src = """
a: 1
b:
  - x
  - k: v
    n: 2
c: [1, two, "3"]
d: {k: v, n: 2}
e: ~
f: ""
# comment
g: "a: b"   # tail
h: ${HOME}/bin
"""
    doc = yaml_loads(src)
    check("yaml scalar", doc["a"] == 1)
    check("yaml seq", doc["b"][0] == "x" and doc["b"][1]["k"] == "v" and doc["b"][1]["n"] == 2)
    check("yaml flow seq", doc["c"] == [1, "two", "3"])
    check("yaml flow map", doc["d"] == {"k": "v", "n": 2})
    check("yaml null", doc["e"] is None)
    check("yaml empty str", doc["f"] == "")
    check("yaml quoted colon", doc["g"] == "a: b")
    check("yaml template", doc["h"] == "${HOME}/bin")
    for bad, why in (("a:\n\tb: 1", "tabs"), ("a: &x 1", "anchor"),
                     ("a: |\n  t", "block scalar"), ("a: 1\na: 2", "duplicate key")):
        try:
            yaml_loads(bad)
            check(f"yaml rejects {why}", False, "parsed without error")
        except YamlError:
            pass

    # retention
    now = dt.datetime(2026, 11, 11, 3, 0, 0)
    mk = lambda *days: [RemoteFile(f"db_2026-11-{d:02d}-03-00-00.sql.zst", 10, dt.datetime(2026, 11, d, 3)) for d in days]
    check("keep keeps newest",
          set(plan_prune(mk(1, 2, 3, 4), Retention(keep=2), now)) == {mk(1)[0].name, mk(2)[0].name})
    check("keep ignores foreign", plan_prune(mk(1, 2) + [RemoteFile("readme.txt", 1, None)],
                                             Retention(keep=0 or 1), now) == [mk(1)[0].name])
    pruned = plan_prune(mk(1, 2, 3, 4), Retention(max_age=dt.timedelta(days=7)), now)
    check("max_age prunes old", set(pruned) == {f.name for f in mk(1, 2, 3)})
    check("max_age keeps fresh",
          plan_prune(mk(6, 7, 8), Retention(max_age=dt.timedelta(days=7)), now) == [])
    check("week covered", period_covered(mk(9, 10, 11), "week", now))
    check("month covered", period_covered(mk(9), "month", now))
    check("daily always due", class_due("daily", [], False, now))
    check("weekly on monday", class_due("weekly", [], False, dt.datetime(2026, 11, 9, 3)))
    check("weekly not on tuesday", not class_due("weekly", [], False, now))
    check("weekly catch-up", class_due("weekly", [], True, now))
    check("weekly catch-up already done", not class_due("weekly", mk(9), True, now))
    check("gaps", missing_days(mk(8, 10)) == [dt.date(2026, 11, 9)])
    check("parse_age", parse_age("7d") == dt.timedelta(days=7) and parse_age("12h") == dt.timedelta(hours=12))
    check("fmt_age", fmt_age(dt.timedelta(days=43)) == "43d")

    # built-in defaults (the single source of default values)
    check("builtin db type", _default("database", "type") == "mysql")
    check("builtin include is a copy",
          _default("archive", "include") == ["*"]
          and _default("archive", "include") is not BUILTIN_DEFAULTS["archive"]["include"])
    check("builtin system path", str(SYSTEM_DEFAULTS_PATH) == "/etc/kwbackup/defaults.yaml")

    # defaults merge
    base = {"rclone": {"args": ["--a"], "remote": "r"},
            "database": {"databases": ["d1"], "retention": {"daily": {"keep": 1}}}}
    over = {"rclone": {"args": ["--b"]},
            "database": {"databases": ["d2"], "retention": {"weekly": {"keep": 2}}}}
    merged = deep_merge(base, over)
    check("merge appends rclone.args", merged["rclone"]["args"] == ["--a", "--b"])
    check("merge replaces databases", merged["database"]["databases"] == ["d2"])
    check("merge keeps maps", merged["database"]["retention"] ==
          {"daily": {"keep": 1}, "weekly": {"keep": 2}})
    check("merge scalar wins", deep_merge({"a": 1, "b": {"c": 1}}, {"b": {"c": 2}})["b"]["c"] == 2)

    # config validation
    with tempfile.TemporaryDirectory() as tmp:
        base = """
project: t
rclone: {}
upstreams:
  r: {config: /dev/null}
database:
  enabled: true
  type: maria
  targets: [r:/DB]
  databases: [db1]
  retention:
    daily: {keep: 8}
"""
        def write(text):
            p = Path(tmp) / "c.yaml"
            p.write_text(text, encoding="utf-8")
            return p
        try:
            load_config(write(base))
            check("config good", False, "should fail: bad db type")
        except ConfigError as exc:
            check("config bad type", "unsupported" in str(exc), str(exc))
        both = base.replace("type: maria", "type: mysql").replace(
            "{keep: 8}", "{keep: 8, max_age: 7d}")
        try:
            load_config(write(both))
            check("retention exclusive", False, "both keys accepted")
        except ConfigError as exc:
            check("retention exclusive", "mutually exclusive" in str(exc), str(exc))
        good = base.replace("type: maria", "type: mysql")
        cfg = load_config(write(good))
        check("config loads", cfg.project == "t" and cfg.database.retention["daily"].keep == 8)
        warned = good.replace("project: t", "project: t\nnonsense: 1")
        cfg_w = load_config(write(warned))
        check("unknown key warns", any("nonsense" in w for w in cfg_w.warnings),
              cfg_w.warnings)

        off = good.replace("enabled: true", "enabled: false").replace(
            "targets: [r:/DB]", "targets: [nope:/DB]")
        cfg_off = load_config(write(off))
        check("disabled section deferred",
              any("unknown upstream" in m for m in cfg_off.deferred_errors),
              cfg_off.deferred_errors)
        try:
            load_config(write(off), strict=True)
            check("disabled section strict", False, "strict accepted the error")
        except ConfigError as exc:
            check("disabled section strict", "unknown upstream" in str(exc), str(exc))

        typo = good.replace("databases: [db1]", "databeses: [db1]")
        try:
            load_config(write(typo))
            check("typo still fatal", False, "accepted a config without databases")
        except ConfigError as exc:
            check("typo still fatal", "databases: required" in str(exc), str(exc))

    if failures:
        for f in failures:
            print(f"FAIL {f}")
        print(f"{len(failures)} selftest(s) failed")
        return EXIT_ERROR
    print("selftest: all checks passed")
    return EXIT_OK
