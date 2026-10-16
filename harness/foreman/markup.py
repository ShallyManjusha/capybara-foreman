"""
Jira wiki markup, for Data Center and Server.

Jira Cloud's v3 API takes ADF, a JSON document tree. Data Center's v2 API takes
wiki markup, a plain string. They have nothing in common, and a client that
speaks only one of them works at roughly half the enterprises you will meet.

Rather than write everything twice, Foreman renders every comment to **markdown**
in `render.py` and converts at the last moment — to ADF by `adf.py` for Cloud,
to wiki markup here for DC. Markdown is the interchange format. Adding a third
tracker means writing one more converter, not one more set of templates.

The subset matches `adf.from_markdown` exactly, so a comment reads the same on
both deployments. Where wiki markup has no equivalent — collapsible sections
without the Adaptavist plugin — it degrades to a heading and a horizontal rule
rather than dropping the content.
"""
import re
from typing import List

# Panels map onto wiki macros. `success` has no macro of its own; `tip` is the
# closest thing Jira ships and renders green, which is what the reader needs.
_PANEL_MACRO = {
    "info": "info",
    "note": "note",
    "warning": "warning",
    "error": "warning",
    "success": "tip",
}

_FENCE = re.compile(r"^```\s*([A-Za-z0-9_+-]*)\s*$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^\s*[-*+]\s+(.*)$")
_ORDERED = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_QUOTE = re.compile(r"^>\s?(.*)$")
_RULE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_TABLE_ROW = re.compile(r"^\s*\|(.+)\|\s*$")
_TABLE_SEP = re.compile(r"^\s*\|[\s:|-]+\|\s*$")
_PANEL_OPEN = re.compile(r"^:::\s*(%s)\s*$" % "|".join(_PANEL_MACRO))
_EXPAND_OPEN = re.compile(r"^:::\s*expand\s+(.+?)\s*$")
_BLOCK_CLOSE = re.compile(r"^:::\s*$")

# Jira rejects a comment body over 32767 characters with a 400 that names no
# field, which is a miserable thing to debug from a cron log.
MAX_BODY_CHARS = 30000


# ──────────────────────────────────────────────────────────────── inline ────
_MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_MD_CODE = re.compile(r"`([^`]+)`")
_MD_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_MD_STRIKE = re.compile(r"~~([^~]+)~~")
_MD_EM = re.compile(r"(?<![\w*_])_([^_]+)_(?![\w*_])")


def inline(src: str) -> str:
    """Convert one line of markdown inline syntax to wiki markup.

    Code spans are extracted first and restored last, so a snippet containing
    `*` or `_` — which is most code — is not mangled into emphasis. Getting
    this wrong turns every quoted line of Python in an analysis comment into
    formatting noise.
    """
    if not src:
        return ""
    held: List[str] = []

    def hold(match):
        held.append(match.group(1))
        return "\x00%d\x00" % (len(held) - 1)

    out = _MD_CODE.sub(hold, src)
    out = _MD_LINK.sub(lambda m: "[%s|%s]" % (m.group(1), m.group(2)), out)
    out = _MD_BOLD.sub(lambda m: "*%s*" % m.group(1), out)
    out = _MD_STRIKE.sub(lambda m: "-%s-" % m.group(1), out)
    out = _MD_EM.sub(lambda m: "_%s_" % m.group(1), out)
    for n, code in enumerate(held):
        out = out.replace("\x00%d\x00" % n, "{{%s}}" % code)
    return out


# ───────────────────────────────────────────────────────── markdown → wiki ──
def from_markdown(src: str) -> str:
    return "\n".join(_blocks((src or "").replace("\r\n", "\n").split("\n")))


def _blocks(lines: List[str], depth: int = 0) -> List[str]:
    out: List[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]

        m = _FENCE.match(line)
        if m:
            lang, body, i = m.group(1), [], i + 1
            while i < len(lines) and not _FENCE.match(lines[i]):
                body.append(lines[i])
                i += 1
            i += 1
            head = "{code:%s}" % lang if lang else "{code}"
            out += [head, "\n".join(body), "{code}", ""]
            continue

        m = _PANEL_OPEN.match(line)
        if m:
            macro = _PANEL_MACRO.get(m.group(1), "info")
            body, i = _collect_block(lines, i + 1)
            out += ["{%s}" % macro] + _blocks(body, depth + 1) + ["{%s}" % macro, ""]
            continue

        m = _EXPAND_OPEN.match(line)
        if m:
            # {expand} needs a plugin that many DC instances do not have, and an
            # unrendered macro shows as literal braces. A heading always works,
            # and the content is the part that matters.
            title, i2 = m.group(1), i + 1
            body, i = _collect_block(lines, i2)
            out += ["", "h4. %s" % inline(title), ""] + _blocks(body, depth + 1) + ["----", ""]
            continue

        if _RULE.match(line):
            out += ["----", ""]
            i += 1
            continue

        m = _HEADING.match(line)
        if m:
            out += ["h%d. %s" % (min(6, len(m.group(1))), inline(m.group(2))), ""]
            i += 1
            continue

        if _TABLE_ROW.match(line):
            first = True
            while i < len(lines) and _TABLE_ROW.match(lines[i]):
                if _TABLE_SEP.match(lines[i]):
                    i += 1
                    continue
                cells = [inline(c.strip()) or " " for c in
                         _TABLE_ROW.match(lines[i]).group(1).split("|")]
                sep = "||" if first else "|"
                out.append(sep + sep.join(cells) + sep)
                first = False
                i += 1
            out.append("")
            continue

        if _BULLET.match(line) or _ORDERED.match(line):
            ordered = bool(_ORDERED.match(line))
            bullet = "#" if ordered else "*"
            while i < len(lines):
                bm = _ORDERED.match(lines[i]) if ordered else _BULLET.match(lines[i])
                if not bm:
                    break
                out.append("%s %s" % (bullet, inline(bm.group(1))))
                i += 1
            out.append("")
            continue

        if _QUOTE.match(line):
            quoted = []
            while i < len(lines) and _QUOTE.match(lines[i]):
                quoted.append(_QUOTE.match(lines[i]).group(1))
                i += 1
            out += ["{quote}"] + _blocks(quoted, depth + 1) + ["{quote}", ""]
            continue

        if not line.strip():
            if out and out[-1] != "":
                out.append("")
            i += 1
            continue

        out.append(inline(line.strip()))
        i += 1

    return out


def _collect_block(lines: List[str], i: int):
    """Read to the matching ::: close, honouring nesting."""
    body, level = [], 1
    while i < len(lines):
        if _PANEL_OPEN.match(lines[i]) or _EXPAND_OPEN.match(lines[i]):
            level += 1
        elif _BLOCK_CLOSE.match(lines[i]):
            level -= 1
            if level == 0:
                return body, i + 1
        body.append(lines[i])
        i += 1
    return body, i


# ───────────────────────────────────────────────────────── wiki → plain ────
_STRIP = [
    (re.compile(r"\{code(?::[^}]*)?\}"), ""),
    (re.compile(r"\{(?:info|note|warning|tip|panel)(?::[^}]*)?\}"), ""),
    (re.compile(r"\{quote\}"), ""),
    (re.compile(r"^h[1-6]\.\s*", re.M), ""),
    (re.compile(r"\{\{([^}]*)\}\}"), r"\1"),
    (re.compile(r"\[([^\]|]+)\|[^\]]+\]"), r"\1"),
]


def to_text(body) -> str:
    """Flatten a DC comment body to plain text.

    Used for reading — comment commands, descriptions, marker matching. DC
    bodies are already strings, so this only strips the macros that would
    otherwise sit in the middle of a `/foreman approve` and stop it matching.
    """
    if body is None:
        return ""
    if not isinstance(body, str):
        # A DC instance behind a Cloud-shaped proxy, or a mixed fixture. Fall
        # back to the ADF reader rather than stringifying a dict.
        from . import adf
        return adf.to_text(body)
    out = body
    for pattern, repl in _STRIP:
        out = pattern.sub(repl, out)
    return out


def truncate(body: str, limit: int = MAX_BODY_CHARS) -> str:
    """Cut to fit, and say that it was cut."""
    if len(body) <= limit:
        return body
    return body[:limit - 300].rstrip() + (
        "\n\n{warning}\nThis comment was truncated to fit Jira's size limit. "
        "The complete record is in the Foreman run report and audit archive.\n"
        "{warning}")
