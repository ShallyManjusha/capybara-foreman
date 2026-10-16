"""
A YAML subset loader, used when PyYAML is absent.

PyYAML is not assumed. Air-gapped IBM build agents frequently lack it, and a
delivery pipeline that cannot start because of a missing parser is a pipeline
nobody runs. PyYAML is used when present; this is the fallback.

The subset covers what Foreman's own policy files use and nothing more: nested
mappings, lists of scalars, lists of mappings, inline `[a, b]` lists, quoted
strings, block scalars (`|` and `>`), and the usual scalar types. Anchors,
multi-document streams and flow mappings are not supported — if you need them,
install PyYAML.
"""
import re
from typing import Any, Dict, List, Tuple

_KEY = re.compile(r"^(?P<key>[^:#]+?)\s*:\s*(?P<val>.*)$")


def load(path: str) -> Any:
    try:
        import yaml
        with open(path, encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except ImportError:
        pass
    with open(path, encoding="utf-8") as fh:
        return loads(fh.read())


def loads(text: str) -> Any:
    lines = _clean(text)
    value, _ = _parse_block(lines, 0, _indent_of(lines[0][1]) if lines else 0)
    return value


def _clean(text: str) -> List[Tuple[int, str]]:
    """Strip comments and blank lines, keeping original line numbers for errors."""
    out = []
    for n, raw in enumerate((text or "").replace("\r\n", "\n").split("\n"), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        out.append((n, raw.rstrip()))
    return out


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _parse_block(lines: List[Tuple[int, str]], i: int, indent: int):
    """Parse every line at `indent` starting from `i`. Returns (value, next_i)."""
    if i >= len(lines):
        return {}, i
    if lines[i][1].lstrip().startswith("- "):
        return _parse_list(lines, i, indent)
    return _parse_map(lines, i, indent)


def _parse_map(lines, i, indent) -> Tuple[Dict[str, Any], int]:
    out: Dict[str, Any] = {}
    while i < len(lines):
        _, line = lines[i]
        cur = _indent_of(line)
        if cur < indent:
            break
        if cur > indent:                       # malformed; treat as belonging up
            i += 1
            continue
        m = _KEY.match(line.strip())
        if not m:
            i += 1
            continue
        key, val = m.group("key").strip().strip('"\''), m.group("val").strip()
        if val in ("|", ">", "|-", ">-"):
            block, i = _parse_scalar_block(lines, i + 1, indent, fold=val[0] == ">")
            out[key] = block
            continue
        if val == "":
            nxt = _peek_indent(lines, i + 1)
            if nxt is not None and nxt > indent:
                out[key], i = _parse_block(lines, i + 1, nxt)
                continue
            if nxt is not None and nxt == indent and \
                    i + 1 < len(lines) and lines[i + 1][1].lstrip().startswith("- "):
                out[key], i = _parse_list(lines, i + 1, indent)
                continue
            out[key] = None
            i += 1
            continue
        out[key] = scalar(val)
        i += 1
    return out, i


def _parse_list(lines, i, indent) -> Tuple[List[Any], int]:
    out: List[Any] = []
    while i < len(lines):
        _, line = lines[i]
        cur = _indent_of(line)
        stripped = line.strip()
        if cur < indent or not stripped.startswith("- "):
            break
        item = stripped[2:].strip()
        if _KEY.match(item) and not item.startswith(('"', "'")):
            # A list of mappings: re-parse the item's first key together with
            # any following lines indented past the dash.
            child_indent = cur + 2
            synthetic = [(lines[i][0], " " * child_indent + item)]
            j = i + 1
            while j < len(lines) and _indent_of(lines[j][1]) >= child_indent \
                    and not lines[j][1].strip().startswith("- "):
                synthetic.append(lines[j])
                j += 1
            value, _ = _parse_map(synthetic, 0, child_indent)
            out.append(value)
            i = j
            continue
        out.append(scalar(item))
        i += 1
    return out, i


def _parse_scalar_block(lines, i, indent, fold=False) -> Tuple[str, int]:
    body = []
    while i < len(lines) and _indent_of(lines[i][1]) > indent:
        body.append(lines[i][1][indent + 2:])
        i += 1
    return (" ".join(x.strip() for x in body) if fold else "\n".join(body)), i


def _peek_indent(lines, i):
    return _indent_of(lines[i][1]) if i < len(lines) else None


def _strip_comment(v: str) -> str:
    """Drop a trailing ` # comment` from an unquoted scalar.

    A `#` only starts a comment when it follows whitespace, so URLs with
    fragments and values like `a#b` survive. Quoted scalars are left alone.
    """
    v = v.strip()
    if not v or v[0] in "'\"":
        return v
    for i, ch in enumerate(v):
        if ch == "#" and (i == 0 or v[i - 1] in " \t"):
            return v[:i].rstrip()
    return v


def scalar(v: str) -> Any:
    v = _strip_comment(v)
    if v.startswith("[") and v.endswith("]"):
        inner = v[1:-1].strip()
        return [scalar(x) for x in _split_inline(inner)] if inner else []
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1]
    low = v.lower()
    if low in ("null", "~", ""):
        return None
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    return v


def _split_inline(inner: str) -> List[str]:
    out, buf, quote = [], "", ""
    for ch in inner:
        if quote:
            buf += ch
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
            buf += ch
        elif ch == ",":
            out.append(buf.strip())
            buf = ""
        else:
            buf += ch
    if buf.strip():
        out.append(buf.strip())
    return out
