# ============================================================================
# CONFIG
# ============================================================================

class ConfigError(Exception):
    pass


# Validation schema: the allowed keys of every section. Anything else is a
# warning with the list of valid keys - a typo stays visible, but an old file
# with a leftover key still runs. Missing or invalid values remain fatal.
SECTIONS = {
    "root": {"project", "log", "runtime", "rclone", "upstreams",
             "database", "storage", "archive", "incremental"},
    "log": {"level", "file"},
    "runtime": {"temp_dir", "keep_temp"},
    "rclone": {"binary", "args", "env", "s3-custom-options"},
    "upstream": {"config", "args", "env", "s3-custom-options"},
    "database": {"enabled", "type", "host", "port", "user", "password_env",
                 "args", "compressor", "compressor_args", "targets", "mode",
                 "databases", "retention", "retention_mode", "catch_up",
                 "s3-custom-options"},
    "storage": {"enabled", "mode", "targets", "sources", "s3-custom-options"},
    "archive": {"enabled", "compressor", "compressor_args", "name", "root",
                "include", "exclude", "targets", "mode", "retention",
                "retention_mode", "s3-custom-options"},
    "incremental": {"enabled", "name", "root", "include", "exclude", "targets",
                    "initial_day", "snapshot", "retention", "s3-custom-options"},
    "retention": {"keep", "max_age"},
    "source": {"path", "dest"},
}

# Enumerations checked in load_config.
COMPRESSORS = ("zstd", "gzip", "pigz", "rar", "none")
DB_TYPES = ("mysql", "mariadb", "postgres", "pgsql", "sqlite")

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
    """One destination: an rclone remote name plus its own connection settings."""
    name: str
    config: str | None = None
    args: list = field(default_factory=list)
    env: dict = field(default_factory=dict)
    s3_options: dict = field(default_factory=dict)


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
    s3_options: dict = field(default_factory=dict)


@dataclass
class StorageCfg:
    enabled: bool = False
    mode: str = _default("storage", "mode")
    targets: list = field(default_factory=list)
    sources: list = field(default_factory=list)   # [(path, dest|None), ...]
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
    s3_options: dict = field(default_factory=dict)


@dataclass
class Config:
    path: Path
    project: str
    defaults_path: Path | None = None
    present_sections: set = field(default_factory=set)   # seen in defaults/task, not builtins
    warnings: list = field(default_factory=list)         # unknown keys and the like
    log_level: str = _default("log", "level")
    log_file: str | None = None
    temp_dir: Path = Path(_default("runtime", "temp_dir"))
    keep_temp: bool = _default("runtime", "keep_temp")
    rclone: RcloneCfg = field(default_factory=RcloneCfg)
    upstreams: dict = field(default_factory=dict)   # name -> UpstreamCfg
    database: DatabaseCfg = field(default_factory=DatabaseCfg)
    storage: StorageCfg = field(default_factory=StorageCfg)
    archive: ArchiveCfg = field(default_factory=ArchiveCfg)
    incremental: IncrementalCfg = field(default_factory=IncrementalCfg)


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



def load_config(path, env=None, defaults=None):
    """Read, merge with the defaults file and validate a config.

    Raises ConfigError with every problem found, not just the first one.
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

    errors, warnings = [], []
    _check_keys(data, "root", "config", warnings)
    data = _expand_env(data, "config", errors)

    cfg = Config(path=path, project=_as_str(data.get("project"), "config.project",
                                            errors, required=True) or path.stem,
                 defaults_path=chosen, present_sections=present_sections,
                 warnings=warnings)

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

    rt = data.get("runtime") or {}
    if not isinstance(rt, dict):
        errors.append("config.runtime: expected a mapping")
        rt = {}
    _check_keys(rt, "runtime", "config.runtime", warnings)
    cfg.temp_dir = Path(_as_str(rt.get("temp_dir"), "config.runtime.temp_dir", errors,
                                _default("runtime", "temp_dir")))
    cfg.keep_temp = _as_bool(rt.get("keep_temp"), "config.runtime.keep_temp", errors)

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
        errors.append("config.database: expected a mapping")
        db = {}
    _check_keys(db, "database", "config.database", warnings)
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
                          cfg.upstreams)
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
        enabled=_as_bool(db.get("enabled"), "config.database.enabled", errors),
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
        s3_options=_as_s3_options(db.get("s3-custom-options"),
                                  "config.database.s3-custom-options", errors),
    )
    if cfg.database.enabled and not cfg.database.databases:
        errors.append("config.database.databases: required when database.enabled is true")
    if cfg.database.enabled and not cfg.database.targets:
        errors.append("config.database.targets: required when database.enabled is true")
    if cfg.database.enabled and not any(cfg.database.retention.values()):
        errors.append("config.database.retention: at least one class must be configured")

    st = data.get("storage") or {}
    if not isinstance(st, dict):
        errors.append("config.storage: expected a mapping")
        st = {}
    _check_keys(st, "storage", "config.storage", warnings)
    cfg.storage = StorageCfg(
        enabled=_as_bool(st.get("enabled"), "config.storage.enabled", errors),
        mode=_as_str(st.get("mode"), "config.storage.mode", errors,
                     _default("storage", "mode")) or "sync",
        targets=_as_targets(st.get("targets"), "config.storage.targets", errors,
                            cfg.upstreams),
        sources=_as_sources(st.get("sources"), "config.storage.sources",
                            errors, warnings),
        s3_options=_as_s3_options(st.get("s3-custom-options"),
                                  "config.storage.s3-custom-options", errors),
    )
    if cfg.storage.enabled and (not cfg.storage.targets or not cfg.storage.sources):
        errors.append("config.storage: targets and sources are required when enabled")

    ar = data.get("archive") or {}
    if not isinstance(ar, dict):
        errors.append("config.archive: expected a mapping")
        ar = {}
    _check_keys(ar, "archive", "config.archive", warnings)
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
        enabled=_as_bool(ar.get("enabled"), "config.archive.enabled", errors),
        mode=archive_mode,
        compressor=(_as_str(ar.get("compressor"), "config.archive.compressor", errors,
                            _default("archive", "compressor")) or "zstd").lower(),
        compressor_args=_as_list(ar.get("compressor_args"), "config.archive.compressor_args", errors),
        name=_as_str(ar.get("name"), "config.archive.name", errors) or cfg.project,
        root=_as_str(ar.get("root"), "config.archive.root", errors) or "",
        include=_as_list(ar.get("include"), "config.archive.include", errors),
        exclude=_as_list(ar.get("exclude"), "config.archive.exclude", errors),
        targets=_as_targets(ar.get("targets"), "config.archive.targets", errors,
                            cfg.upstreams),
        retention=_parse_retention(ar.get("retention"), "config.archive.retention",
                                   errors, warnings, ar_mode),
        s3_options=_as_s3_options(ar.get("s3-custom-options"),
                                  "config.archive.s3-custom-options", errors),
    )
    if cfg.archive.enabled and (not cfg.archive.targets or not cfg.archive.root):
        errors.append("config.archive: targets and root are required when enabled")

    inc = data.get("incremental") or {}
    if not isinstance(inc, dict):
        errors.append("config.incremental: expected a mapping")
        inc = {}
    _check_keys(inc, "incremental", "config.incremental", warnings)
    initial_day = _as_int(inc.get("initial_day"), "config.incremental.initial_day",
                          errors, _default("incremental", "initial_day"), minimum=1)
    if initial_day is not None and initial_day > 28:
        errors.append("config.incremental.initial_day: must be between 1 and 28")
    cfg.incremental = IncrementalCfg(
        enabled=_as_bool(inc.get("enabled"), "config.incremental.enabled", errors),
        name=_as_str(inc.get("name"), "config.incremental.name", errors) or cfg.project,
        root=_as_str(inc.get("root"), "config.incremental.root", errors) or "",
        include=_as_list(inc.get("include"), "config.incremental.include", errors),
        exclude=_as_list(inc.get("exclude"), "config.incremental.exclude", errors),
        targets=_as_targets(inc.get("targets"), "config.incremental.targets",
                            errors, cfg.upstreams),
        initial_day=initial_day if initial_day is not None else 1,
        snapshot=_as_str(inc.get("snapshot"), "config.incremental.snapshot", errors) or "",
        retention=_parse_retention(inc.get("retention"), "config.incremental.retention",
                                   errors, warnings),
        s3_options=_as_s3_options(inc.get("s3-custom-options"),
                                  "config.incremental.s3-custom-options", errors),
    )
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
        out[name] = UpstreamCfg(
            name=name,
            config=_as_str(raw.get("config"), f"{item}.config", errors),
            args=_as_list(raw.get("args"), f"{item}.args", errors),
            env=_as_env(raw.get("env"), f"{item}.env", errors),
            s3_options=_as_s3_options(raw.get("s3-custom-options"),
                                      f"{item}.s3-custom-options", errors),
        )
    return out


def _as_targets(value, path, errors, upstreams):
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
        if match.group("name") not in upstreams:
            errors.append(f"{where}: unknown upstream {match.group('name')!r} "
                          f"(defined: {', '.join(upstreams) or 'none'})")
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


