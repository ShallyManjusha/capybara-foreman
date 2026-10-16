"""
Atlassian Document Format.

The Jira Cloud v3 API will not accept a string where a document is expected, so
everything Foreman says on a ticket passes through here. The ticket thread *is*
the user interface for this system — a reviewer approves or rejects based on
what they read there — so the rendering is not cosmetic. A wall of escaped
markdown is a gate nobody reads carefully.

This is a deliberate markdown *subset*: headings, paragraphs, bullet and
ordered lists, fenced code, blockquotes, tables, rules, panels, collapsible
expands, and the inline marks. Anything outside the subset degrades to plain
text rather than raising — a template typo must not break a run mid-flight.
"""
import re
from typing import Any, Dict, List, Optional

# Jira rejects documents above ~32k characters outright, and a reviewer stops
# reading long before that. Long code blocks are the usual culprit.
MAX_DOC_CHARS = 30000

_PANEL_TYPES = ("info", "note", "warning", "success", "error")


# ──────────────────────────────────────────────────────────────── inline ────
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_EM = re.compile(r"(?<![\w*])_([^_]+)_(?![\w*])")
_STRIKE = re.compile(r"~~([^~]+)~~")
_BARE_URL = re.compile(r"(?<![(\[\w])(https?://[^\s<>\])]+)")


def _text(value: str, marks: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    node: Dict[str, Any] = {"type": "text", "text": value}
    if marks:
        node["marks"] = marks
    return node


def inline(src: str) -> List[Dict[str, Any]]:
    """Parse one line of markdown into ADF inline nodes.

    Ordering matters: code spans are extracted first and never re-scanned, so
    a snippet like `**not bold**` inside backticks survives intact. Getting
    this wrong turns quoted code in an analysis comment into formatting noise.
    """
    if not src:
        return []
    out: List[Dict[str, Any]] = []

    def emit_plain(chunk: str):
        if chunk:
            out.extend(_marked(chunk))

    pos = 0
    for m in _CODE.finditer(src):
        emit_plain(src[pos:m.start()])
        out.append(_text(m.group(1), [{"type": "code"}]))
        pos = m.end()
    emit_plain(src[pos:])
    return out or [_text(src)]


def _marked(chunk: str) -> List[Dict[str, Any]]:
    """Apply link / bold / emphasis / strike marks to a code-free chunk."""
    out: List[Dict[str, Any]] = []
    pos = 0
    for m in _LINK.finditer(chunk):
        out.extend(_styled(chunk[pos:m.start()]))
        out.append(_text(m.group(1),
                         [{"type": "link", "attrs": {"href": m.group(2)}}]))
        pos = m.end()
    tail = chunk[pos:]

    # Bare URLs become links too — Foreman emits a lot of them (PR, pipeline,
    # report) and an unlinked URL in a Jira comment is not clickable.
    pos2 = 0
    for m in _BARE_URL.finditer(tail):
        out.extend(_styled(tail[pos2:m.start()]))
        url = m.group(1).rstrip(".,;:")
        out.append(_text(url, [{"type": "link", "attrs": {"href": url}}]))
        pos2 = m.start() + len(url)
    out.extend(_styled(tail[pos2:]))
    return out


def _styled(chunk: str) -> List[Dict[str, Any]]:
    if not chunk:
        return []
    out: List[Dict[str, Any]] = []
    pos = 0
    pattern = re.compile("|".join([
        r"(?P<bold>\*\*[^*]+\*\*)",
        r"(?P<strike>~~[^~]+~~)",
        r"(?P<em>(?<![\w*])_[^_]+_(?![\w*]))",
    ]))
    for m in pattern.finditer(chunk):
        if m.start() > pos:
            out.append(_text(chunk[pos:m.start()]))
        raw = m.group(0)
        if m.lastgroup == "bold":
            out.append(_text(raw[2:-2], [{"type": "strong"}]))
        elif m.lastgroup == "strike":
            out.append(_text(raw[2:-2], [{"type": "strike"}]))
        else:
            out.append(_text(raw[1:-1], [{"type": "em"}]))
        pos = m.end()
    if pos < len(chunk):
        out.append(_text(chunk[pos:]))
    return out


# ───────────────────────────────────────────────────────────────── blocks ────
def paragraph(src: str) -> Dict[str, Any]:
    return {"type": "paragraph", "content": inline(src)}


def heading(src: str, level: int) -> Dict[str, Any]:
    return {"type": "heading", "attrs": {"level": max(1, min(6, level))},
            "content": inline(src)}


def code_block(src: str, language: str = "") -> Dict[str, Any]:
    node: Dict[str, Any] = {"type": "codeBlock",
                            "content": [{"type": "text", "text": src or " "}]}
    if language:
        node["attrs"] = {"language": language}
    return node


def rule() -> Dict[str, Any]:
    return {"type": "rule"}


def panel(kind: str, content: List[Dict[str, Any]]) -> Dict[str, Any]:
    if kind not in _PANEL_TYPES:
        kind = "info"
    return {"type": "panel", "attrs": {"panelType": kind}, "content": content}


def expand(title: str, content: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Collapsible section. Keeps a long analysis readable on the ticket."""
    return {"type": "expand", "attrs": {"title": title[:200]},
            "content": content or [paragraph(" ")]}


def bullet_list(items: List[str]) -> Dict[str, Any]:
    return {"type": "bulletList",
            "content": [{"type": "listItem", "content": [paragraph(i)]}
                        for i in items] or
                       [{"type": "listItem", "content": [paragraph(" ")]}]}


def table(rows: List[List[str]], header: bool = True) -> Dict[str, Any]:
    """rows[0] is the header row when `header` is set."""
    content = []
    for n, row in enumerate(rows):
        cell_type = "tableHeader" if (header and n == 0) else "tableCell"
        content.append({"type": "tableRow", "content": [
            {"type": cell_type, "attrs": {}, "content": [paragraph(c)]}
            for c in row]})
    return {"type": "table", "attrs": {"isNumberColumnEnabled": False,
                                       "layout": "default"},
            "content": content}


def doc(content: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"type": "doc", "version": 1,
            "content": content or [paragraph(" ")]}


# ─────────────────────────────────────────────────────── markdown → ADF ────
_FENCE = re.compile(r"^```\s*([A-Za-z0-9_+-]*)\s*$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^\s*[-*+]\s+(.*)$")
_ORDERED = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_QUOTE = re.compile(r"^>\s?(.*)$")
_RULE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_TABLE_ROW = re.compile(r"^\s*\|(.+)\|\s*$")
_TABLE_SEP = re.compile(r"^\s*\|[\s:|-]+\|\s*$")
_PANEL_OPEN = re.compile(r"^:::\s*(%s)\s*$" % "|".join(_PANEL_TYPES))
_EXPAND_OPEN = re.compile(r"^:::\s*expand\s+(.+?)\s*$")
_BLOCK_CLOSE = re.compile(r"^:::\s*$")


def from_markdown(src: str) -> Dict[str, Any]:
    """Render the markdown subset into a complete ADF document."""
    return doc(_blocks((src or "").replace("\r\n", "\n").split("\n")))


def _blocks(lines: List[str]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    para: List[str] = []
    i = 0

    def flush():
        if para:
            out.append(paragraph(" ".join(para).strip()))
            para.clear()

    while i < len(lines):
        line = lines[i]

        m = _FENCE.match(line)
        if m:
            flush()
            lang, body, i = m.group(1), [], i + 1
            while i < len(lines) and not _FENCE.match(lines[i]):
                body.append(lines[i])
                i += 1
            i += 1                                   # consume the closing fence
            out.append(code_block("\n".join(body), lang))
            continue

        m = _PANEL_OPEN.match(line) or _EXPAND_OPEN.match(line)
        if m:
            flush()
            is_panel = bool(_PANEL_OPEN.match(line))
            label, body, i = m.group(1), [], i + 1
            depth = 1
            while i < len(lines):
                if _PANEL_OPEN.match(lines[i]) or _EXPAND_OPEN.match(lines[i]):
                    depth += 1
                elif _BLOCK_CLOSE.match(lines[i]):
                    depth -= 1
                    if depth == 0:
                        break
                body.append(lines[i])
                i += 1
            i += 1
            inner = _blocks(body)
            out.append(panel(label, inner) if is_panel else expand(label, inner))
            continue

        if _RULE.match(line):
            flush()
            out.append(rule())
            i += 1
            continue

        m = _HEADING.match(line)
        if m:
            flush()
            out.append(heading(m.group(2), len(m.group(1))))
            i += 1
            continue

        if _TABLE_ROW.match(line):
            flush()
            rows, i = [], i
            while i < len(lines) and _TABLE_ROW.match(lines[i]):
                if not _TABLE_SEP.match(lines[i]):
                    cells = [c.strip() for c in
                             _TABLE_ROW.match(lines[i]).group(1).split("|")]
                    rows.append(cells)
                i += 1
            if rows:
                out.append(table(rows))
            continue

        if _BULLET.match(line) or _ORDERED.match(line):
            flush()
            ordered = bool(_ORDERED.match(line))
            items = []
            while i < len(lines):
                bm = _ORDERED.match(lines[i]) if ordered else _BULLET.match(lines[i])
                if not bm:
                    break
                items.append(bm.group(1))
                i += 1
            out.append({
                "type": "orderedList" if ordered else "bulletList",
                "content": [{"type": "listItem", "content": [paragraph(x)]}
                            for x in items]})
            continue

        m = _QUOTE.match(line)
        if m:
            flush()
            quoted = []
            while i < len(lines) and _QUOTE.match(lines[i]):
                quoted.append(_QUOTE.match(lines[i]).group(1))
                i += 1
            out.append({"type": "blockquote", "content": _blocks(quoted)})
            continue

        if not line.strip():
            flush()
            i += 1
            continue

        para.append(line.strip())
        i += 1

    flush()
    return out


# ─────────────────────────────────────────────────────── ADF → plain text ────
def to_text(node: Any) -> str:
    """Flatten an ADF document to plain text.

    Used for reading: comment commands, ticket descriptions, and anything from
    the board that has to be matched or passed to the model. Ticket text is
    untrusted input, and it is far easier to reason about a flat string than
    about an arbitrary nested document.
    """
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(to_text(n) for n in node)
    if not isinstance(node, dict):
        return str(node)

    kind = node.get("type")
    if kind == "text":
        return node.get("text", "")
    if kind == "hardBreak":
        return "\n"
    if kind == "mention":
        return "@" + str((node.get("attrs") or {}).get("text", "")).lstrip("@")
    if kind == "emoji":
        return str((node.get("attrs") or {}).get("shortName", ""))
    if kind == "inlineCard":
        return str((node.get("attrs") or {}).get("url", ""))

    inner = to_text(node.get("content"))
    if kind in ("paragraph", "heading", "listItem", "codeBlock", "blockquote",
                "tableRow", "rule", "panel", "expand"):
        return inner + "\n"
    return inner


def truncate(document: Dict[str, Any], limit: int = MAX_DOC_CHARS) -> Dict[str, Any]:
    """Drop trailing blocks until the document fits, and say that it was cut.

    Jira rejects an oversized document with a 400 that names no field, which is
    a miserable thing to debug at three in the morning. Cutting here, visibly,
    is better than a comment that silently never appears.
    """
    import json
    if len(json.dumps(document)) <= limit:
        return document
    content = list(document.get("content") or [])
    while content and len(json.dumps({"type": "doc", "version": 1,
                                      "content": content})) > limit - 400:
        content.pop()
    content.append(panel("warning", [paragraph(
        "This comment was truncated to fit Jira's document size limit. "
        "The complete record is in the Foreman run report and audit archive.")]))
    return {"type": "doc", "version": 1, "content": content}
