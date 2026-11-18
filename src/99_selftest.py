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

        with_hooks = good + """
archive:
  enabled: false
  before: ["echo a", "echo b"]
  after: ["echo c"]
"""
        cfg_h = load_config(write(with_hooks))
        check("hooks parsed", cfg_h.archive.before == ["echo a", "echo b"]
              and cfg_h.archive.after == ["echo c"], cfg_h.archive.before)
        try:
            load_config(write(good + "\narchive:\n  enabled: false\n  before: 5\n"),
                       strict=True)
            check("hook type validated", False, "accepted a number as a hook")
        except ConfigError as exc:
            check("hook type validated", "before" in str(exc), str(exc))
        cfg_t = load_config(write(good + "\nruntime:\n  hook_timeout: 30\n"))
        check("hook timeout", cfg_t.hook_timeout == 30, cfg_t.hook_timeout)
        cfg_it = load_config(write(good.replace(
            "databases: [db1]", "databases: [db1]\n  ignore_table: [logs]")))
        check("ignore_table parsed", cfg_it.database.ignore_table == ["logs"],
              cfg_it.database.ignore_table)
        cfg_ae = load_config(write(
            good + "\nstorage:\n  enabled: false\n  allow_empty: true\n"))
        check("allow_empty parsed", cfg_ae.storage.allow_empty is True,
              cfg_ae.storage.allow_empty)

        with_vault = good + """
vault:
  enabled: true
  root: /tmp
  volume_size: 3G
  targets: ["r:/V"]
  journal: {type: txt, period: daily, path: /tmp/journal, keep_done: 10d}
  restore: {order: asc, priority: [photos], chown: true}
"""
        cfg_v = load_config(write(with_vault))
        check("vault parsed", cfg_v.vault.enabled
              and cfg_v.vault.volume_size == 3 * 1024 ** 3
              and cfg_v.vault.journal.type == "txt"
              and cfg_v.vault.journal.keep_done == dt.timedelta(days=10)
              and cfg_v.vault.restore.order == "asc"
              and cfg_v.vault.restore.chown is True, cfg_v.vault)
        cfg_vs = load_config(write(good + """
vault:
  enabled: false
  journal: {type: scan}
"""))
        check("vault scan", cfg_vs.vault.journal.type == "scan",
              cfg_vs.vault.journal)

        with_notify = good + """
notify:
  on: [success]
  webhook: {url: "http://127.0.0.1:9999/hook"}
"""
        cfg_n = load_config(write(with_notify))
        check("notify parsed", cfg_n.notify is not None
              and cfg_n.notify.on == ["success"]
              and cfg_n.notify.webhook.url.endswith("/hook"), cfg_n.notify)
        bad_notify = good + "\nnotify:\n  on: [never]\n"
        try:
            load_config(write(bad_notify))
            check("notify on validated", False, "accepted 'never'")
        except ConfigError as exc:
            check("notify on validated", "notify.on" in str(exc), str(exc))

        typo = good.replace("databases: [db1]", "databeses: [db1]")
        try:
            load_config(write(typo))
            check("typo still fatal", False, "accepted a config without databases")
        except ConfigError as exc:
            check("typo still fatal", "databases: required" in str(exc), str(exc))

    # progress line and the source size behind the percentage
    line = progress_line("packing", 1536, 1024, None, "a.tar")
    check("progress line", line == "  packing 1.5 KiB, 1.0 KiB/s (a.tar)", line)
    line = progress_line("dumping", 2048, 512, 34.6, "db.sql.zst")
    check("progress percent",
          line == "  dumping 2.0 KiB, 512 B/s, 35% (db.sql.zst)", line)

    long_name = "data_2026-11_full_2026-11-15-19-54-31.tar"
    line = progress_line("packing", 3 * 2**30, 300 * 2**20, 7, long_name, 50)
    check("progress width", len(line) <= 50 and line.endswith("...)"), line)

    with tempfile.TemporaryDirectory() as src_dir:
        root = Path(src_dir)
        (root / "a").write_bytes(b"x" * 100)
        (root / "b.tmp").write_bytes(b"x" * 200)
        (root / "sub").mkdir()
        (root / "sub" / "c").write_bytes(b"x" * 50)
        check("source_bytes", source_bytes(root, ["*"], ["*.tmp"]) == 150,
              source_bytes(root, ["*"], ["*.tmp"]))
        check("source_bytes include", source_bytes(root, ["sub"], []) == 50,
              source_bytes(root, ["sub"], []))

    # journal: txt-daily, complete lines only, .done rename, TTL cleanup
    with tempfile.TemporaryDirectory() as jdir:
        jroot = Path(jdir)
        old = jroot / "2026-11-10.journal.txt"
        old.write_text(
            "2026-11-10T01:00:00Z\tADDED\tphotos/a.jpg\n"
            "2026-11-10T01:00:01Z\tDELETED\tphotos/old.jpg\n"
            "2026-11-10T01:00:02Z\tBROKEN\tphotos/x.jpg\n"
            "2026-11-10T01:00:03Z\tADDED\t../escape\n"
            "2026-11-10T01:00:04Z\tADDED\tphotos/partial", encoding="utf-8")
        (jroot / "2026-11-17.journal.txt").write_text(
            "2026-11-17T02:00:00Z\tADDED\tphotos/2026/11/b.jpg\n", encoding="utf-8")
        (jroot / "2026-11-18.journal.txt").write_text("", encoding="utf-8")
        (jroot / "2026-11-09.journal.txt.done").write_text("", encoding="utf-8")

        pending = pending_journals(jroot, today=dt.date(2026, 11, 18))
        check("journal pending",
              [day for day, _ in pending] == ["2026-11-10", "2026-11-17"], pending)

        warnings = []
        events = read_journal(old, warnings)
        check("journal events",
              [(e.op, e.path) for e in events]
              == [("ADDED", "photos/a.jpg"), ("DELETED", "photos/old.jpg")], events)
        check("journal warnings", len(warnings) == 2, warnings)

        events_all, files = read_pending(jroot, today=dt.date(2026, 11, 18),
                                 warnings=[])
        check("journal read_pending",
              len(files) == 2 and len(events_all) == 3, (files, events_all))

        mark_done(pending[0][1])
        check("journal mark_done",
              not pending[0][1].exists()
              and [day for day, _ in pending_journals(jroot, today=dt.date(2026, 11, 18))]
              == ["2026-11-17"], pending_journals(jroot, today=dt.date(2026, 11, 18)))

        removed = cleanup_done(jroot, keep=dt.timedelta(days=7),
                               today=dt.date(2026, 11, 18))
        check("journal cleanup_done",
              len(removed) == 2 and not (jroot / "2026-11-09.journal.txt.done").exists(),
              removed)
        check("journal cleanup off", cleanup_done(jroot, keep=None) == [])

    # dump command: args are appended to the defaults, ignore_table expands
    db_mysql = SimpleNamespace(type="mysql", args=["--skip-extended-insert"],
                               host="localhost", port=None, user=None,
                               ignore_table=["logs", "shop.sessions"])
    cmd = dump_command(SimpleNamespace(database=db_mysql), "shop")
    check("dump args appended",
          "--single-transaction" in cmd and "--skip-extended-insert" in cmd, cmd)
    check("ignore_table mysql",
          cmd.count("--ignore-table=shop.logs") == 1
          and "--ignore-table=shop.sessions" in cmd, cmd)
    db_pg = SimpleNamespace(type="postgres", args=[], host="localhost", port=5432,
                            user="u", ignore_table=["audit"])
    cmd = dump_command(SimpleNamespace(database=db_pg), "shop")
    check("ignore_table postgres",
          "--exclude-table=audit" in cmd and "--no-owner" in cmd, cmd)

    # vault upload rollback: no manifest may survive a failed multi-target put
    class FakeStore:
        name = "fake"
        path = "/x"

        def __init__(self, fail=False):
            self.fail, self.deleted = fail, []

        def put(self, files):
            if self.fail:
                raise CommandError("rclone", 1)

        def delete(self, name):
            self.deleted.append(name)

    good, bad = FakeStore(), FakeStore(fail=True)
    files = [Path("/tmp/x/a-part-001.tar"), Path("/tmp/x/a.manifest.json")]
    try:
        _vault_upload([good, bad], files)
        check("vault upload rollback", False, "upload failure was ignored")
    except ActionFailed:
        check("vault upload rollback",
              good.deleted == ["a.manifest.json"], (good.deleted, bad.deleted))

    check("vault exclude",
          vault_excluded(".state/db", [".state"])
          and vault_excluded("a/b/47news_state.sqlite", ["47news_state.sqlite"])
          and vault_excluded("a.tmp", ["*.tmp"])
          and not vault_excluded("keep/a.txt", [".state"]),
          "exclude matcher is wrong")

    check("current tar file",
          _current_file([("a", 10), ("b", 20)], 5) == "a"
          and _current_file([("a", 10), ("b", 20)], 15) == "b"
          and _current_file([("a", 10), ("b", 20)], 30) == "b",
          "file mapping is wrong")

    # notifications: safe reasons, message contents, channel plumbing
    check("yaml keys stay strings",
          yaml_loads("notify:\n  on: [success]\n")["notify"].get("on") == ["success"],
          yaml_loads("notify:\n  on: [success]\n"))
    reason = failure_reason(CommandError(
        "rclone --s3-secret-access-key=SECRET copyto a b", 3))
    check("failure reason", reason == "rclone exited with 3", reason)

    hook = WebhookCfg(url="http://127.0.0.1:9/hook")
    on_failure = NotifyCfg(on=["failure"], webhook=hook)
    check("notify gating",
          Notifier.enabled(on_failure, 1)
          and not Notifier.enabled(on_failure, 0)
          and not Notifier.enabled(NotifyCfg(on=[], webhook=hook), 1)
          and not Notifier.enabled(NotifyCfg(on=["failure"]), 1),
          "on/off gating is wrong")

    fake_cfg = SimpleNamespace(path=Path("/etc/kwbackup/shop.yaml"), project="shop")
    subject, text = Notifier.build_message(
        fake_cfg, "backup", 1, ["database: 2 db(s), 30 MiB"], None,
        [{"action": "archive", "entity": "s3a", "reason": "rclone exited with 1"}])
    check("notify message",
          "shop" in subject and "FAILED, exit 1" in text
          and "archive \u2192 s3a: rclone exited with 1" in text
          and "database: 2 db(s)" in text and "SECRET" not in text, text)

    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            sent.update(host=host, port=port)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self):
            sent["tls"] = True

        def login(self, user, password):
            sent["login"] = (user, password)

        def send_message(self, message):
            sent["subject"] = message["Subject"]
            sent["to"] = message["To"]

    real_smtp = smtplib.SMTP
    smtplib.SMTP = FakeSMTP
    try:
        Notifier._send_mail(
            MailCfg(to=["a@example.com", "b@example.com"],
                    sender="kw@example.com", tls=True), "subj", "body", 5)
    finally:
        smtplib.SMTP = real_smtp
    check("mail delivery", sent.get("subject") == "subj"
          and sent.get("to") == "a@example.com, b@example.com"
          and sent.get("tls") is True, sent)

    posted = {}
    real_http = Notifier._http_json
    Notifier._http_json = staticmethod(
        lambda url, payload, headers, timeout: posted.update(url=url, payload=payload))
    os.environ["KW_TEST_TG"] = "token-123"
    try:
        Notifier._send_telegram(
            TelegramCfg(token_env="KW_TEST_TG", chat_id="42"), "subj", "body", 5)
    finally:
        Notifier._http_json = real_http
        os.environ.pop("KW_TEST_TG", None)
    check("telegram delivery",
          posted.get("payload", {}).get("chat_id") == "42"
          and posted.get("url", "").endswith("/sendMessage")
          and "token-123" in posted.get("url", ""), posted)

    if failures:
        for f in failures:
            print(f"FAIL {f}")
        print(f"{len(failures)} selftest(s) failed")
        return EXIT_ERROR
    print("selftest: all checks passed")
    return EXIT_OK
