# ============================================================================
# CONFIG
# ============================================================================

class ConfigError(Exception):
    pass


# Validation schema: the allowed keys of every section. Anything else is a
# warning with the list of valid keys - a typo stays visible, but an old file
# with a leftover key still runs. Missing or invalid values remain fatal.
# syslog facilities accepted in log.facility (logging.handlers.SysLogHandler names)
SYSLOG_FACILITIES = ("auth", "authpriv", "cron", "daemon", "ftp", "kern", "lpr",
                     "mail", "news", "syslog", "user", "uucp",
                     "local0", "local1", "local2", "local3", "local4",
                     "local5", "local6", "local7")


SECTIONS = {
    "root": {"project", "log", "runtime", "rclone", "upstreams", "notify",
             "database", "storage", "archive", "incremental", "vault"},
    "log": {"level", "file", "syslog", "facility"},
    "runtime": {"temp_dir", "keep_temp", "verbose", "debug", "hook_timeout"},
    "rclone": {"binary", "args", "env", "s3-custom-options"},
    "upstream": {"type", "config", "args", "env", "s3-custom-options",
                 "bwlimit", "host", "ssh_options"},
    "database": {"enabled", "type", "host", "port", "user", "password_env",
                 "args", "compressor", "compressor_args", "targets", "mode",
                 "databases", "retention", "retention_mode", "catch_up",
                 "ignore_table", "before", "after", "s3-custom-options"},
    "storage": {"enabled", "mode", "targets", "sources", "allow_empty",
                "before", "after", "s3-custom-options"},
    "archive": {"enabled", "compressor", "compressor_args", "name", "root",
                "include", "exclude", "targets", "mode", "retention",
                "retention_mode", "before", "after", "s3-custom-options"},
    "incremental": {"enabled", "name", "root", "include", "exclude", "targets",
                    "initial_day", "snapshot", "retention", "before", "after",
                    "s3-custom-options"},
    "vault": {"enabled", "root", "state_dir", "volume_size", "checksum",
              "journal", "restore", "targets", "before", "after"},
    "journal": {"type", "period", "path", "keep_done"},
    "restore": {"order", "priority", "chown"},
    "notify": {"on", "retries", "retry_delay", "timeout",
               "mail", "webhook", "telegram"},
    "mail": {"to", "from", "smtp_host", "port", "tls", "user_env", "password_env"},
    "webhook": {"url", "headers"},
    "telegram": {"token_env", "chat_id"},
    "retention": {"keep", "max_age"},
    "source": {"path", "dest"},
}

# Enumerations checked in load_config.
COMPRESSORS = ("zstd", "gzip", "pigz", "rar", "none")
DB_TYPES = ("mysql", "mariadb", "postgres", "pgsql", "sqlite")
NOTIFY_ON = ("failure", "success", "always")

# Retention classes: validated here and iterated by the actions and reports.
RETENTION_CLASSES = ("daily", "weekly", "monthly")

# max_age syntax: 30s, 15m, 12h, 7d, 6w.
AGE_RE = re.compile(r"^(\d+)([smhdw])$")
AGE_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}

# Lists that are APPENDED when a task config overrides the defaults file.
# These are "add another option" lists where duplicates are harmless and the
# last flag wins (rclone, dumpers, compressors). Every other list is replaced:
# `databases`, `storage.sources` and `archive.include` must not silently grow
# when a single project overrides them.
APPEND_KEYS = {
    "rclone.args",
    "database.args",
    "database.compressor_args",
    "archive.compressor_args",
    "archive.exclude",
}


def parse_age(text):
    m = AGE_RE.match(str(text).strip())
    if not m:
        raise ValueError(f"bad age {text!r}, expected e.g. 7d, 43d, 360d, 12h")
    return dt.timedelta(seconds=int(m.group(1)) * AGE_UNITS[m.group(2)])


@dataclass
class Retention:
    """Deletion rule for one class. Exactly one of keep / max_age is set.

    keep    - count of newest files to survive (immune to missed runs).
    max_age - time window; everything older than the age is deleted.
    """
    keep: int | None = None
    max_age: dt.timedelta | None = None

    @property
    def mode(self):
        return "keep" if self.keep is not None else "max_age"

    def describe(self):
        if self.keep is not None:
            return f"keep {self.keep}"
        return f"max_age {fmt_age(self.max_age)}"


def fmt_age(td):
    for unit, size in (("w", 604800), ("d", 86400), ("h", 3600), ("m", 60)):
        if td.total_seconds() % size == 0 and td.total_seconds() >= size:
            return f"{int(td.total_seconds() // size)}{unit}"
    return f"{int(td.total_seconds())}s"


@dataclass
class RcloneCfg:
    """How to run rclone. Where to upload is UpstreamCfg."""
    binary: str = _default("rclone", "binary")
    args: list = field(default_factory=list)
    env: dict = field(default_factory=dict)
    s3_options: dict = field(default_factory=dict)


@dataclass
class UpstreamCfg:
    """One destination. rclone is the default transport; vault adds rsync."""
    name: str
    type: str = "rclone"          # rclone | rsync
    config: str | None = None
    args: list = field(default_factory=list)
    env: dict = field(default_factory=dict)
    s3_options: dict = field(default_factory=dict)
    bwlimit: int = 0              # bytes/s, 0 = unlimited
    host: str = ""                # rsync: user@host
    ssh_options: list = field(default_factory=list)


@dataclass
class DatabaseCfg:
    enabled: bool = False
    mode: str = _default("database", "mode")
    type: str = _default("database", "type")
    host: str = _default("database", "host")
    port: int | None = None
    user: str | None = None
    password_env: str | None = None
    args: list = field(default_factory=list)
    compressor: str = _default("database", "compressor")
    compressor_args: list = field(default_factory=list)
    targets: list = field(default_factory=list)
    databases: list = field(default_factory=list)
    retention: dict = field(default_factory=dict)      # class -> Retention | None
    catch_up: bool = _default("database", "catch_up")
    ignore_table: list = field(default_factory=lambda: _default("database", "ignore_table"))
    before: list = field(default_factory=list)
    after: list = field(default_factory=list)
    s3_options: dict = field(default_factory=dict)


@dataclass
class StorageCfg:
    enabled: bool = False
    mode: str = _default("storage", "mode")
    allow_empty: bool = _default("storage", "allow_empty")
    targets: list = field(default_factory=list)
    sources: list = field(default_factory=list)   # [(path, dest|None), ...]
    before: list = field(default_factory=list)
    after: list = field(default_factory=list)
    s3_options: dict = field(default_factory=dict)


@dataclass
class ArchiveCfg:
    enabled: bool = False
    mode: str = _default("archive", "mode")
    compressor: str = _default("archive", "compressor")
    compressor_args: list = field(default_factory=list)
    name: str = ""          # defaults to the project name, set in load_config
    root: str = ""
    include: list = field(default_factory=lambda: _default("archive", "include"))
    exclude: list = field(default_factory=list)
    targets: list = field(default_factory=list)
    retention: Retention | None = None
    before: list = field(default_factory=list)
    after: list = field(default_factory=list)
    s3_options: dict = field(default_factory=dict)


@dataclass
class IncrementalCfg:
    enabled: bool = False
    name: str = ""
    root: str = ""
    include: list = field(default_factory=lambda: _default("incremental", "include"))
    exclude: list = field(default_factory=list)
    targets: list = field(default_factory=list)
    initial_day: int = _default("incremental", "initial_day")
    snapshot: str = ""
    retention: Retention | None = None
    before: list = field(default_factory=list)
    after: list = field(default_factory=list)
    s3_options: dict = field(default_factory=dict)


@dataclass
class JournalCfg:
    type: str = "scan"            # txt | scan
    period: str = "daily"
    path: str = ""
    keep_done: dt.timedelta | None = None


@dataclass
class RestoreCfg:
    order: str = "desc"           # asc | desc
    priority: list = field(default_factory=list)
    chown: bool = False


@dataclass
class VaultCfg:
    enabled: bool = False
    root: str = ""
    state_dir: str = ""
    volume_size: int = 2 * 1024 ** 3
    checksum: str = "none"        # none | sha256
    journal: JournalCfg | None = None
    restore: RestoreCfg = field(default_factory=RestoreCfg)
    targets: list = field(default_factory=list)
    before: list = field(default_factory=list)
    after: list = field(default_factory=list)


@dataclass
class MailCfg:
    to: list = field(default_factory=list)
    sender: str = ""
    smtp_host: str = "localhost"
    port: int = 25
    tls: bool = False
    user_env: str = ""
    password_env: str = ""


@dataclass
class WebhookCfg:
    url: str = ""
    headers: dict = field(default_factory=dict)


@dataclass
class TelegramCfg:
    token_env: str = ""
    chat_id: str = ""


@dataclass
class NotifyCfg:
    on: list = field(default_factory=list)          # empty = notifications off
    retries: int = 0
    retry_delay: int = 3
    timeout: int = 10
    mail: MailCfg | None = None
    webhook: WebhookCfg | None = None
    telegram: TelegramCfg | None = None

    def channels(self):
        return [ch for ch in (self.mail, self.webhook, self.telegram) if ch]


@dataclass
class Config:
    path: Path
    project: str
    defaults_path: Path | None = None
    present_sections: set = field(default_factory=set)   # seen in defaults/task, not builtins
    warnings: list = field(default_factory=list)         # unknown keys and the like
    deferred_errors: list = field(default_factory=list)  # disabled sections, --check reports
    log_level: str = _default("log", "level")
    log_file: str | None = None
    log_syslog: bool | str = _default("log", "syslog")   # False | True | socket path
    log_facility: str = _default("log", "facility")
    temp_dir: Path = Path(_default("runtime", "temp_dir"))
    keep_temp: bool = _default("runtime", "keep_temp")
    verbose: bool = _default("runtime", "verbose")
    debug: bool = _default("runtime", "debug")
    hook_timeout: int = _default("runtime", "hook_timeout")
    rclone: RcloneCfg = field(default_factory=RcloneCfg)
    upstreams: dict = field(default_factory=dict)   # name -> UpstreamCfg
    database: DatabaseCfg = field(default_factory=DatabaseCfg)
    storage: StorageCfg = field(default_factory=StorageCfg)
    archive: ArchiveCfg = field(default_factory=ArchiveCfg)
    incremental: IncrementalCfg = field(default_factory=IncrementalCfg)
    vault: VaultCfg = field(default_factory=VaultCfg)
    notify: NotifyCfg | None = None


def find_defaults(explicit=None):
    """The system defaults file. Only /etc/kwbackup/defaults.yaml, nothing else.

    `explicit` is the hidden --defaults flag: tests and running the examples
    in place. None means "no file, the built-in defaults are the whole base".
    """
    if explicit:
        return Path(explicit)
    return SYSTEM_DEFAULTS_PATH if SYSTEM_DEFAULTS_PATH.is_file() else None



def deep_merge(base, override, path=""):
    """Defaults <- task. Maps merge, APPEND_KEYS lists append, the rest wins."""
    result = dict(base)
    for key, value in override.items():
        full = f"{path}.{key}" if path else key
        current = result.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            result[key] = deep_merge(current, value, full)
        elif isinstance(current, list) and isinstance(value, list) and full in APPEND_KEYS:
            # dedupe: the same flag in defaults and in the task is harmless
            # but noisy in logs
            result[key] = list(dict.fromkeys(list(current) + list(value)))
        else:
            result[key] = value
    return result


def _read_yaml(path):
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"{path}: {exc.strerror}") from exc
    try:
        data = yaml_loads(text)
    except YamlError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return data


def _check_keys(data, section, path, warnings):
    allowed = SECTIONS[section]
    for key in data:
        if key not in allowed:
            warnings.append(f"{path}.{key}: unknown key, ignored "
                            f"(allowed: {', '.join(sorted(allowed))})")


def _as_str(value, path, errors, default=None, required=False):
    if value is None:
        if required:
            errors.append(f"{path}: required")
        return default
    if not isinstance(value, str):
        errors.append(f"{path}: expected a string, got {type(value).__name__}")
        return default
    return value


def _as_bool(value, path, errors, default=False):
    if value is None:
        return default
    if not isinstance(value, bool):
        errors.append(f"{path}: expected true/false, got {value!r}")
        return default
    return value


def _as_int(value, path, errors, default=None, minimum=None):
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool):
        errors.append(f"{path}: expected an integer, got {value!r}")
        return default
    if minimum is not None and value < minimum:
        errors.append(f"{path}: must be >= {minimum}, got {value}")
        return default
    return value


SIZE_RE = re.compile(r"^(\d+)([KMGTP]?)$", re.IGNORECASE)
SIZE_UNITS = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3,
              "T": 1024 ** 4, "P": 1024 ** 5}


def _as_size(value, path, errors, default=None):
    if value is None:
        return default
    if isinstance(value, bool):
        errors.append(f"{path}: expected a size like 2G")
        return default
    if isinstance(value, int):
        if value < 0:
            errors.append(f"{path}: must be >= 0")
            return default
        return value
    if isinstance(value, str):
        match = SIZE_RE.match(value.strip())
        if match:
            return int(match.group(1)) * SIZE_UNITS[match.group(2).upper()]
    errors.append(f"{path}: expected a size like 2G, 500M or 0, got {value!r}")
    return default


def _as_timedelta(value, path, errors, default=None):
    if value is None:
        return default
    if value is False:
        return None
    if isinstance(value, bool):
        errors.append(f"{path}: expected a duration like 7d or false")
        return default
    if isinstance(value, int):
        if value < 0:
            errors.append(f"{path}: must be >= 0")
            return default
        return dt.timedelta(seconds=value)
    if isinstance(value, str):
        match = AGE_RE.match(value.strip())
        if match:
            return dt.timedelta(seconds=int(match.group(1)) * AGE_UNITS[match.group(2)])
        if value.strip().lower() in ("false", "none", "off"):
            return None
    errors.append(f"{path}: expected a duration like 7d or false, got {value!r}")
    return default


def _as_list(value, path, errors):
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        out = []
        for i, item in enumerate(value):
            if not isinstance(item, str):
                errors.append(f"{path}[{i}]: expected a string, got {item!r}")
                continue
            out.append(item)
        return out
    errors.append(f"{path}: expected a string or a list of strings")
    return []


def _expand_env(value, path, errors):
    if isinstance(value, str):
        def repl(m):
            name = m.group(1)
            if name not in os.environ:
                errors.append(f"{path}: environment variable {name} is not set")
                return ""
            return os.environ[name]
        return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", repl, value)
    if isinstance(value, list):
        return [_expand_env(v, f"{path}[{i}]", errors) for i, v in enumerate(value)]
    if isinstance(value, dict):
        return {k: _expand_env(v, f"{path}.{k}", errors) for k, v in value.items()}
    return value


def _as_str_map(value, path, errors):
    if value is None:
        return {}
    if not isinstance(value, dict):
        errors.append(f"{path}: expected a mapping")
        return {}
    out = {}
    for key, item in value.items():
        if not isinstance(key, str):
            errors.append(f"{path}: keys must be strings")
            continue
        if isinstance(item, bool):
            out[key] = "true" if item else "false"      # YAML turns yes/no into bool
        elif isinstance(item, (int, float)):
            out[key] = str(item)
        elif isinstance(item, str):
            out[key] = item
        else:
            errors.append(f"{path}.{key}: expected a string")
            continue
    return out


def _parse_retention(value, path, errors, warnings, mode_hint=None):
    """Parse one retention class. Returns Retention or None (class disabled)."""
    if value is None or value is False:
        return None
    if value is True:
        errors.append(f"{path}: true is not a retention rule; use {{keep: N}} or {{max_age: 7d}}")
        return None
    if not isinstance(value, dict):
        errors.append(f"{path}: expected {{keep: N}} or {{max_age: 7d}}")
        return None
    _check_keys(value, "retention", path, warnings)
    has_keep, has_age = "keep" in value, "max_age" in value
    if has_keep and has_age:
        errors.append(f"{path}: 'keep' and 'max_age' are mutually exclusive, pick one")
        return None
    if not has_keep and not has_age:
        errors.append(f"{path}: specify exactly one of 'keep' or 'max_age'")
        return None
    if mode_hint == "keep" and has_age:
        errors.append(f"{path}: retention_mode is 'keep', so 'max_age' is not allowed")
        return None
    if mode_hint == "max_age" and has_keep:
        errors.append(f"{path}: retention_mode is 'max_age', so 'keep' is not allowed")
        return None
    if has_keep:
        keep = _as_int(value["keep"], f"{path}.keep", errors, minimum=1)
        return Retention(keep=keep) if keep is not None else None
    try:
        age = parse_age(value["max_age"])
    except ValueError as exc:
        errors.append(f"{path}.max_age: {exc}")
        return None
    return Retention(max_age=age)



class _Errors(list):
    """Fatal config errors; while parsing a disabled section they are deferred.

    A disabled section is still parsed -- typos and stale upstream names must
    not crash a host that has the task switched off -- but the messages are
    kept in cfg.deferred_errors, shown by --list and enforced by --check.
    """

    def __init__(self, cfg, initial=()):
        super().__init__(initial)
        self.cfg = cfg
        self.deferred = False

    def append(self, message):
        if self.deferred:
            self.cfg.deferred_errors.append(message)
        else:
            super().append(message)


def _parse_notify(data, errors, warnings):
    """The notify section; None means notifications are entirely off."""
    raw = data.get("notify")
    if raw is None or raw is False:
        return None
    if not isinstance(raw, dict):
        errors.append("config.notify: expected a mapping")
        return None
    _check_keys(raw, "notify", "config.notify", warnings)

    if "on" not in raw:
        on = ["failure"]
    elif raw["on"] is None:
        on = []                                   # `on:` with no value = off
    else:
        on = []
        for index, item in enumerate(_as_list(raw["on"], "config.notify.on", errors)):
            item = item.strip().lower()
            if item in ("", "none", "off", "false"):
                continue
            if item not in NOTIFY_ON:
                errors.append(f"config.notify.on[{index}]: expected one of "
                              f"{', '.join(NOTIFY_ON)}, got {item!r}")
                continue
            if item not in on:
                on.append(item)

    retries = _as_int(raw.get("retries"), "config.notify.retries", errors,
                      0, minimum=0)
    retry_delay = _as_int(raw.get("retry_delay"), "config.notify.retry_delay",
                          errors, 3, minimum=0)
    timeout = _as_int(raw.get("timeout"), "config.notify.timeout", errors,
                      10, minimum=1)
    if timeout is not None and timeout > 300:
        errors.append("config.notify.timeout: must be <= 300 seconds")
        timeout = 300

    mail = None
    if raw.get("mail") is not None:
        m = raw["mail"]
        if not isinstance(m, dict):
            errors.append("config.notify.mail: expected a mapping")
        else:
            _check_keys(m, "mail", "config.notify.mail", warnings)
            mail = MailCfg(
                to=_as_list(m.get("to"), "config.notify.mail.to", errors),
                sender=_as_str(m.get("from"), "config.notify.mail.from",
                               errors) or "",
                smtp_host=_as_str(m.get("smtp_host"),
                                  "config.notify.mail.smtp_host",
                                  errors) or "localhost",
                port=_as_int(m.get("port"), "config.notify.mail.port", errors,
                             25, minimum=1) or 25,
                tls=_as_bool(m.get("tls"), "config.notify.mail.tls", errors),
                user_env=_as_str(m.get("user_env"),
                                 "config.notify.mail.user_env", errors) or "",
                password_env=_as_str(m.get("password_env"),
                                     "config.notify.mail.password_env",
                                     errors) or "",
            )
            if not mail.to:
                errors.append("config.notify.mail.to: required")
            if not mail.sender:
                errors.append("config.notify.mail.from: required")
            if mail.port > 65535:
                errors.append("config.notify.mail.port: must be 1..65535")
            if mail.user_env and not mail.password_env:
                errors.append("config.notify.mail.password_env: required "
                              "when user_env is set")

    webhook = None
    if raw.get("webhook") is not None:
        w = raw["webhook"]
        if not isinstance(w, dict):
            errors.append("config.notify.webhook: expected a mapping")
        else:
            _check_keys(w, "webhook", "config.notify.webhook", warnings)
            url = _as_str(w.get("url"), "config.notify.webhook.url", errors) or ""
            if not url:
                errors.append("config.notify.webhook.url: required")
            elif not url.startswith(("http://", "https://")):
                errors.append("config.notify.webhook.url: expected http:// or https://")
            webhook = WebhookCfg(
                url=url,
                headers=_as_str_map(w.get("headers"),
                                    "config.notify.webhook.headers", errors),
            )

    telegram = None
    if raw.get("telegram") is not None:
        t = raw["telegram"]
        if not isinstance(t, dict):
            errors.append("config.notify.telegram: expected a mapping")
        else:
            _check_keys(t, "telegram", "config.notify.telegram", warnings)
            chat = t.get("chat_id")
            if isinstance(chat, int) and not isinstance(chat, bool):
                chat = str(chat)
            chat_id = _as_str(chat, "config.notify.telegram.chat_id", errors) or ""
            token_env = _as_str(t.get("token_env"),
                                "config.notify.telegram.token_env",
                                errors) or ""
            if not chat_id:
                errors.append("config.notify.telegram.chat_id: required")
            if not token_env:
                errors.append("config.notify.telegram.token_env: required")
            telegram = TelegramCfg(token_env=token_env, chat_id=chat_id)

    return NotifyCfg(on=on, retries=retries or 0,
                     retry_delay=retry_delay if retry_delay is not None else 3,
                     timeout=timeout if timeout is not None else 10,
                     mail=mail, webhook=webhook, telegram=telegram)


def load_config(path, env=None, defaults=None, strict=False):
    """Read, merge with the defaults file and validate a config.

    Raises ConfigError with every problem found, not just the first one.
    Sections with enabled: false are parsed but not enforced unless strict
    (--check, or a run that would actually execute them).
    """
    path = Path(path)
    if env is not None:
        os.environ.update(env)
    if defaults and not Path(defaults).is_file():
        raise ConfigError(f"{defaults}: defaults file not found")

    task = _read_yaml(path)
    chosen = find_defaults(defaults)
    raw = task
    if chosen is not None:
        if not chosen.is_file():
            raise ConfigError(f"{chosen}: defaults file not found")
        raw = deep_merge(_read_yaml(chosen), task)
    # What the user actually wrote (built-ins excluded): lets the CLI tell
    # "the section is absent" from "the section is present but disabled".
    present_sections = set(raw)
    data = deep_merge(BUILTIN_DEFAULTS, raw)

    fatal, warnings = [], []
    _check_keys(data, "root", "config", warnings)
    data = _expand_env(data, "config", fatal)

    project = _as_str(data.get("project"), "config.project", fatal,
                      required=True) or path.stem
    cfg = Config(path=path, project=project,
                 defaults_path=chosen, present_sections=present_sections,
                 warnings=warnings)
    errors = _Errors(cfg, fatal)

    log_raw = data.get("log") or {}
    if not isinstance(log_raw, dict):
        errors.append("config.log: expected a mapping")
        log_raw = {}
    _check_keys(log_raw, "log", "config.log", warnings)
    level = (_as_str(log_raw.get("level"), "config.log.level", errors,
                     _default("log", "level")) or "info").lower()
    if level not in ("debug", "info", "warning", "error"):
        errors.append(f"config.log.level: unknown level {level!r}")
        level = "info"
    cfg.log_level = level
    cfg.log_file = _as_str(log_raw.get("file"), "config.log.file", errors)

    syslog = log_raw.get("syslog")
    if syslog is None:
        cfg.log_syslog = _default("log", "syslog")
    elif isinstance(syslog, bool):
        cfg.log_syslog = syslog
    elif isinstance(syslog, str):
        if not syslog.strip():
            errors.append("config.log.syslog: expected true/false or a socket path")
            cfg.log_syslog = False
        else:
            cfg.log_syslog = syslog.strip()
    else:
        errors.append("config.log.syslog: expected true/false or a socket path, "
                      f"got {type(syslog).__name__}")
        cfg.log_syslog = False

    facility = (_as_str(log_raw.get("facility"), "config.log.facility", errors,
                        _default("log", "facility")) or "daemon").lower()
    if facility not in SYSLOG_FACILITIES:
        errors.append(f"config.log.facility: unknown facility {facility!r} "
                      f"(allowed: {', '.join(SYSLOG_FACILITIES)})")
        facility = "daemon"
    cfg.log_facility = facility

    rt = data.get("runtime") or {}
    if not isinstance(rt, dict):
        errors.append("config.runtime: expected a mapping")
        rt = {}
    _check_keys(rt, "runtime", "config.runtime", warnings)
    cfg.temp_dir = Path(_as_str(rt.get("temp_dir"), "config.runtime.temp_dir", errors,
                                _default("runtime", "temp_dir")))
    cfg.keep_temp = _as_bool(rt.get("keep_temp"), "config.runtime.keep_temp", errors)
    cfg.verbose = _as_bool(rt.get("verbose"), "config.runtime.verbose", errors,
                           _default("runtime", "verbose"))
    cfg.debug = _as_bool(rt.get("debug"), "config.runtime.debug", errors,
                         _default("runtime", "debug"))
    cfg.hook_timeout = _as_int(rt.get("hook_timeout"),
                               "config.runtime.hook_timeout", errors,
                               _default("runtime", "hook_timeout"), minimum=1)
    cfg.notify = _parse_notify(data, errors, warnings)

    rl = data.get("rclone") or {}
    if not isinstance(rl, dict):
        errors.append("config.rclone: expected a mapping")
        rl = {}
    _check_keys(rl, "rclone", "config.rclone", warnings)
    cfg.rclone = RcloneCfg(
        binary=_as_str(rl.get("binary"), "config.rclone.binary", errors,
                       _default("rclone", "binary")),
        args=_as_list(rl.get("args"), "config.rclone.args", errors),
        env=_as_env(rl.get("env"), "config.rclone.env", errors),
        s3_options=_as_s3_options(rl.get("s3-custom-options"),
                                  "config.rclone.s3-custom-options", errors),
    )

    up_raw = data.get("upstreams") or {}
    if not isinstance(up_raw, dict):
        errors.append("config.upstreams: expected a mapping")
        up_raw = {}
    cfg.upstreams = _as_upstreams(up_raw, "config.upstreams", errors, warnings)
    if not cfg.upstreams:
        errors.append("config.upstreams: at least one upstream is required "
                      "(name: {config, env, args, s3-custom-options})")

    db = data.get("database") or {}
    if not isinstance(db, dict):
        (errors if strict else cfg.deferred_errors).append(
            "config.database: expected a mapping")
        db = {}
    _check_keys(db, "database", "config.database", warnings)
    db_enabled = _as_bool(db.get("enabled"), "config.database.enabled", errors)
    errors.deferred = not (db_enabled or strict)
    retention_mode = _as_str(db.get("retention_mode"), "config.database.retention_mode", errors)
    if retention_mode not in (None, "keep", "max_age"):
        errors.append("config.database.retention_mode: expected keep or max_age")
        retention_mode = None
    db_mode = (_as_str(db.get("mode"), "config.database.mode", errors,
                       _default("database", "mode")) or "copy").lower()
    if db_mode != "copy":
        errors.append(f"config.database.mode: only 'copy' is supported in the config "
                      f"(use -m sync on the command line), got {db_mode!r}")
        db_mode = "copy"
    db_type = (_as_str(db.get("type"), "config.database.type", errors,
                       _default("database", "type")) or "mysql").lower()
    if db_type not in DB_TYPES:
        errors.append(f"config.database.type: unsupported {db_type!r} (allowed: {', '.join(DB_TYPES)})")
    compressor = (_as_str(db.get("compressor"), "config.database.compressor", errors,
                          _default("database", "compressor")) or "zstd").lower()
    if compressor not in COMPRESSORS:
        errors.append(f"config.database.compressor: unsupported {compressor!r} (allowed: {', '.join(COMPRESSORS)})")
    targets = _as_targets(db.get("targets"), "config.database.targets", errors,
                          cfg.upstreams, types=("rclone",))
    retention = {}
    ret_raw = db.get("retention") or {}
    if not isinstance(ret_raw, dict):
        errors.append("config.database.retention: expected a mapping")
        ret_raw = {}
    for key in ret_raw:
        if key not in RETENTION_CLASSES:
            errors.append(f"config.database.retention.{key}: unknown class "
                          f"(allowed: {', '.join(RETENTION_CLASSES)})")
    for cls in RETENTION_CLASSES:
        retention[cls] = _parse_retention(ret_raw.get(cls),
                                          f"config.database.retention.{cls}",
                                          errors, warnings, retention_mode)
    cfg.database = DatabaseCfg(
        enabled=db_enabled,
        mode=db_mode,
        type=db_type,
        host=_as_str(db.get("host"), "config.database.host", errors,
                     _default("database", "host")) or "localhost",
        port=_as_int(db.get("port"), "config.database.port", errors, minimum=1),
        user=_as_str(db.get("user"), "config.database.user", errors),
        password_env=_as_str(db.get("password_env"), "config.database.password_env", errors),
        args=_as_list(db.get("args"), "config.database.args", errors),
        compressor=compressor,
        compressor_args=_as_list(db.get("compressor_args"), "config.database.compressor_args", errors),
        targets=targets,
        databases=_as_list(db.get("databases"), "config.database.databases", errors),
        retention=retention,
        catch_up=_as_bool(db.get("catch_up"), "config.database.catch_up", errors),
        ignore_table=_as_list(db.get("ignore_table"),
                              "config.database.ignore_table", errors),
        before=_as_list(db.get("before"), "config.database.before", errors),
        after=_as_list(db.get("after"), "config.database.after", errors),
        s3_options=_as_s3_options(db.get("s3-custom-options"),
                                  "config.database.s3-custom-options", errors),
    )
    if cfg.database.ignore_table and cfg.database.type == "sqlite":
        warnings.append("config.database.ignore_table: sqlite has no table "
                        "selection, ignored")
    errors.deferred = False
    if cfg.database.enabled and not cfg.database.databases:
        errors.append("config.database.databases: required when database.enabled is true")
    if cfg.database.enabled and not cfg.database.targets:
        errors.append("config.database.targets: required when database.enabled is true")
    if cfg.database.enabled and not any(cfg.database.retention.values()):
        errors.append("config.database.retention: at least one class must be configured")

    st = data.get("storage") or {}
    if not isinstance(st, dict):
        (errors if strict else cfg.deferred_errors).append(
            "config.storage: expected a mapping")
        st = {}
    _check_keys(st, "storage", "config.storage", warnings)
    st_enabled = _as_bool(st.get("enabled"), "config.storage.enabled", errors)
    errors.deferred = not (st_enabled or strict)
    cfg.storage = StorageCfg(
        enabled=st_enabled,
        mode=_as_str(st.get("mode"), "config.storage.mode", errors,
                     _default("storage", "mode")) or "sync",
        targets=_as_targets(st.get("targets"), "config.storage.targets", errors,
                            cfg.upstreams, types=("rclone",)),
        sources=_as_sources(st.get("sources"), "config.storage.sources",
                            errors, warnings),
        allow_empty=_as_bool(st.get("allow_empty"),
                             "config.storage.allow_empty", errors),
        before=_as_list(st.get("before"), "config.storage.before", errors),
        after=_as_list(st.get("after"), "config.storage.after", errors),
        s3_options=_as_s3_options(st.get("s3-custom-options"),
                                  "config.storage.s3-custom-options", errors),
    )
    errors.deferred = False
    if cfg.storage.enabled and (not cfg.storage.targets or not cfg.storage.sources):
        errors.append("config.storage: targets and sources are required when enabled")

    ar = data.get("archive") or {}
    if not isinstance(ar, dict):
        (errors if strict else cfg.deferred_errors).append(
            "config.archive: expected a mapping")
        ar = {}
    _check_keys(ar, "archive", "config.archive", warnings)
    ar_enabled = _as_bool(ar.get("enabled"), "config.archive.enabled", errors)
    errors.deferred = not (ar_enabled or strict)
    archive_mode = (_as_str(ar.get("mode"), "config.archive.mode", errors,
                            _default("archive", "mode")) or "copy").lower()
    if archive_mode != "copy":
        errors.append(f"config.archive.mode: only 'copy' is supported in the config "
                      f"(use -m sync on the command line), got {archive_mode!r}")
        archive_mode = "copy"
    ar_mode = _as_str(ar.get("retention_mode"), "config.archive.retention_mode", errors)
    if ar_mode not in (None, "keep", "max_age"):
        errors.append("config.archive.retention_mode: expected keep or max_age")
        ar_mode = None
    cfg.archive = ArchiveCfg(
        enabled=ar_enabled,
        mode=archive_mode,
        compressor=(_as_str(ar.get("compressor"), "config.archive.compressor", errors,
                            _default("archive", "compressor")) or "zstd").lower(),
        compressor_args=_as_list(ar.get("compressor_args"), "config.archive.compressor_args", errors),
        name=_as_str(ar.get("name"), "config.archive.name", errors) or cfg.project,
        root=_as_str(ar.get("root"), "config.archive.root", errors) or "",
        include=_as_list(ar.get("include"), "config.archive.include", errors),
        exclude=_as_list(ar.get("exclude"), "config.archive.exclude", errors),
        targets=_as_targets(ar.get("targets"), "config.archive.targets", errors,
                            cfg.upstreams, types=("rclone",)),
        retention=_parse_retention(ar.get("retention"), "config.archive.retention",
                                   errors, warnings, ar_mode),
        before=_as_list(ar.get("before"), "config.archive.before", errors),
        after=_as_list(ar.get("after"), "config.archive.after", errors),
        s3_options=_as_s3_options(ar.get("s3-custom-options"),
                                  "config.archive.s3-custom-options", errors),
    )
    errors.deferred = False
    if cfg.archive.enabled and (not cfg.archive.targets or not cfg.archive.root):
        errors.append("config.archive: targets and root are required when enabled")

    inc = data.get("incremental") or {}
    if not isinstance(inc, dict):
        (errors if strict else cfg.deferred_errors).append(
            "config.incremental: expected a mapping")
        inc = {}
    _check_keys(inc, "incremental", "config.incremental", warnings)
    inc_enabled = _as_bool(inc.get("enabled"), "config.incremental.enabled", errors)
    errors.deferred = not (inc_enabled or strict)
    initial_day = _as_int(inc.get("initial_day"), "config.incremental.initial_day",
                          errors, _default("incremental", "initial_day"), minimum=1)
    if initial_day is not None and initial_day > 28:
        errors.append("config.incremental.initial_day: must be between 1 and 28")
    cfg.incremental = IncrementalCfg(
        enabled=inc_enabled,
        name=_as_str(inc.get("name"), "config.incremental.name", errors) or cfg.project,
        root=_as_str(inc.get("root"), "config.incremental.root", errors) or "",
        include=_as_list(inc.get("include"), "config.incremental.include", errors),
        exclude=_as_list(inc.get("exclude"), "config.incremental.exclude", errors),
        targets=_as_targets(inc.get("targets"), "config.incremental.targets",
                            errors, cfg.upstreams, types=("rclone",)),
        initial_day=initial_day if initial_day is not None else 1,
        snapshot=_as_str(inc.get("snapshot"), "config.incremental.snapshot", errors) or "",
        retention=_parse_retention(inc.get("retention"), "config.incremental.retention",
                                   errors, warnings),
        before=_as_list(inc.get("before"), "config.incremental.before", errors),
        after=_as_list(inc.get("after"), "config.incremental.after", errors),
        s3_options=_as_s3_options(inc.get("s3-custom-options"),
                                  "config.incremental.s3-custom-options", errors),
    )
    errors.deferred = False
    if cfg.incremental.enabled:
        if not cfg.incremental.root:
            errors.append("config.incremental.root: required when incremental.enabled is true")
        if not cfg.incremental.targets:
            errors.append("config.incremental.targets: required when incremental.enabled is true")
        if not cfg.incremental.snapshot:
            errors.append("config.incremental.snapshot: required when incremental.enabled is true "
                          "(GNU tar snapshot file, e.g. /var/lib/kwbackup/project.snar)")
        if not cfg.incremental.retention:
            errors.append("config.incremental.retention: required when incremental.enabled "
                          "is true (keep: N chains or max_age)")

    vlt = data.get("vault") or {}
    if not isinstance(vlt, dict):
        errors.append("config.vault: expected a mapping")
        vlt = {}
    _check_keys(vlt, "vault", "config.vault", warnings)
    vault_enabled = _as_bool(vlt.get("enabled"), "config.vault.enabled", errors)
    errors.deferred = not (vault_enabled or strict)

    default_volume = _as_size(_default("vault", "volume_size"),
                              "config.vault.volume_size", errors, 2 * 1024 ** 3)
    volume_size = _as_size(vlt.get("volume_size"), "config.vault.volume_size",
                           errors, default_volume)
    if volume_size is not None and volume_size <= 0:
        errors.append("config.vault.volume_size: must be > 0")
        volume_size = default_volume
    checksum = (_as_str(vlt.get("checksum"), "config.vault.checksum", errors,
                        _default("vault", "checksum")) or "none").lower()
    if checksum not in ("none", "sha256"):
        errors.append(f"config.vault.checksum: expected none or sha256, got {checksum!r}")
        checksum = "none"

    journal_raw = vlt.get("journal")
    journal = None
    if journal_raw is False:
        journal = JournalCfg(type="scan")
    elif journal_raw is not None:
        if not isinstance(journal_raw, dict):
            errors.append("config.vault.journal: expected a mapping")
        else:
            _check_keys(journal_raw, "journal", "config.vault.journal", warnings)
            jtype = (_as_str(journal_raw.get("type"), "config.vault.journal.type",
                             errors) or "txt").lower()
            if jtype not in ("txt", "scan"):
                errors.append("config.vault.journal.type: expected txt or scan, "
                              f"got {jtype!r}")
                jtype = "txt"
            period = (_as_str(journal_raw.get("period"),
                              "config.vault.journal.period", errors)
                      or "daily").lower()
            if period != "daily":
                errors.append("config.vault.journal.period: only daily is supported")
            path = _as_str(journal_raw.get("path"), "config.vault.journal.path",
                           errors) or ""
            keep = _as_timedelta(journal_raw.get("keep_done"),
                                 "config.vault.journal.keep_done", errors,
                                 dt.timedelta(days=7))
            if jtype == "txt":
                if not path:
                    errors.append("config.vault.journal.path: required for type txt")
                journal = JournalCfg(type="txt", period="daily", path=path,
                                     keep_done=keep)
            else:
                for key in ("period", "path", "keep_done"):
                    if key in journal_raw:
                        warnings.append(f"config.vault.journal.{key}: "
                                        "not used with type scan")
                journal = JournalCfg(type="scan")

    restore_raw = vlt.get("restore") or {}
    if not isinstance(restore_raw, dict):
        errors.append("config.vault.restore: expected a mapping")
        restore_raw = {}
    _check_keys(restore_raw, "restore", "config.vault.restore", warnings)
    order = (_as_str(restore_raw.get("order"),
                     "config.vault.restore.order", errors) or "desc").lower()
    if order not in ("asc", "desc"):
        errors.append("config.vault.restore.order: expected asc or desc")
        order = "desc"
    priority = _as_list(restore_raw.get("priority"),
                        "config.vault.restore.priority", errors)
    chown = _as_bool(restore_raw.get("chown"), "config.vault.restore.chown",
                     errors)

    state_dir = _as_str(vlt.get("state_dir"), "config.vault.state_dir", errors)
    if not state_dir:
        state_dir = str(Path("/var/lib/kwbackup") / cfg.project / "vault")
    cfg.vault = VaultCfg(
        enabled=vault_enabled,
        root=_as_str(vlt.get("root"), "config.vault.root", errors) or "",
        state_dir=state_dir,
        volume_size=volume_size if volume_size is not None else default_volume,
        checksum=checksum,
        journal=journal,
        restore=RestoreCfg(order=order, priority=priority, chown=chown),
        targets=_as_targets(vlt.get("targets"), "config.vault.targets", errors,
                            cfg.upstreams, types=("rclone", "rsync")),
        before=_as_list(vlt.get("before"), "config.vault.before", errors),
        after=_as_list(vlt.get("after"), "config.vault.after", errors),
    )
    if cfg.vault.enabled:
        if not cfg.vault.root:
            errors.append("config.vault.root: required when vault.enabled is true")
        if not cfg.vault.targets:
            errors.append("config.vault.targets: required when vault.enabled is true")
    errors.deferred = False

    if errors:
        raise ConfigError("invalid config:\n  - " + "\n  - ".join(errors))
    return cfg


UPSTREAM_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
TARGET_RE = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*):(?P<path>.*)$")


def target_join(target, *parts):
    """'s3a:/DB' + 'DAILY' -> 's3a:/DB/DAILY' (remote prefix kept)."""
    name, _, path = target.partition(":")
    bits = [path.strip("/")] + [str(p).strip("/") for p in parts if p]
    tail = "/".join(bit for bit in bits if bit)
    return f"{name}:/{tail}" if tail else f"{name}:/"


def _as_upstreams(value, path, errors, warnings):
    if value is None:
        return {}
    if not isinstance(value, dict):
        errors.append(f"{path}: expected a mapping of upstream name -> settings")
        return {}
    out = {}
    for name, raw in value.items():
        item = f"{path}.{name}"
        if not isinstance(name, str) or not UPSTREAM_NAME_RE.match(name):
            errors.append(f"{item}: bad upstream name")
            continue
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            errors.append(f"{item}: expected a mapping")
            continue
        _check_keys(raw, "upstream", item, warnings)
        kind = (_as_str(raw.get("type"), f"{item}.type", errors) or "rclone").lower()
        if kind not in ("rclone", "rsync"):
            errors.append(f"{item}.type: expected rclone or rsync, got {kind!r}")
            kind = "rclone"
        bwlimit = _as_size(raw.get("bwlimit"), f"{item}.bwlimit", errors, 0) or 0
        host = _as_str(raw.get("host"), f"{item}.host", errors) or ""
        ssh_options = _as_list(raw.get("ssh_options"), f"{item}.ssh_options", errors)
        if kind == "rsync":
            if not host:
                errors.append(f"{item}.host: required for type rsync (user@host)")
            for key in ("config", "args", "env", "s3-custom-options"):
                if raw.get(key) is not None:
                    errors.append(f"{item}.{key}: only for type rclone")
            out[name] = UpstreamCfg(name=name, type="rsync", host=host,
                                    ssh_options=ssh_options, bwlimit=bwlimit)
        else:
            if host or ssh_options:
                errors.append(f"{item}.host/ssh_options: only for type rsync")
            out[name] = UpstreamCfg(
                name=name, type="rclone",
                config=_as_str(raw.get("config"), f"{item}.config", errors),
                args=_as_list(raw.get("args"), f"{item}.args", errors),
                env=_as_env(raw.get("env"), f"{item}.env", errors),
                s3_options=_as_s3_options(raw.get("s3-custom-options"),
                                          f"{item}.s3-custom-options", errors),
                bwlimit=bwlimit,
            )
    return out


def _as_targets(value, path, errors, upstreams, types=None):
    if value is None:
        return []
    if not isinstance(value, list):
        errors.append(f"{path}: expected a list, e.g. ['s3a:/DB', 's3b:/DB']")
        return []
    out = []
    for i, item in enumerate(value):
        where = f"{path}[{i}]"
        if not isinstance(item, str):
            errors.append(f"{where}: expected 'upstream:path'")
            continue
        match = TARGET_RE.match(item)
        if not match:
            errors.append(f"{where}: expected 'upstream:path', got {item!r}")
            continue
        upstream = upstreams.get(match.group("name"))
        if upstream is None:
            errors.append(f"{where}: unknown upstream {match.group('name')!r} "
                          f"(defined: {', '.join(upstreams) or 'none'})")
            continue
        if types and upstream.type not in types:
            errors.append(f"{where}: upstream {upstream.name!r} has type "
                          f"{upstream.type}; this task needs {', '.join(types)}")
            continue
        out.append(item)
    return out


def _as_sources(value, path, errors, warnings):
    """Storage sources: "path" or {path: ..., dest: ...}."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        errors.append(f"{path}: expected a list of paths or {{path, dest}} mappings")
        return []
    out = []
    for i, item in enumerate(value):
        item_path = f"{path}[{i}]"
        if isinstance(item, str):
            out.append((item, None))
        elif isinstance(item, dict):
            _check_keys(item, "source", item_path, warnings)
            src = _as_str(item.get("path"), f"{item_path}.path", errors, required=True)
            dest = _as_str(item.get("dest"), f"{item_path}.dest", errors)
            if src:
                out.append((src, dest))
        else:
            errors.append(f"{item_path}: expected a string or a mapping")
    return out


S3_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _as_s3_options(value, path, errors):
    """rclone backend flags: s3-chunk-size: 5M -> --s3-chunk-size=5M.

    The set of flags is open (rclone has hundreds), so names are not checked
    against a list - only the shape: no spaces, no leading dash. A null value
    removes an option inherited from rclone.s3-custom-options.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        errors.append(f"{path}: expected a mapping of rclone flags")
        return {}
    out = {}
    for key, val in value.items():
        if not isinstance(key, str) or not S3_KEY_RE.match(key):
            errors.append(f"{path}: bad option name {key!r} "
                          f"(letters, digits, dot, dash and underscore)")
            continue
        if isinstance(val, bool) or isinstance(val, (int, float, str)) or val is None:
            out[key] = val
        else:
            errors.append(f"{path}.{key}: expected a string, number, boolean or null")
    return out


def _as_env(value, path, errors):
    if value is None:
        return {}
    if not isinstance(value, dict):
        errors.append(f"{path}: expected a mapping of NAME: value")
        return {}
    return {str(k): str(v) for k, v in value.items()}


