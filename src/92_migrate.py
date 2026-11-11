# ============================================================================
# MIGRATE  (old bash config -> YAML)
#
# The old config is executable bash, so the only reliable reader is bash
# itself: source it in a subshell and ask for the variables we know about.
# Nothing from the old file is executed in our process.
# ============================================================================

MIGRATE_VARS = (
    "TEMP_PATH", "LOG_FILE", "RCLONE_CONFIG", "RCLONE_PROVIDER", "RCLONE_CUSTOM_OPTIONS",
    "ENABLE_BACKUP_DATABASE", "DB_TYPE", "DB_HOST", "DB_USER", "DATABASES",
    "DATABASE_MYSQL_OPTIONS", "DATABASE_PGSQL_OPTIONS", "DATABASE_SQLITE_OPTIONS",
    "DATABASE_RAR_OPTIONS", "DATABASE_ZSTD_OPTIONS", "DATABASE_CLOUD_CONTAINER",
    "USE_ARCHIVER", "DB_BACKUP_DAILY", "DB_BACKUP_WEEKLY", "DB_BACKUP_MONTHLY",
    "DB_MIN_AGE_DAILY", "DB_MIN_AGE_WEEKLY", "DB_MIN_AGE_MONTHLY",
    "ENABLE_BACKUP_STORAGE", "STORAGE_SOURCES_ROOT", "STORAGE_SOURCES",
    "STORAGE_CLOUD_CONTAINER", "STORAGE_BACKUP_ALGO",
    "ENABLE_BACKUP_ARCHIVE", "ARCHIVE_USE_COMPRESSOR", "ARCHIVE_RAR_OPTIONS",
    "ARCHIVE_ZSTD_OPTIONS", "ARCHIVE_GZIP_OPTIONS", "ARCHIVE_FILENAME",
    "ARCHIVE_ROOT", "ARCHIVE_INCLUDE_LIST", "ARCHIVE_EXCLUDE_LIST",
    "ARCHIVE_CLOUD_CONTAINER", "ARCHIVE_ROTATION_PERIOD",
)

DECL_RE = re.compile(r"^declare -(?P<flags>[a-zA-Z]*)\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)="
                     r"(?P<value>.*)$")
ARRAY_RE = re.compile(r'\[[0-9]+\]="((?:[^"\\]|\\.)*)"')
SHELL_ESC = re.compile(r'\\([\\"$`])')
TS_SUFFIX_RE = re.compile(r"_\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}$")
SECRET_HINT = ("access-key", "secret", "password", "token")

COMPRESSOR_MAP = {"zip": "gzip", "pigz": "gzip", "gz": "gzip", "zst": "zstd"}


def _unescape(text):
    return SHELL_ESC.sub(r"\1", text)


def read_bash_config(path):
    """Source the old config and return {NAME: str | [str]}."""
    script = ('set -a; . "$1" >/dev/null 2>&1 || true; shift; '
              'for v in "$@"; do declare -p "$v" 2>/dev/null; done; exit 0')
    res = subprocess.run(["bash", "-c", script, "--", str(path), *MIGRATE_VARS],
                         capture_output=True, text=True)
    if res.returncode != 0:
        raise ActionFailed(f"migrate: cannot read {path}: {res.stderr.strip()}")
    values = {}
    for line in res.stdout.splitlines():
        m = DECL_RE.match(line.strip())
        if not m:
            continue
        name, raw = m.group("name"), m.group("value")
        if raw.startswith("("):
            values[name] = [_unescape(v) for v in ARRAY_RE.findall(raw)]
        else:
            values[name] = _unescape(raw.strip()[1:-1]) if raw.strip().startswith('"') else raw
    return values


def _flag(values, name, default=False):
    raw = values.get(name)
    if raw in (None, ""):
        return default
    return str(raw).strip() in ("1", "y", "yes", "true", "on")


def _str(values, name, default=""):
    value = values.get(name, default)
    if isinstance(value, list):
        return value[0] if value else default
    return value if value not in (None, "") else default


def _list(values, name):
    value = values.get(name)
    if value is None:
        return []
    if isinstance(value, list):
        return [v for v in value if v != ""]
    return [p for p in str(value).split() if p]


def _keep_from_age(age, period_days):
    """Convert an old max_age window into a copy count (priority: keep)."""
    try:
        seconds = parse_age(age).total_seconds()
    except ValueError:
        return None
    return max(1, round(seconds / (period_days * 86400)))


def _yaml_value(value):
    """Quote a value for our YAML subset when needed."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    text = str(value)
    if text == "":
        return '""'
    plain_ok = (text == text.strip()
                and not text.startswith(("-", "?", ":", "!", "&", "*", "|", ">", "@", "`",
                                         "[", "]", "{", "}", "#", "%", ",", '"', "'"))
                and ": " not in text and " #" not in text
                and text.lower() not in ("true", "false", "yes", "no", "on", "off", "null", "~")
                and not re.fullmatch(r"-?\d+(\.\d+)?", text))
    if plain_ok:
        return text
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def build_yaml(old, project, source_path):
    out = []
    add = out.append

    def section(title):
        add("")
        add(f"# {'-' * 74}")
        add(f"# {title}")
        add(f"# {'-' * 74}")

    add(f"# Сгенерировано kwbackup migrate из {source_path}")
    add("# Проверьте перед использованием: kwbackup -c <этот файл> --check-config")
    add("")
    add("version: 1")
    add(f"project: {_yaml_value(project)}")

    log_file = _str(old, "LOG_FILE")
    if log_file:
        section("log")
        add("log:")
        add("  level: info")
        add(f"  file: {_yaml_value(log_file)}")

    temp = _str(old, "TEMP_PATH", "/tmp")
    section("runtime")
    add("runtime:")
    add(f"  temp_dir: {_yaml_value(str(Path(temp) / 'kwbackup') if temp.rstrip('/') == '/tmp' else temp)}")

    remote = _str(old, "RCLONE_PROVIDER", "s3")
    section("rclone")
    add("rclone:")
    add(f"  remote: {_yaml_value(remote)}")
    rclone_config = _str(old, "RCLONE_CONFIG")
    if rclone_config:
        add(f"  config: {_yaml_value(rclone_config)}")
    custom = _list(old, "RCLONE_CUSTOM_OPTIONS")
    if custom:
        secrets = [a for a in custom if any(h in a.lower() for h in SECRET_HINT)]
        add("  args:")
        for arg in custom:
            add(f"    - {_yaml_value(arg)}")
        if secrets:
            add("  # !!! В args выше ключи доступа. BEGET выдаёт их на бакет,")
            add("  #     поэтому они и передаются флагами rclone (env_auth = false")
            add("  #     в rclone.conf игнорирует AWS_* переменные). Держите файл")
            add("  #     с правами 0600 либо подставляйте из окружения:")
            add('  #     "--s3-access-key-id=${BEGET_SHOP_KEY}"')

    if _flag(old, "ENABLE_BACKUP_DATABASE"):
        db_type = _str(old, "DB_TYPE", "mysql").lower()
        compressor = COMPRESSOR_MAP.get(_str(old, "USE_ARCHIVER", "").lower(),
                                        _str(old, "USE_ARCHIVER", "").lower() or "none")
        container = _str(old, "DATABASE_CLOUD_CONTAINER", "/DB")
        section("database")
        add("database:")
        add("  enabled: true")
        add(f"  type: {_yaml_value(db_type)}")
        add(f"  host: {_yaml_value(_str(old, 'DB_HOST', 'localhost'))}")
        user = _str(old, "DB_USER")
        if user:
            add(f"  user: {_yaml_value(user)}")
        if db_type in ("postgres", "pgsql"):
            dump_args = _list(old, "DATABASE_PGSQL_OPTIONS")
        elif db_type == "sqlite":
            dump_args = _list(old, "DATABASE_SQLITE_OPTIONS")
        else:
            dump_args = _list(old, "DATABASE_MYSQL_OPTIONS")
        if dump_args:
            add("  args:")
            for arg in dump_args:
                add(f"    - {_yaml_value(arg)}")
        else:
            add("  args: []")
        add(f"  compressor: {_yaml_value(compressor)}")
        comp_args = _list(old, "DATABASE_ZSTD_OPTIONS" if compressor == "zstd"
                          else "DATABASE_RAR_OPTIONS")
        if comp_args:
            add(f"  compressor_args: [{', '.join(_yaml_value(a) for a in comp_args)}]")
        add(f"  target: {_yaml_value(f'{remote}:{container}')}")
        databases = _list(old, "DATABASES")
        if databases:
            add("  databases:")
            for name in databases:
                add(f"    - {_yaml_value(name)}")
        else:
            add("  databases: []          # в старом конфиге DATABASES был пуст")
        add("  catch_up: false")
        add("  retention_mode: keep")
        add("  retention:")
        for cls, enabled_key, age_key, period in (
                ("daily", "DB_BACKUP_DAILY", "DB_MIN_AGE_DAILY", 1),
                ("weekly", "DB_BACKUP_WEEKLY", "DB_MIN_AGE_WEEKLY", 7),
                ("monthly", "DB_BACKUP_MONTHLY", "DB_MIN_AGE_MONTHLY", 30)):
            if not _flag(old, enabled_key):
                add(f"    {cls}: false")
                continue
            age = _str(old, age_key, {"daily": "7d", "weekly": "43d",
                                      "monthly": "360d"}[cls])
            keep = _keep_from_age(age, period)
            if keep is None:
                add(f"    {cls}: {{max_age: {_yaml_value(age)}}}")
            else:
                add(f"    {cls}: {{keep: {keep}}}          # было max_age: {age}")

    if _flag(old, "ENABLE_BACKUP_STORAGE"):
        container = _str(old, "STORAGE_CLOUD_CONTAINER", "/STORAGE")
        root = _str(old, "STORAGE_SOURCES_ROOT")
        section("storage")
        add("storage:")
        add("  enabled: true")
        add(f"  mode: {_yaml_value(_str(old, 'STORAGE_BACKUP_ALGO', 'sync'))}")
        add(f"  target: {_yaml_value(f'{remote}:{container}')}")
        sources = _list(old, "STORAGE_SOURCES")
        if not sources and not root:
            add("  sources: []          # в старом конфиге не было источников")
        else:
            add("  sources:")
        if not sources and root:
            add(f"    - path: {_yaml_value(root)}")
            add('      dest: ""    # весь каталог в корень контейнера')
        for source in sources:
            add(f"    - {_yaml_value(source if not root else root.rstrip('/') + '/' + source)}")

    if _flag(old, "ENABLE_BACKUP_ARCHIVE"):
        compressor = COMPRESSOR_MAP.get(_str(old, "ARCHIVE_USE_COMPRESSOR", "").lower(),
                                        _str(old, "ARCHIVE_USE_COMPRESSOR", "").lower() or "none")
        container = _str(old, "ARCHIVE_CLOUD_CONTAINER", "/ARCHIVE")
        name = TS_SUFFIX_RE.sub("", _str(old, "ARCHIVE_FILENAME", "archive")) or "archive"
        section("archive")
        add("archive:")
        add("  enabled: true")
        add(f"  compressor: {_yaml_value(compressor)}")
        comp_args = _list(old, {"zstd": "ARCHIVE_ZSTD_OPTIONS"}.get(
            compressor, "ARCHIVE_RAR_OPTIONS" if compressor == "rar" else "ARCHIVE_GZIP_OPTIONS"))
        if comp_args:
            add(f"  compressor_args: [{', '.join(_yaml_value(a) for a in comp_args)}]")
        add(f"  name: {_yaml_value(name)}")
        archive_root = _str(old, "ARCHIVE_ROOT")
        add(f"  root: {_yaml_value(archive_root)}"
            + ("" if archive_root else "          # !!! в старом конфиге не был задан"))
        includes = _list(old, "ARCHIVE_INCLUDE_LIST") or ["*"]
        excludes = _list(old, "ARCHIVE_EXCLUDE_LIST")
        add(f"  include: [{', '.join(_yaml_value(p) for p in includes)}]")
        add(f"  exclude: [{', '.join(_yaml_value(p) for p in excludes)}]")
        add(f"  target: {_yaml_value(f'{remote}:{container}')}")
        period = _str(old, "ARCHIVE_ROTATION_PERIOD", "71d")
        keep = _keep_from_age(period, 1)
        if keep is not None:
            add(f"  retention: {{keep: {keep}}}          # было max_age: {period}")
        if _str(old, "ARCHIVE_RAR_INCLUDE_LIST") or _str(old, "ARCHIVE_RAR_EXCLUDE_LIST"):
            add("  # Старые списки include/exclude для rar не переносятся: используйте")
            add("  # include/exclude выше, они работают для любого компрессора.")
    add("")
    return "\n".join(out)


def migrate_main(argv):
    parser = argparse.ArgumentParser(
        prog=f"{PROG} migrate",
        description="Convert a legacy bash config (kwbackup.sh) into YAML.")
    parser.add_argument("config", help="old .conf file")
    parser.add_argument("-o", "--output", help="write YAML here (default: stdout)")
    parser.add_argument("-p", "--project", help="project name (default: derived from path)")
    parser.add_argument("-f", "--force", action="store_true", help="overwrite output file")
    args = parser.parse_args(argv)

    src = Path(args.config)
    if not src.is_file():
        print(f"{PROG} migrate: {src}: not found", file=sys.stderr)
        return EXIT_USAGE
    project = args.project
    if not project:
        dirname = src.resolve().parent.name or "default"
        stem = src.stem.lstrip("_.") or dirname
        project = stem if stem not in ("config", "conf") else dirname
    try:
        old = read_bash_config(src)
    except ActionFailed as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return EXIT_ERROR
    if not old:
        print(f"{PROG} migrate: {src}: no known variables found, is this a kwbackup config?",
              file=sys.stderr)
        return EXIT_CONFIG
    text = build_yaml(old, project, src)

    if args.output:
        dst = Path(args.output)
        if dst.exists() and not args.force:
            print(f"{PROG} migrate: {dst} exists, use --force to overwrite", file=sys.stderr)
            return EXIT_USAGE
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(text, encoding="utf-8")
        print(f"written {dst} ({len(text.splitlines())} lines)")
        print(f"check it with: {PROG} -c {dst} --check-config")
    else:
        print(text)
    return EXIT_OK
