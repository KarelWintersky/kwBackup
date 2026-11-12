# ============================================================================
# CONFIG
# ============================================================================

class ConfigError(Exception):
    pass


# Validation schema: the allowed keys of every section. Anything else is a
# config error with the list of valid keys, so a typo cannot silently
# disable a section (`databeses:` would otherwise be ignored and the backup
# would run with no databases at all).
SECTIONS = {
    "root": {"version", "project", "log", "runtime", "rclone",
             "database", "storage", "archive", "security"},
    "log": {"level", "file"},
    "runtime": {"temp_dir", "keep_temp"},
    "rclone": {"binary", "config", "remote", "args", "env"},
    "database": {"enabled", "type", "host", "port", "user", "password_env",
                 "args", "compressor", "compressor_args", "target", "mode",
                 "databases", "retention", "retention_mode", "catch_up"},
    "storage": {"enabled", "mode", "target", "sources"},
    "archive": {"enabled", "compressor", "compressor_args", "name", "root",
                "include", "exclude", "target", "mode", "retention",
                "retention_mode"},
    "security": {"check_excessive_access_rights"},
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
class SecurityCfg:
    # Warn when a file that carries S3 credentials is readable by
    # group/other. Some setups mount configs read-only with fixed modes;
    # they can switch this off.
    check_excessive_access_rights: bool = _default("security", "check_excessive_access_rights")


@dataclass
class RcloneCfg:
    binary: str = _default("rclone", "binary")
    config: str | None = None
    remote: str = ""
    args: list = field(default_factory=list)
    env: dict = field(default_factory=dict)


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
    target: str = ""
    databases: list = field(default_factory=list)
    retention: dict = field(default_factory=dict)      # class -> Retention | None
    catch_up: bool = _default("database", "catch_up")


@dataclass
class StorageCfg:
    enabled: bool = False
    mode: str = _default("storage", "mode")
    target: str = ""
    sources: list = field(default_factory=list)   # [(path, dest|None), ...]


@dataclass
class ArchiveCfg:
    enabled: bool = False
    mode: str = _default("archive", "mode")
    compressor: str = _default("archive", "compressor")
    compressor_args: list = field(default_factory=list)
    name: str = _default("archive", "name")
    root: str = ""
    include: list = field(default_factory=lambda: _default("archive", "include"))
    exclude: list = field(default_factory=list)
    target: str = ""
    retention: Retention | None = None


@dataclass
class Config:
    path: Path
    project: str
    defaults_path: Path | None = None
    secret_files: list = field(default_factory=list)   # [(label, path)]
    log_level: str = _default("log", "level")
    log_file: str | None = None
    temp_dir: Path = Path(_default("runtime", "temp_dir"))
    keep_temp: bool = _default("runtime", "keep_temp")
    rclone: RcloneCfg = field(default_factory=RcloneCfg)
    security: SecurityCfg = field(default_factory=SecurityCfg)
    database: DatabaseCfg = field(default_factory=DatabaseCfg)
    storage: StorageCfg = field(default_factory=StorageCfg)
    archive: ArchiveCfg = field(default_factory=ArchiveCfg)


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


def _has_credentials(data):
    """Does THIS file (before merging) carry S3 keys in rclone.args?"""
    rclone = data.get("rclone") or {}
    args = rclone.get("args") if isinstance(rclone, dict) else None
    if not isinstance(args, list):
        return False
    return any("secret" in str(arg).lower() or "access-key" in str(arg).lower()
               for arg in args)


def secret_warnings(cfg):
    """Files that pass S3 credentials as rclone flags must not be readable
    by group/other.

    Keys inside rclone.conf are a normal setup (account-level credentials);
    they are not flagged: rclone flags in the task config override them.

    Controlled by security.check_excessive_access_rights (default: on).
    """
    if not cfg.security.check_excessive_access_rights:
        return []
    files = list(cfg.secret_files)
    warnings = []
    seen = set()
    for label, path in files:
        if str(path) in seen:
            continue
        seen.add(str(path))
        try:
            mode = Path(path).stat().st_mode & 0o777
        except OSError:
            continue
        if mode & 0o077:
            warnings.append(f"{label} {path} contains credentials but its mode is "
                            f"{mode:03o}; chmod 600 {path}")
    return warnings


def _check_keys(data, section, path, errors):
    allowed = SECTIONS[section]
    for key in data:
        if key not in allowed:
            errors.append(f"{path}.{key}: unknown key (allowed: {', '.join(sorted(allowed))})")


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


def _parse_retention(value, path, errors, mode_hint=None):
    """Parse one retention class. Returns Retention or None (class disabled)."""
    if value is None or value is False:
        return None
    if value is True:
        errors.append(f"{path}: true is not a retention rule; use {{keep: N}} or {{max_age: 7d}}")
        return None
    if not isinstance(value, dict):
        errors.append(f"{path}: expected {{keep: N}} or {{max_age: 7d}}")
        return None
    _check_keys(value, "retention", path, errors)
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
    secret_files = [("config", path)] if _has_credentials(task) else []
    data = BUILTIN_DEFAULTS
    if chosen is not None:
        if not chosen.is_file():
            raise ConfigError(f"{chosen}: defaults file not found")
        defaults_data = _read_yaml(chosen)
        if _has_credentials(defaults_data):
            secret_files.append(("defaults", chosen))
        data = deep_merge(BUILTIN_DEFAULTS, defaults_data)
    data = deep_merge(data, task)

    errors = []
    _check_keys(data, "root", "config", errors)
    if data.get("version") != 1:
        errors.append("config.version: must be 1")
    data = _expand_env(data, "config", errors)

    cfg = Config(path=path, project=_as_str(data.get("project"), "config.project",
                                            errors, required=True) or path.stem,
                 defaults_path=chosen, secret_files=secret_files)

    log_raw = data.get("log") or {}
    if not isinstance(log_raw, dict):
        errors.append("config.log: expected a mapping")
        log_raw = {}
    _check_keys(log_raw, "log", "config.log", errors)
    level = (_as_str(log_raw.get("level"), "config.log.level", errors,
                     _default("log", "level")) or "info").lower()
    if level not in ("debug", "info", "warning", "error"):
        errors.append(f"config.log.level: unknown level {level!r}")
        level = "info"
    cfg.log_level = level
    cfg.log_file = _as_str(log_raw.get("file"), "config.log.file", errors)

    sec = data.get("security") or {}
    if not isinstance(sec, dict):
        errors.append("config.security: expected a mapping")
        sec = {}
    _check_keys(sec, "security", "config.security", errors)
    cfg.security = SecurityCfg(
        check_excessive_access_rights=_as_bool(
            sec.get("check_excessive_access_rights"),
            "config.security.check_excessive_access_rights", errors,
            _default("security", "check_excessive_access_rights")),
    )

    rt = data.get("runtime") or {}
    if not isinstance(rt, dict):
        errors.append("config.runtime: expected a mapping")
        rt = {}
    _check_keys(rt, "runtime", "config.runtime", errors)
    cfg.temp_dir = Path(_as_str(rt.get("temp_dir"), "config.runtime.temp_dir", errors,
                                _default("runtime", "temp_dir")))
    cfg.keep_temp = _as_bool(rt.get("keep_temp"), "config.runtime.keep_temp", errors)

    rl = data.get("rclone") or {}
    if not isinstance(rl, dict):
        errors.append("config.rclone: expected a mapping")
        rl = {}
    _check_keys(rl, "rclone", "config.rclone", errors)
    cfg.rclone = RcloneCfg(
        binary=_as_str(rl.get("binary"), "config.rclone.binary", errors,
                       _default("rclone", "binary")),
        config=_as_str(rl.get("config"), "config.rclone.config", errors),
        remote=_as_str(rl.get("remote"), "config.rclone.remote", errors, required=True) or "",
        args=_as_list(rl.get("args"), "config.rclone.args", errors),
        env=_as_env(rl.get("env"), "config.rclone.env", errors),
    )

    db = data.get("database") or {}
    if not isinstance(db, dict):
        errors.append("config.database: expected a mapping")
        db = {}
    _check_keys(db, "database", "config.database", errors)
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
    target = _as_str(db.get("target"), "config.database.target", errors) or ""
    if target and ":" not in target:
        errors.append("config.database.target: expected 'remote:path', e.g. 'beget:/DB'")
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
        retention[cls] = _parse_retention(ret_raw.get(cls), f"config.database.retention.{cls}",
                                          errors, retention_mode)
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
        target=target,
        databases=_as_list(db.get("databases"), "config.database.databases", errors),
        retention=retention,
        catch_up=_as_bool(db.get("catch_up"), "config.database.catch_up", errors),
    )
    if cfg.database.enabled and not cfg.database.databases:
        errors.append("config.database.databases: required when database.enabled is true")
    if cfg.database.enabled and not cfg.database.target:
        errors.append("config.database.target: required when database.enabled is true")
    if cfg.database.enabled and not any(cfg.database.retention.values()):
        errors.append("config.database.retention: at least one class must be configured")

    st = data.get("storage") or {}
    if not isinstance(st, dict):
        errors.append("config.storage: expected a mapping")
        st = {}
    _check_keys(st, "storage", "config.storage", errors)
    cfg.storage = StorageCfg(
        enabled=_as_bool(st.get("enabled"), "config.storage.enabled", errors),
        mode=_as_str(st.get("mode"), "config.storage.mode", errors,
                     _default("storage", "mode")) or "sync",
        target=_as_str(st.get("target"), "config.storage.target", errors) or "",
        sources=_as_sources(st.get("sources"), "config.storage.sources", errors),
    )
    if cfg.storage.enabled and (not cfg.storage.target or not cfg.storage.sources):
        errors.append("config.storage: target and sources are required when enabled")

    ar = data.get("archive") or {}
    if not isinstance(ar, dict):
        errors.append("config.archive: expected a mapping")
        ar = {}
    _check_keys(ar, "archive", "config.archive", errors)
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
        name=_as_str(ar.get("name"), "config.archive.name", errors,
                     _default("archive", "name")) or "archive",
        root=_as_str(ar.get("root"), "config.archive.root", errors) or "",
        include=_as_list(ar.get("include"), "config.archive.include", errors),
        exclude=_as_list(ar.get("exclude"), "config.archive.exclude", errors),
        target=_as_str(ar.get("target"), "config.archive.target", errors) or "",
        retention=_parse_retention(ar.get("retention"), "config.archive.retention", errors, ar_mode),
    )
    if cfg.archive.enabled and (not cfg.archive.target or not cfg.archive.root):
        errors.append("config.archive: target and root are required when enabled")

    if errors:
        raise ConfigError("invalid config:\n  - " + "\n  - ".join(errors))
    return cfg


def _as_sources(value, path, errors):
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
            _check_keys(item, "source", item_path, errors)
            src = _as_str(item.get("path"), f"{item_path}.path", errors, required=True)
            dest = _as_str(item.get("dest"), f"{item_path}.dest", errors)
            if src:
                out.append((src, dest))
        else:
            errors.append(f"{item_path}: expected a string or a mapping")
    return out


def _as_env(value, path, errors):
    if value is None:
        return {}
    if not isinstance(value, dict):
        errors.append(f"{path}: expected a mapping of NAME: value")
        return {}
    return {str(k): str(v) for k, v in value.items()}


