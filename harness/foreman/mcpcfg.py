"""
Per-stage MCP access (TapiForge).

Foreman turns MCP off for every stage (`disable_tool_groups: [mcp, subagent]`)
because an MCP server is a route to capabilities the mode never declared. The
mapping stages are the deliberate exception, and they get it the narrow way:

  * the server is written into the *disposable checkout's* `.bob/mcp.json`, never
    into the user's global config, and `.bob/` is removed before any diff is
    taken, so the credential cannot reach a commit;
  * only the stage that needs it runs with the `mcp` group enabled;
  * `alwaysAllow` lists tools by name. While *drafting* mappings (before the
    human gate) only the read tools are auto-approved; the write tools are
    approved only for the apply stage that runs *after* Gate 2.

Names of TapiForge's tools are the client's to supply
(TAPIFORGE_MCP_READ_TOOLS / TAPIFORGE_MCP_WRITE_TOOLS); this module does not
guess them. If they are unset, the mapping stage falls back to the Excel/JSON
hand-off, which needs no MCP at all.

Configure either a remote server (TAPIFORGE_MCP_URL [+ TAPIFORGE_MCP_TOKEN]) or
a local one (TAPIFORGE_MCP_COMMAND [+ TAPIFORGE_MCP_ARGS, TAPIFORGE_MCP_ENV]).
"""
import json
import os
import shlex
from typing import Any, Dict, List, Optional

SERVER_NAME = "tapiforge"


def _csv(name: str) -> List[str]:
    return [x.strip() for x in os.environ.get(name, "").split(",") if x.strip()]


def configured() -> bool:
    return bool(os.environ.get("TAPIFORGE_MCP_URL", "").strip()
                or os.environ.get("TAPIFORGE_MCP_COMMAND", "").strip())


def mode() -> str:
    """'mcp' when TapiForge is reachable, else 'file' (Excel/JSON hand-off)."""
    want = os.environ.get("FOREMAN_MAPPING_MODE", "").strip().lower()
    if want in ("file", "mcp"):
        return want
    return "mcp" if configured() else "file"


def server_entry(allow_write: bool) -> Dict[str, Any]:
    read, write = _csv("TAPIFORGE_MCP_READ_TOOLS"), _csv("TAPIFORGE_MCP_WRITE_TOOLS")
    entry: Dict[str, Any] = {"disabled": False,
                             "alwaysAllow": read + (write if allow_write else [])}
    url = os.environ.get("TAPIFORGE_MCP_URL", "").strip()
    if url:
        # Bob names the transports differently: `url` is SSE, `httpURL` is
        # streamable HTTP. The client says which one their server speaks.
        key = "httpURL" if os.environ.get("TAPIFORGE_MCP_TRANSPORT", "http"
                                          ).strip().lower() in ("http", "streamable-http") else "url"
        entry[key] = url
        token = os.environ.get("TAPIFORGE_MCP_TOKEN", "").strip()
        if token:
            header = os.environ.get("TAPIFORGE_MCP_AUTH_HEADER", "Authorization").strip()
            scheme = os.environ.get("TAPIFORGE_MCP_AUTH_SCHEME", "Bearer").strip()
            entry["headers"] = {header: ("%s %s" % (scheme, token)).strip()}
    else:
        entry["command"] = os.environ.get("TAPIFORGE_MCP_COMMAND", "").strip()
        args = os.environ.get("TAPIFORGE_MCP_ARGS", "").strip()
        if args:
            entry["args"] = shlex.split(args)
        env = os.environ.get("TAPIFORGE_MCP_ENV", "").strip()
        if env:
            # KEY=value,KEY2=value2 -- the MCP server's own credentials. They
            # live in the child process of the MCP server only.
            entry["env"] = dict(p.split("=", 1) for p in env.split(",") if "=" in p)
    timeout = os.environ.get("TAPIFORGE_MCP_TIMEOUT_MS", "").strip()
    if timeout.isdigit():
        entry["timeout"] = int(timeout)
    return entry


def install(workdir: str, allow_write: bool) -> Optional[str]:
    """Write `.bob/mcp.json` into the checkout. Returns the path, or None."""
    if not configured():
        return None
    d = os.path.join(workdir, ".bob")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "mcp.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"mcpServers": {SERVER_NAME: server_entry(allow_write)}}, fh,
                  indent=2)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def secrets() -> List[str]:
    """Values that must never appear in a log, a comment, or Jira."""
    out = [os.environ.get("TAPIFORGE_MCP_TOKEN", "").strip()]
    for pair in os.environ.get("TAPIFORGE_MCP_ENV", "").split(","):
        if "=" in pair:
            out.append(pair.split("=", 1)[1].strip())
    return [s for s in out if len(s) >= 6]


def scrub(text: str) -> str:
    for s in secrets():
        text = (text or "").replace(s, "***")
    return text
