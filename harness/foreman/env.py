"""
.env loading. Stdlib only — python-dotenv is not assumed.

Every credential the system uses is read from the environment, and the
environment is populated from a .env file that is never committed. Nothing in
this codebase should ever contain a literal key.
"""
import os
import re
from typing import Dict, List, Optional

_LINE = re.compile(r"""^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$""")


def load_env(path: str = ".env", override: bool = False) -> Dict[str, str]:
    """Parse a .env file into os.environ. Returns what was loaded.

    Real environment variables win over .env by default: a CI job injecting a
    secret from a proper secret store must not be silently overridden by a
    stale file left in the working tree.
    """
    loaded = {}
    if not os.path.exists(path):
        return loaded
    for raw in open(path, encoding="utf-8"):
        line = raw.rstrip("\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _LINE.match(line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if val[:1] == val[-1:] and val[:1] in ("'", '"') and len(val) >= 2:
            val = val[1:-1]
        else:
            # A JQL clause legitimately contains '#'-free text but may contain
            # ' #'; only strip a comment that is clearly detached.
            val = val.split("  #", 1)[0].strip()
        loaded[key] = val
        if override or key not in os.environ:
            os.environ[key] = val
    return loaded


def require(*names: str) -> Optional[str]:
    """Return an error message naming every missing variable, or None."""
    missing = [n for n in names if not os.environ.get(n, "").strip()]
    if not missing:
        return None
    return ("missing required environment variable(s): %s\n"
            "  Add them to your .env file (see .env.example)."
            % ", ".join(missing))


def redact(value: str, keep: int = 4) -> str:
    """Never log a whole credential."""
    if not value:
        return "<unset>"
    if len(value) <= keep * 2:
        return "*" * len(value)
    return "%s...%s (%d chars)" % (value[:keep], value[-keep:], len(value))


def flag(name: str, default: bool = False) -> bool:
    """Boolean from the environment, tolerant of spacing and case."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def num(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name, "")).strip())
    except (TypeError, ValueError):
        return default


def csv(name: str, default: Optional[List[str]] = None) -> List[str]:
    raw = os.environ.get(name, "") or ""
    out = [x.strip() for x in raw.split(",") if x.strip()]
    return out if out else list(default or [])
