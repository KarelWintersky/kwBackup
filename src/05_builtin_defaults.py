# ============================================================================
# BUILT-IN DEFAULTS
#
# The lowest configuration layer. Precedence:
#
#   BUILTIN_DEFAULTS  <  /etc/kwbackup/defaults.yaml  <  --config FILE
#
# This dict is the single place in the code where default values live.
# Everything else (dataclass fields, the parser) reads from it.
#
# Deliberately NOT here: `project`, `rclone.remote`, `rclone.config`, the
# database host/credentials and retention policy. Those are site decisions
# and belong to /etc/kwbackup/defaults.yaml or the task file.
# ============================================================================

BUILTIN_DEFAULTS = {
    "log": {
        "level": "info",
    },
    "runtime": {
        "temp_dir": "/tmp/kwbackup",
        "keep_temp": False,
    },
    "rclone": {
        "binary": "rclone",
        "args": [],
        "env": {},
    },
    "database": {
        "type": "mysql",
        "mode": "copy",
        "host": "localhost",
        "compressor": "zstd",
        "compressor_args": [],
        "catch_up": False,
    },
    "storage": {
        "mode": "sync",
    },
    "archive": {
        "compressor": "zstd",
        "mode": "copy",
        "name": "archive",
        "include": ["*"],
        "exclude": [],
    }
}

# The only defaults file the tool looks at (overridable for tests and for
# running the examples in place via the hidden --defaults flag).
SYSTEM_DEFAULTS_PATH = Path("/etc/kwbackup/defaults.yaml")


def _default(section, key):
    """A value from BUILTIN_DEFAULTS (the single source of default values)."""
    value = BUILTIN_DEFAULTS.get(section, {}).get(key)
    return list(value) if isinstance(value, list) else value
