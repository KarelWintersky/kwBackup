#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kwbackup — backup tool: database dumps, storage sync, file archive.

Single file, standard library only. Drop-in replacement for kwbackup.sh.

    kwbackup.py -c project.yaml -d              # database backup
    kwbackup.py -c project.yaml -d -s -a        # all actions
    kwbackup.py -c project.yaml --check-config  # validate config, exit
    kwbackup.py -c project.yaml --list          # what is actually in the cloud
    kwbackup.py -c project.yaml --verify        # download latest and test it
    kwbackup.py --selftest                      # internal unit tests

Exit codes: 0 ok, 1 runtime error, 2 config error, 3 usage error,
            4 already running (lock), 5 verification failed.
"""

VERSION = "1.0.0-dev"
PROG = "kwbackup"

import argparse
import contextlib
import datetime as dt
import fcntl
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

EXIT_OK, EXIT_ERROR, EXIT_CONFIG, EXIT_USAGE, EXIT_LOCKED, EXIT_VERIFY = 0, 1, 2, 3, 4, 5

LOG = logging.getLogger("kwbackup")
DRY = False
TS_FORMAT = "%Y-%m-%d-%H-%M-%S"


# ============================================================================
# §1  YAML-LITE
#
# Subset of YAML, enough for config files: block mappings, block sequences,
# flow sequences/mappings, quoted and plain scalars, comments. Anchors,
# aliases, tags, block scalars (| >) and multiple documents are rejected
# loudly with a line number instead of being silently misparsed.
# ============================================================================

class YamlError(Exception):
    def __init__(self, msg, line=None, text=None):
        self.line, self.text = line, text
        if line:
            msg = f"line {line}: {msg}\n    {text}"
        super().__init__(msg)


class _Line:
    __slots__ = ("no", "indent", "body", "text")

    def __init__(self, no, raw):
        self.no, self.text = no, raw
        stripped = raw.lstrip(" ")
        if stripped.startswith("\t"):
            raise YamlError("tabs are not allowed for indentation", no, raw)
        self.indent = len(raw) - len(stripped)
        self.body = stripped.rstrip("\n\r")


def _strip_comment(text, lineno):
    out, quote, i = [], None, 0
    while i < len(text):
        ch = text[i]
        if quote:
            out.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < len(text):
                out.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            out.append(ch)
        elif ch == "#" and (i == 0 or text[i - 1] in " \t"):
            break
        else:
            out.append(ch)
        i += 1
    if quote:
        raise YamlError("unterminated quoted string", lineno, text)
    return "".join(out).rstrip()


def _split_flow(text, lineno):
    parts, depth, quote, cur = [], 0, None, []
    for ch in text:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    if depth:
        raise YamlError("unbalanced flow collection", lineno, text)
    if "".join(cur).strip():
        parts.append("".join(cur))
    return [p.strip() for p in parts]


def _scalar(text, lineno):
    text = text.strip()
    if text == "":
        return None
    if text[0] in "&*!":
        raise YamlError("anchors, aliases and tags are not supported", lineno, text)
    if text[0] in "|>":
        raise YamlError("block scalars (| and >) are not supported; use a list", lineno, text)
    if text[0] == '"':
        if len(text) < 2 or not text.endswith('"'):
            raise YamlError("bad double-quoted scalar", lineno, text)
        body = text[1:-1]
        if "\\" in body:
            try:
                body = body.encode("utf-8").decode("unicode_escape")
            except UnicodeDecodeError:
                raise YamlError("bad escape sequence", lineno, text)
        return body
    if text[0] == "'":
        if len(text) < 2 or not text.endswith("'"):
            raise YamlError("bad single-quoted scalar", lineno, text)
        return text[1:-1].replace("''", "'")
    if text[0] == "[" and text.endswith("]"):
        inner = text[1:-1].strip()
        return [_scalar(p, lineno) for p in _split_flow(inner, lineno)] if inner else []
    if text[0] == "{" and text.endswith("}"):
        inner = text[1:-1].strip()
        result = {}
        for part in _split_flow(inner, lineno):
            key, sep, val = part.partition(":")
            if not sep:
                raise YamlError(f"expected 'key: value' in flow mapping, got {part!r}", lineno, text)
            result[_scalar(key, lineno)] = _scalar(val, lineno)
        return result
    if text in ("~", "null", "Null", "NULL"):
        return None
    if text in ("true", "True", "TRUE", "yes", "on"):
        return True
    if text in ("false", "False", "FALSE", "no", "off"):
        return False
    try:
        return int(text, 10)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


def _partition_key(body):
    quote, depth = None, 0
    for i, ch in enumerate(body):
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == ":" and depth == 0 and (i + 1 == len(body) or body[i + 1] in " \t"):
            return body[:i].strip(), ":", body[i + 1:]
    return body, "", ""


def _is_key_line(text):
    return bool(_partition_key(text)[1]) and not text.startswith(("[", "{"))


class _YamlParser:
    def __init__(self, lines):
        self.lines = lines
        self.i = 0

    def peek(self):
        while self.i < len(self.lines) and self.lines[self.i] is None:
            self.i += 1
        return self.lines[self.i] if self.i < len(self.lines) else None

    def parse_node(self, indent):
        line = self.peek()
        if line is None or line.indent < indent:
            return None
        if line.body.startswith("- ") or line.body == "-":
            return self.parse_seq(line.indent)
        return self.parse_map(line.indent)

    def parse_seq(self, indent):
        items = []
        while True:
            line = self.peek()
            if line is None or line.indent != indent or not (line.body.startswith("- ") or line.body == "-"):
                if line is not None and line.indent > indent:
                    raise YamlError("bad indentation in sequence", line.no, line.text)
                break
            rest = line.body[1:].strip()
            if rest:
                if _is_key_line(rest):
                    self.lines[self.i] = _Line(line.no, " " * (indent + 2) + rest)
                    items.append(self.parse_map(indent + 2))
                    continue
                items.append(_scalar(_strip_comment(rest, line.no), line.no))
                self.i += 1
                continue
            self.i += 1
            child = self.peek()
            items.append(None if child is None or child.indent <= indent else self.parse_node(child.indent))
        return items

    def parse_map(self, indent):
        result = {}
        while True:
            line = self.peek()
            if line is None or line.indent < indent:
                break
            if line.indent > indent:
                raise YamlError("unexpected indentation", line.no, line.text)
            if line.body.startswith("- "):
                break
            body = _strip_comment(line.body, line.no)
            key, sep, rest = _partition_key(body)
            if not sep:
                raise YamlError("expected 'key: value'", line.no, line.text)
            key = _scalar(key, line.no)
            if key in result:
                raise YamlError(f"duplicate key {key!r}", line.no, line.text)
            self.i += 1
            if rest.strip():
                result[key] = _scalar(rest, line.no)
                continue
            child = self.peek()
            if child is None or child.indent <= indent:
                result[key] = None
                if child is not None and child.indent == indent and child.body.startswith("- "):
                    result[key] = self.parse_seq(indent)
                continue
            result[key] = self.parse_node(child.indent)
        return result


def yaml_loads(text):
    lines = []
    for no, raw in enumerate(text.splitlines(), 1):
        s = raw.lstrip(" ")
        if not s.strip() or s.startswith("#") or s.strip() == "---":
            lines.append(None)
            continue
        if s.strip() == "...":
            break
        lines.append(_Line(no, raw))
    parser = _YamlParser(lines)
    first = parser.peek()
    if first is None:
        return None
    result = parser.parse_node(first.indent)
    trailing = parser.peek()
    if trailing is not None:
        raise YamlError("trailing content (multiple documents?)", trailing.no, trailing.text)
    return result


# ============================================================================
# §2  CONFIG
# ============================================================================

class ConfigError(Exception):
    pass


SECTIONS = {
    "root": {"version", "project", "log", "runtime", "rclone",
             "database", "storage", "archive"},
    "log": {"level", "file"},
    "runtime": {"temp_dir", "keep_temp"},
    "rclone": {"binary", "config", "remote", "args", "env"},
    "database": {"enabled", "type", "host", "port", "user", "password_env",
                 "args", "compressor", "compressor_args", "target",
                 "databases", "retention", "retention_mode", "catch_up"},
    "storage": {"enabled", "mode", "target", "sources"},
    "archive": {"enabled", "compressor", "compressor_args", "name", "root",
                "include", "exclude", "target", "retention", "retention_mode",
                "mode"},
}

COMPRESSORS = ("zstd", "gzip", "pigz", "rar", "none")
DB_TYPES = ("mysql", "mariadb", "postgres", "pgsql", "sqlite")
RETENTION_CLASSES = ("daily", "weekly", "monthly")
AGE_RE = re.compile(r"^(\d+)([smhdw])$")
AGE_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


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
    binary: str = "rclone"
    config: str | None = None
    remote: str = ""
    args: list = field(default_factory=list)
    env: dict = field(default_factory=dict)


@dataclass
class DatabaseCfg:
    enabled: bool = False
    type: str = "mysql"
    host: str = "localhost"
    port: int | None = None
    user: str | None = None
    password_env: str | None = None
    args: list = field(default_factory=list)
    compressor: str = "zstd"
    compressor_args: list = field(default_factory=list)
    target: str = ""
    databases: list = field(default_factory=list)
    retention: dict = field(default_factory=dict)      # class -> Retention | None
    catch_up: bool = False


@dataclass
class StorageCfg:
    enabled: bool = False
    mode: str = "sync"
    target: str = ""
    sources: list = field(default_factory=list)


@dataclass
class ArchiveCfg:
    enabled: bool = False
    compressor: str = "zstd"
    compressor_args: list = field(default_factory=list)
    name: str = "archive"
    root: str = ""
    include: list = field(default_factory=lambda: ["*"])
    exclude: list = field(default_factory=list)
    target: str = ""
    retention: Retention | None = None


@dataclass
class Config:
    path: Path
    project: str
    log_level: str = "info"
    log_file: str | None = None
    temp_dir: Path = Path("/tmp/kwbackup")
    keep_temp: bool = False
    rclone: RcloneCfg = field(default_factory=RcloneCfg)
    database: DatabaseCfg = field(default_factory=DatabaseCfg)
    storage: StorageCfg = field(default_factory=StorageCfg)
    archive: ArchiveCfg = field(default_factory=ArchiveCfg)


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


SECTIONS["retention"] = {"keep", "max_age"}


def load_config(path, env=None):
    """Read and validate a YAML config. Raises ConfigError with all problems."""
    path = Path(path)
    if env is not None:
        os.environ.update(env)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"{path}: {exc.strerror}") from exc
    try:
        data = yaml_loads(text)
    except YamlError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")

    errors = []
    _check_keys(data, "root", "config", errors)
    if data.get("version") != 1:
        errors.append("config.version: must be 1")
    data = _expand_env(data, "config", errors)

    cfg = Config(path=path, project=_as_str(data.get("project"), "config.project",
                                            errors, required=True) or path.stem)

    log_raw = data.get("log") or {}
    if not isinstance(log_raw, dict):
        errors.append("config.log: expected a mapping")
        log_raw = {}
    _check_keys(log_raw, "log", "config.log", errors)
    level = (_as_str(log_raw.get("level"), "config.log.level", errors, "info") or "info").lower()
    if level not in ("debug", "info", "warning", "error"):
        errors.append(f"config.log.level: unknown level {level!r}")
        level = "info"
    cfg.log_level = level
    cfg.log_file = _as_str(log_raw.get("file"), "config.log.file", errors)

    rt = data.get("runtime") or {}
    if not isinstance(rt, dict):
        errors.append("config.runtime: expected a mapping")
        rt = {}
    _check_keys(rt, "runtime", "config.runtime", errors)
    cfg.temp_dir = Path(_as_str(rt.get("temp_dir"), "config.runtime.temp_dir", errors,
                                "/tmp/kwbackup"))
    cfg.keep_temp = _as_bool(rt.get("keep_temp"), "config.runtime.keep_temp", errors)

    rl = data.get("rclone") or {}
    if not isinstance(rl, dict):
        errors.append("config.rclone: expected a mapping")
        rl = {}
    _check_keys(rl, "rclone", "config.rclone", errors)
    cfg.rclone = RcloneCfg(
        binary=_as_str(rl.get("binary"), "config.rclone.binary", errors, "rclone"),
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
    db_type = (_as_str(db.get("type"), "config.database.type", errors, "mysql") or "mysql").lower()
    if db_type not in DB_TYPES:
        errors.append(f"config.database.type: unsupported {db_type!r} (allowed: {', '.join(DB_TYPES)})")
    compressor = (_as_str(db.get("compressor"), "config.database.compressor", errors, "zstd") or "zstd").lower()
    if compressor not in COMPRESSORS:
        errors.append(f"config.database.compressor: unsupported {compressor!r} (allowed: {', '.join(COMPRESSORS)})")
    target = _as_str(db.get("target"), "config.database.target", errors, required=True) or ""
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
        type=db_type,
        host=_as_str(db.get("host"), "config.database.host", errors, "localhost") or "localhost",
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
    if cfg.database.enabled and not any(cfg.database.retention.values()):
        errors.append("config.database.retention: at least one class must be configured")

    st = data.get("storage") or {}
    if not isinstance(st, dict):
        errors.append("config.storage: expected a mapping")
        st = {}
    _check_keys(st, "storage", "config.storage", errors)
    cfg.storage = StorageCfg(
        enabled=_as_bool(st.get("enabled"), "config.storage.enabled", errors),
        mode=_as_str(st.get("mode"), "config.storage.mode", errors, "sync") or "sync",
        target=_as_str(st.get("target"), "config.storage.target", errors) or "",
        sources=_as_list(st.get("sources"), "config.storage.sources", errors),
    )
    if cfg.storage.enabled and (not cfg.storage.target or not cfg.storage.sources):
        errors.append("config.storage: target and sources are required when enabled")

    ar = data.get("archive") or {}
    if not isinstance(ar, dict):
        errors.append("config.archive: expected a mapping")
        ar = {}
    _check_keys(ar, "archive", "config.archive", errors)
    ar_mode = _as_str(ar.get("retention_mode"), "config.archive.retention_mode", errors)
    if ar_mode not in (None, "keep", "max_age"):
        errors.append("config.archive.retention_mode: expected keep or max_age")
        ar_mode = None
    cfg.archive = ArchiveCfg(
        enabled=_as_bool(ar.get("enabled"), "config.archive.enabled", errors),
        compressor=(_as_str(ar.get("compressor"), "config.archive.compressor", errors, "zstd") or "zstd").lower(),
        compressor_args=_as_list(ar.get("compressor_args"), "config.archive.compressor_args", errors),
        name=_as_str(ar.get("name"), "config.archive.name", errors, "archive") or "archive",
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


def _as_env(value, path, errors):
    if value is None:
        return {}
    if not isinstance(value, dict):
        errors.append(f"{path}: expected a mapping of NAME: value")
        return {}
    return {str(k): str(v) for k, v in value.items()}


# ============================================================================
# §3  LOGGING
# ============================================================================

class _Fmt(logging.Formatter):
    COLORS = {logging.DEBUG: "\033[90m", logging.INFO: "",
              logging.WARNING: "\033[33m", logging.ERROR: "\033[31m"}

    def format(self, record):
        msg = super().format(record)
        color = self.COLORS.get(record.levelno, "")
        return f"{color}{msg}\033[0m" if color and sys.stderr.isatty() else msg


def setup_logging(cfg, verbose=False, debug=False):
    LOG.handlers.clear()
    LOG.propagate = False
    level = logging.DEBUG if (debug or verbose) else getattr(logging, cfg.log_level.upper())
    LOG.setLevel(logging.DEBUG)
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(level)
    console.setFormatter(_Fmt("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
    LOG.addHandler(console)
    if cfg.log_file:
        try:
            Path(cfg.log_file).parent.mkdir(parents=True, exist_ok=True)
            fh = logging.FileHandler(cfg.log_file, encoding="utf-8")
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
            LOG.addHandler(fh)
        except OSError as exc:
            LOG.warning("log file %s: %s", cfg.log_file, exc)


def fmt_bytes(size):
    step = 1024.0
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < step:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= step
    return f"{size:.1f} PiB"


# ============================================================================
# §4  LOCK
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


# ============================================================================
# §5  RUNNER
# ============================================================================

class ActionFailed(Exception):
    """An action finished with errors; the message is already user-facing."""


class CommandError(Exception):
    def __init__(self, stage, returncode, stderr=""):
        self.stage, self.returncode, self.stderr = stage, returncode, stderr
        super().__init__(f"{stage} failed with exit code {returncode}"
                         + (f": {stderr.strip()[:2000]}" if stderr.strip() else ""))


def _fmt_cmd(cmd):
    return " ".join(cmd)


def run(cmd, env=None, check=True, timeout=None):
    if DRY:
        LOG.info("dry-run: %s", _fmt_cmd(cmd))
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    LOG.debug("run: %s", _fmt_cmd(cmd))
    res = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=timeout)
    if check and res.returncode != 0:
        raise CommandError(_fmt_cmd(cmd), res.returncode, res.stderr or res.stdout)
    return res


def run_pipeline(stages, out_path, env=None, last_writes_file=False):
    """Run a chain of commands connected by pipes; the last one writes out_path.

    last_writes_file=True is for tools that create the archive themselves
    (rar), instead of streaming to stdout.
    """
    if DRY:
        for cmd in stages:
            LOG.info("dry-run: %s", _fmt_cmd(cmd))
        LOG.info("dry-run: output -> %s", out_path)
        return
    err_files, procs, out_fh, prev = [], [], None, None
    try:
        for i, cmd in enumerate(stages):
            last = i == len(stages) - 1
            ef = tempfile.TemporaryFile()
            err_files.append(ef)
            if last and not last_writes_file:
                out_fh = open(out_path, "wb")
                stdout = out_fh
            elif last:
                stdout = subprocess.DEVNULL
            else:
                stdout = subprocess.PIPE
            LOG.debug("run: %s", _fmt_cmd(cmd))
            proc = subprocess.Popen(
                cmd, stdin=prev if prev is not None else subprocess.DEVNULL,
                stdout=stdout, stderr=ef, env=env,
            )
            if prev is not None:
                prev.close()
            prev = None if last else proc.stdout
            procs.append(proc)
        codes = [p.wait() for p in procs]
    finally:
        if out_fh:
            out_fh.close()
        for ef in err_files:
            ef.close()
    for i, (code, cmd) in enumerate(zip(codes, stages)):
        if code != 0:
            err_files_off = None
            raise CommandError(_fmt_cmd(cmd), code, "")
    return


# ============================================================================
# §6  RCLONE
# ============================================================================

@dataclass
class RemoteFile:
    name: str
    size: int
    ts: dt.datetime | None


class Rclone:
    def __init__(self, cfg: RcloneCfg):
        self.cfg = cfg
        self.env = dict(os.environ)
        self.env.update(cfg.env)

    def _base(self):
        cmd = [self.cfg.binary]
        if self.cfg.config:
            cmd += ["--config", self.cfg.config]
        return cmd + list(self.cfg.args)

    def run(self, *args, check=True):
        return run(self._base() + list(args), env=self.env, check=check)

    def lsf(self, path):
        """List files under remote:path as RemoteFile objects (empty if missing)."""
        if DRY:
            return []
        res = self.run("lsf", "--files-only", "--format", "ps", path.rstrip("/") + "/", check=False)
        if res.returncode != 0:
            if "directory not found" in (res.stderr or "").lower():
                return []
            raise CommandError(f"rclone lsf {path}", res.returncode, res.stderr)
        files = []
        for line in res.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            name, _, size = line.partition(";")
            files.append(RemoteFile(name=name, size=int(size or 0), ts=parse_ts(name)))
        return files

    def copyto(self, src, dst):
        return self.run("copyto", src, dst, check=True)

    def deletefile(self, path):
        return self.run("deletefile", path, check=True)

    def remote_path(self, *parts):
        return "/".join([self.cfg.remote.rstrip("/")] + [p.strip("/") for p in parts if p])


# ============================================================================
# §7  RETENTION
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


# ============================================================================
# §8  ACTIONS
# ============================================================================

def db_dir_layout(cfg, dbname):
    """One database -> <target>/<CLASS>, many -> <target>/<db>/<CLASS>."""
    return [dbname] if len(cfg.database.databases) > 1 else []


def class_path(cfg, dbname, cls):
    return "/".join([cfg.database.target.rstrip("/")] + db_dir_layout(cfg, dbname) + [cls.upper()])


def dump_command(cfg, dbname, binary=None):
    db = cfg.database
    kind = db.type
    if kind == "sqlite":
        return [binary or "sqlite3", db.host, ".dump"]
    if kind in ("mysql", "mariadb"):
        exe = binary or ("mariadb-dump" if kind == "mariadb" else "mysqldump")
        cmd = [exe] + (db.args or ["--single-transaction", "--quick",
                                   "--no-tablespaces", "--skip-lock-tables"])
        cmd.append(f"--host={db.host}")
        if db.port:
            cmd.append(f"--port={db.port}")
        if db.user:
            cmd.append(f"--user={db.user}")
        return cmd + [dbname]
    if kind in ("postgres", "pgsql"):
        cmd = ["pg_dump"] + (db.args or ["--no-owner", "--no-privileges"])
        cmd += ["--host", db.host]
        if db.port:
            cmd += ["--port", str(db.port)]
        if db.user:
            cmd += ["--username", db.user]
        return cmd + ["--dbname", dbname]
    raise CommandError(f"dump: unsupported db type {kind}", 1)


def compress_stage(cfg_compressor, cfg_args, out_path, inner_name):
    """Return (stage_cmd, last_writes_file) or (None, False) for no compression."""
    comp = cfg_compressor
    args = list(cfg_args)
    if comp == "zstd":
        return (["zstd", "--quiet"] + args + ["-c"], False)
    if comp in ("gzip", "pigz"):
        exe = "pigz" if comp == "pigz" else "gzip"
        return ([exe] + args + ["-c"], False)
    if comp == "rar":
        return (["rar", "a", f"-si{inner_name}", "-inul"] + args + [out_path], True)
    return (None, False)


COMPRESSOR_EXT = {"zstd": ".zst", "gzip": ".gz", "pigz": ".gz", "rar": ".rar", "none": ""}


def action_database(cfg, args):
    """Dump every database, upload to the class folders, prune old copies."""
    db = cfg.database
    if not db.enabled and not args.force:
        LOG.info("database backup is disabled in config (use --force to override)")
        return {"skipped": True}

    uploader = Rclone(cfg.rclone)
    cfg.temp_dir.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now()
    stamp = now.strftime(TS_FORMAT)
    ext = COMPRESSOR_EXT[db.compressor]
    total = {"databases": 0, "uploaded": 0, "deleted": 0, "bytes": 0}
    failures = []

    for dbname in db.databases:
        out_name = f"{dbname}_{stamp}.sql{ext}"
        out_path = cfg.temp_dir / out_name
        try:
            with task_lock(f"{cfg.project}.db.{dbname}", cfg.path):
                LOG.info("[%s] dumping %s (%s)", dbname, dbname, db.type)
                env = dict(os.environ)
                if db.password_env:
                    env["MYSQL_PWD"] = os.environ.get(db.password_env, "")
                    if db.type in ("postgres", "pgsql"):
                        env["PGPASSWORD"] = os.environ.get(db.password_env, "")
                stages = [dump_command(cfg, dbname)]
                comp, writes_file = compress_stage(db.compressor, db.compressor_args,
                                                   str(out_path), out_name)
                if comp:
                    stages.append(comp)
                run_pipeline(stages, str(out_path), env=env, last_writes_file=writes_file)
                size = 0
                if not DRY:
                    size = out_path.stat().st_size
                    if size == 0:
                        raise CommandError("dump produced an empty file", 1)
                    LOG.info("[%s] dump ready: %s", dbname, fmt_bytes(size))

                for cls in RETENTION_CLASSES:
                    retention = db.retention.get(cls)
                    if retention is None:
                        continue
                    remote_dir = class_path(cfg, dbname, cls)
                    existing = uploader.lsf(remote_dir)
                    if not class_due(cls, existing, db.catch_up, now):
                        continue
                    dest = f"{remote_dir}/{out_name}"
                    uploader.copyto(str(out_path), dest)
                    total["uploaded"] += 1
                    LOG.info("[%s] %s -> %s (%s)", dbname,
                             "would upload" if DRY else "uploaded", dest,
                             retention.describe())
                    files = uploader.lsf(remote_dir)
                    for stale in plan_prune(files, retention, now):
                        uploader.deletefile(f"{remote_dir}/{stale}")
                        total["deleted"] += 1
                        LOG.info("[%s] pruned %s/%s", dbname, cls.upper(), stale)
                total["databases"] += 1
                total["bytes"] += size
        except Locked:
            raise
        except (CommandError, OSError) as exc:
            LOG.error("[%s] %s", dbname, exc)
            failures.append(f"{dbname}: {exc}")
        finally:
            if not cfg.keep_temp and out_path.exists():
                out_path.unlink()
    if failures:
        raise ActionFailed("database backup failed: " + "; ".join(failures))
    return total


def action_list(cfg, args):
    uploader = Rclone(cfg.rclone)
    now = dt.datetime.now()
    rc = EXIT_OK
    if cfg.database.enabled or args.force:
        for dbname in cfg.database.databases:
            print(f"\nDATABASE {dbname}  ->  {cfg.database.target}")
            for cls in RETENTION_CLASSES:
                retention = cfg.database.retention.get(cls)
                remote_dir = class_path(cfg, dbname, cls)
                files = [f for f in uploader.lsf(remote_dir) if f.ts]
                files.sort(key=lambda f: f.ts)
                label = cls.upper().ljust(7)
                if not retention:
                    print(f"  {label} disabled")
                    continue
                if not files:
                    print(f"  {label} 0 files  ⚠ empty  ({retention.describe()})")
                    if cls == "daily":
                        rc = EXIT_VERIFY
                    continue
                first, last = files[0].ts, files[-1].ts
                print(f"  {label} {len(files):>3} files  {first:%Y-%m-%d} .. {last:%Y-%m-%d}"
                      f"  last {fmt_bytes(files[-1].size)}  ({retention.describe()})")
                if cls == "daily":
                    age = (now.date() - last.date()).days
                    if age > 1:
                        print(f"  {'':7} ⚠ newest DAILY copy is {age} days old")
                        rc = EXIT_VERIFY
                    for gap in missing_days(files)[:5]:
                        print(f"  {'':7} ⚠ gap: {gap:%Y-%m-%d}")
    if cfg.storage.enabled:
        print("\nSTORAGE  not implemented yet")
    if cfg.archive.enabled:
        print("\nARCHIVE  not implemented yet")
    return rc


def _peek_decompressed(path, n=4096):
    """First n bytes of the uncompressed stream."""
    name = str(path).lower()
    if name.endswith((".zst", ".zstd")):
        cmd = ["zstd", "--quiet", "-dc", str(path)]
    elif name.endswith(".gz"):
        cmd = ["gzip", "-dc", str(path)]
    elif name.endswith(".rar"):
        return b""
    else:
        cmd = ["cat", str(path)]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        return proc.stdout.read(n) if proc.stdout else b""
    finally:
        proc.kill()
        proc.wait()


MARKERS = (b"MySQL dump", b"MariaDB dump", b"PostgreSQL database dump",
           b"SQLite format", b"BEGIN TRANSACTION", b"PRAGMA")


def action_verify(cfg, args):
    uploader = Rclone(cfg.rclone)
    rc = EXIT_OK
    if not (cfg.database.enabled or args.force):
        LOG.info("nothing to verify")
        return rc
    cfg.temp_dir.mkdir(parents=True, exist_ok=True)
    for dbname in cfg.database.databases:
        for cls in RETENTION_CLASSES:
            remote_dir = class_path(cfg, dbname, cls)
            files = [f for f in uploader.lsf(remote_dir) if f.ts]
            if not files:
                continue
            files.sort(key=lambda f: f.ts)
            picked = [files[-1]] if not args.all else files
            for f in picked:
                local = cfg.temp_dir / f.name
                try:
                    LOG.info("[%s] verifying %s/%s (%s)", dbname, cls.upper(), f.name,
                             fmt_bytes(f.size))
                    uploader.copyto(f"{remote_dir}/{f.name}", str(local))
                    got = local.stat().st_size
                    if got != f.size:
                        print(f"  ✗ {cls.upper()}/{f.name}: size mismatch {got} != {f.size}")
                        rc = EXIT_VERIFY
                        continue
                    name = f.name.lower()
                    if name.endswith((".zst", ".zstd")):
                        run(["zstd", "--quiet", "-t", str(local)])
                    elif name.endswith(".gz"):
                        run(["gzip", "-t", str(local)])
                    elif name.endswith(".rar"):
                        run(["rar", "t", "-inul", str(local)])
                    head = _peek_decompressed(local)
                    if head and not any(m in head for m in MARKERS):
                        print(f"  ? {cls.upper()}/{f.name}: decompresses, but no SQL header found")
                        rc = EXIT_VERIFY
                    else:
                        print(f"  ✓ {cls.upper()}/{f.name}: {fmt_bytes(got)}")
                except (CommandError, OSError) as exc:
                    print(f"  ✗ {cls.upper()}/{f.name}: {exc}")
                    rc = EXIT_VERIFY
                finally:
                    if local.exists():
                        local.unlink()
    return rc


# ============================================================================
# §9  PREFLIGHT
# ============================================================================

def preflight(cfg, actions):
    problems = []
    wanted = []
    if "database" in actions and (cfg.database.enabled):
        db = cfg.database
        wanted.append(_dump_binary(db))
        if db.compressor != "none":
            wanted.append("pigz" if db.compressor == "pigz" else db.compressor)
    if "storage" in actions or "archive" in actions:
        wanted.append("rclone")
    wanted.append(cfg.rclone.binary)
    for binary in dict.fromkeys(wanted):
        if binary and shutil.which(binary) is None:
            problems.append(f"binary not found in PATH: {binary}")
    try:
        cfg.temp_dir.mkdir(parents=True, exist_ok=True)
        probe = cfg.temp_dir / f".probe-{os.getpid()}"
        probe.write_bytes(b"x")
        probe.unlink()
    except OSError as exc:
        problems.append(f"temp dir {cfg.temp_dir}: {exc.strerror}")
    if cfg.database.enabled and cfg.database.password_env:
        if cfg.database.password_env not in os.environ:
            problems.append(f"environment variable {cfg.database.password_env} is not set")
    if cfg.rclone.config and not Path(cfg.rclone.config).exists():
        problems.append(f"rclone config not found: {cfg.rclone.config}")
    for p in problems:
        LOG.error("preflight: %s", p)
    return not problems


def _dump_binary(db):
    if db.type in ("mysql", "mariadb"):
        return "mariadb-dump" if db.type == "mariadb" else "mysqldump"
    if db.type in ("postgres", "pgsql"):
        return "pg_dump"
    return "sqlite3"


# ============================================================================
# §10  CLI
# ============================================================================

def build_parser():
    p = argparse.ArgumentParser(
        prog=PROG, description="Backup tool: database dumps, file storage, archives.",
        epilog="Exit codes: 0 ok, 1 error, 2 config error, 3 usage, 4 locked, 5 verify failed.",
    )
    p.add_argument("-c", "--config", metavar="FILE", help="config file (YAML)")
    p.add_argument("-d", "--database", action="store_true", help="backup databases")
    p.add_argument("-s", "--storage", action="store_true", help="backup file storage")
    p.add_argument("-a", "--archive", action="store_true", help="backup file archive")
    p.add_argument("-m", "--mode", choices=("copy", "sync"),
                   help="override upload algorithm for storage/archive")
    p.add_argument("-f", "--force", action="store_true",
                   help="run actions even if disabled in config")
    p.add_argument("--dry-run", action="store_true", help="show what would be done")
    p.add_argument("--check-config", action="store_true", help="validate config and exit")
    p.add_argument("--list", action="store_true", help="show what is stored in the cloud")
    p.add_argument("--verify", action="store_true", help="download and check the newest copy")
    p.add_argument("--all", action="store_true", help="with --verify: check every stored copy")
    p.add_argument("--restore", action="store_true", help="download a copy (not implemented yet)")
    p.add_argument("--to", metavar="DIR", help="restore target directory")
    p.add_argument("--at", metavar="TS", help="restore a specific copy, e.g. 2026-11-09-03-00-00")
    p.add_argument("--verbose", action="store_true", help="more messages")
    p.add_argument("--debug", action="store_true", help="debug messages, print every command")
    p.add_argument("--selftest", action="store_true", help="run internal tests and exit")
    p.add_argument("--version", action="store_true", help="print version and exit")
    return p


def main(argv=None):
    global DRY
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()
    if args.version:
        print(f"{PROG} {VERSION}")
        return EXIT_OK
    if not args.config:
        parser.print_usage()
        print(f"{PROG}: error: -c/--config is required", file=sys.stderr)
        return EXIT_USAGE

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    setup_logging(cfg, args.verbose, args.debug)
    DRY = args.dry_run
    LOG.debug("config: %s (project %s)", cfg.path, cfg.project)

    if args.check_config:
        print(f"config OK: {cfg.path}")
        print(f"  rclone: {cfg.rclone.remote} via {cfg.rclone.config or 'default'}")
        if cfg.database.enabled:
            print(f"  database: {cfg.database.type}, {len(cfg.database.databases)} db(s), "
                  f"compressor {cfg.database.compressor}")
            for cls in RETENTION_CLASSES:
                ret = cfg.database.retention.get(cls)
                print(f"    {cls:8} {ret.describe() if ret else 'disabled'}"
                      f"{'' if ret or not cfg.database.catch_up else ''}")
        return EXIT_OK

    if args.list:
        return action_list(cfg, args)
    if args.verify:
        return action_verify(cfg, args)
    if args.restore:
        print(f"{PROG}: --restore is not implemented yet", file=sys.stderr)
        return EXIT_USAGE

    actions = []
    if args.database:
        actions.append("database")
    if args.storage:
        actions.append("storage")
    if args.archive:
        actions.append("archive")
    if not actions:
        actions = [name for name, enabled in (("database", cfg.database.enabled),
                                             ("storage", cfg.storage.enabled),
                                             ("archive", cfg.archive.enabled)) if enabled]
        if not actions:
            print("Nothing to do. Enable an action in config or pass -d/-s/-a.")
            return EXIT_OK
        LOG.info("no action requested, running enabled ones: %s", ", ".join(actions))

    if not preflight(cfg, actions):
        return EXIT_ERROR

    rc = EXIT_OK
    started = dt.datetime.now()
    try:
        for name in actions:
            if name == "database":
                stats = action_database(cfg, args)
                if not stats.get("skipped"):
                    LOG.info("database: %d db(s), %d uploaded, %d pruned, %s",
                             stats["databases"], stats["uploaded"], stats["deleted"],
                             fmt_bytes(stats["bytes"]))
            elif name in ("storage", "archive"):
                LOG.error("%s backup is not implemented yet", name)
                rc = EXIT_ERROR
    except Locked as exc:
        LOG.error("%s", exc)
        return EXIT_LOCKED
    except (ActionFailed, CommandError, ConfigError, OSError) as exc:
        LOG.error("%s", exc)
        return EXIT_ERROR
    LOG.info("done in %.1fs, exit %d", (dt.datetime.now() - started).total_seconds(), rc)
    return rc


# ============================================================================
# §11  SELFTEST
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

    # config validation
    with tempfile.TemporaryDirectory() as tmp:
        base = """
version: 1
project: t
rclone: {remote: r, config: /dev/null}
database:
  enabled: true
  type: maria
  target: r:/DB
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
        bogus = good.replace("databases: [db1]", "databeses: [db1]")
        try:
            load_config(write(bogus))
            check("unknown key", False, "accepted")
        except ConfigError as exc:
            check("unknown key", "unknown key" in str(exc), str(exc))

    if failures:
        for f in failures:
            print(f"FAIL {f}")
        print(f"{len(failures)} selftest(s) failed")
        return EXIT_ERROR
    print("selftest: all checks passed")
    return EXIT_OK


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print(f"\n{PROG}: interrupted", file=sys.stderr)
        sys.exit(130)
