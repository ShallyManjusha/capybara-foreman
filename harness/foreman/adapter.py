"""
Adapter isolating every Bob Shell invocation detail behind one interface.

Everything version-specific about the CLI lives here and nowhere else, so a Bob
Shell upgrade is a change to this file rather than to the pipeline.

**Two CLI generations, one interface.** Bob Shell changed shape at 2.0: the
headless entrypoint moved from top-level flags to a `run` subcommand, and
several flags were renamed or dropped.

    1.0.x   bob --chat-mode S --output-format json --max-coins N \
                --allowed-tools ... --approval-mode ... --sandbox "<task>"
    2.x     bob run --mode S --format json --max-cost N --max-turns N \
                -w <path> --disable-tool-groups mcp,subagent "<task>"

The dialect is detected from `--help`, not assumed and not configured. A fleet
runs mixed versions for months during a rollout, and a harness that works only
on the version its author had installed is a harness that fails on the first
machine that is not theirs.

Notably, 2.x has **no `--allowed-tools`**. Tool restriction moved into the mode
definition's `groups`, which is a better place for it: the restriction travels
with the mode instead of depending on every caller remembering the flag. Read
modes declare `groups: [read]`; the build mode declares an `edit` group scoped
by `fileRegex`. The harness still re-checks the resulting diff, because a
declaration is not a control.

**Two tool profiles.** Foreman's analysis stages get a read-only toolset. The
build stage genuinely has to write files and run the test command, so it gets a
wider one — and that is precisely why the harness never trusts what comes back.
The build runs inside a disposable worktree; afterwards the harness diffs that
worktree and enforces the change budget and path allowlist on the *result*. A
model that writes outside its brief produces a rejected diff, not a surprise
commit. Asking the model nicely to stay in scope is a prompt; checking the diff
is a control.
"""
import json
import os
import re
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional

READ_TOOLS = ["read_file", "read_many_files", "glob", "search_file_content"]
WRITE_TOOLS = READ_TOOLS + ["write_file", "replace", "run_shell_command"]


def _is_general_key(key: str) -> bool:
    """Best-effort: only 'general' keys need a team id, and the type is in the
    key's own prefix. Wrong guesses cost a clearer error message, nothing more."""
    return "_bob-apikey_" in key or "_bob-admin_" in key or "_general_" in key


def api_key() -> str:
    """The Bob credential, under either generation's variable name."""
    return (os.environ.get("BOB_API_KEY", "")
            or os.environ.get("BOBSHELL_API_KEY", "")).strip()


class BobResult:
    def __init__(self, ok, payload, raw_stdout, raw_stderr, exit_code,
                 duration_sec, error=None, error_kind=None, cost=0.0,
                 tool_calls=0):
        self.ok = ok
        self.payload = payload
        self.raw_stdout = raw_stdout
        self.raw_stderr = raw_stderr
        self.exit_code = exit_code
        self.duration_sec = duration_sec
        self.error = error
        # budget_exceeded | timeout | bad_json | nonzero_exit | not_found |
        # bad_invocation | auth_failed
        self.error_kind = error_kind
        # What the run actually cost, as reported by Bob rather than estimated
        # here. A budget you cannot account for after the fact is a budget you
        # cannot tune.
        self.cost = cost
        self.tool_calls = tool_calls

    def as_dict(self):
        return {"ok": self.ok, "exit_code": self.exit_code,
                "duration_sec": round(self.duration_sec, 2),
                "cost": round(self.cost, 4), "tool_calls": self.tool_calls,
                "error": self.error, "error_kind": self.error_kind}


class BobAdapter:
    def __init__(self, cfg: Dict[str, Any]):
        b = (cfg or {}).get("bob", {}) or {}
        self.binary = b.get("binary", "bob")
        # "auto" | "v1" | "v2". Override only to reproduce a historical run.
        self.dialect_pref = str(b.get("dialect", "auto")).lower()
        self.max_turns = int(b.get("max_turns", 60) or 60)
        self.disable_tool_groups = b.get("disable_tool_groups") or ["mcp", "subagent"]
        # 2.x refuses to start with a "general" API key unless it is told which
        # team to bill and authorise against. Interactive SSO sessions carry it
        # implicitly; a headless run has to say it out loud.
        self.team_id = (b.get("team_id") or os.environ.get("BOB_TEAM_ID") or "").strip()
        self.model = b.get("model") or os.environ.get("FOREMAN_MODEL") or None
        self.approval_mode = b.get("approval_mode", "default")
        self.write_approval_mode = b.get("write_approval_mode", "yolo")
        self.sandbox = b.get("sandbox", True)
        self.accept_license = b.get("accept_license", True)
        self.hide_intermediary = b.get("hide_intermediary_output", True)
        self.output_format = b.get("output_format", "json")
        self.read_tools = b.get("read_tools") or list(READ_TOOLS)
        self.write_tools = b.get("write_tools") or list(WRITE_TOOLS)
        self._caps: Optional[Dict[str, Any]] = None

    # ──────────────────────────────────────────────────────────────── probe ──
    def capabilities(self) -> Dict[str, Any]:
        """Probe --help once and cache which flags this build supports.

        Prevents a Bob Shell upgrade that renames or drops a flag from silently
        producing runs with no budget cap or no tool restrictions — a failure
        that looks exactly like success until the bill arrives.
        """
        if self._caps is not None:
            return self._caps
        if not shutil.which(self.binary):
            self._caps = {"_present": False}
            return self._caps
        try:
            out = subprocess.run([self.binary, "--help"], capture_output=True,
                                 text=True, timeout=60).stdout
        except Exception:
            self._caps = {"_present": True}
            return self._caps
        # `bob run --help` is where 2.x keeps the headless flags; the
        # top-level help lists only subcommands.
        try:
            run_out = subprocess.run([self.binary, "run", "--help"],
                                     capture_output=True, text=True,
                                     timeout=60).stdout
        except Exception:
            run_out = ""
        combined = out + "\n" + run_out

        caps: Dict[str, Any] = {}
        for flag in ("--chat-mode", "--max-coins", "--output-format",
                     "--hide-intermediary-output", "--allowed-tools",
                     "--approval-mode", "--sandbox", "--accept-license",
                     "--trust", "--include-directories", "--model",
                     "--mode", "--max-cost", "--max-turns", "--format",
                     "--workspace", "--disable-tool-groups", "--disable-mcp",
                     "--disable-subagents", "--team-id"):
            caps[flag] = flag in combined
        caps["_present"] = True
        caps["_version"] = self._version()
        caps["_has_run"] = bool(re.search(r"^\s*run\s", out, re.M)) or bool(run_out)
        caps["_dialect"] = self._detect_dialect(caps)
        self._caps = caps
        return caps

    def _detect_dialect(self, caps: Dict[str, Any]) -> str:
        if self.dialect_pref in ("v1", "v2"):
            return self.dialect_pref
        if caps.get("--chat-mode") and caps.get("--max-coins"):
            return "v1"
        if caps.get("_has_run") and caps.get("--mode"):
            return "v2"
        # Unknown build: prefer the newer shape, and let preflight complain
        # loudly rather than silently running with no budget cap.
        return "v2" if caps.get("_has_run") else "v1"

    @property
    def dialect(self) -> str:
        return self.capabilities().get("_dialect", "v1")

    def _version(self) -> str:
        try:
            return subprocess.run([self.binary, "--version"], capture_output=True,
                                  text=True, timeout=30).stdout.strip()
        except Exception:
            return "unknown"

    def preflight(self) -> List[str]:
        """Blocking problems. Empty list means good to run."""
        problems = []
        caps = self.capabilities()
        if not caps.get("_present"):
            problems.append("bob binary not found on PATH: %s" % self.binary)
            return problems
        ver = str(caps.get("_version", "?")).splitlines()[0]
        required = (("--chat-mode", "--max-coins", "--output-format")
                    if self.dialect == "v1"
                    else ("--mode", "--max-cost", "--format"))
        for flag in required:
            if not caps.get(flag):
                problems.append(
                    "bobshell %s (%s dialect) does not support %s; the harness "
                    "relies on it" % (ver, self.dialect, flag))
        key = api_key()
        if not key:
            problems.append(
                "neither BOB_API_KEY nor BOBSHELL_API_KEY is set in the "
                "environment (2.x reads BOB_API_KEY)")
        elif self.dialect == "v2" and not self.team_id and _is_general_key(key):
            problems.append(
                "BOB_TEAM_ID is not set. Bob Shell 2.x refuses a headless run "
                "with a general API key unless --team-id is supplied. Find it "
                "on your Bob team page and add BOB_TEAM_ID to .env.")
        elif os.path.exists(key):
            problems.append(
                "BOBSHELL_API_KEY looks like a file path. Inject the secret "
                "value at runtime from your secret store; never read it from a "
                "file inside the repository.")
        return problems

    # ────────────────────────────────────────────────────────────────── run ──
    # NOTE ON UNITS. 1.0.x `--max-coins` counts coins; 2.x `--max-cost` is a
    # currency amount. They are not interchangeable, so `budget` here is
    # whatever unit the configured dialect uses, and `foreman.yaml` carries a
    # separate figure for each. Passing a coin count as a cost cap is how a
    # stage silently gets a budget three orders of magnitude off.
    def build_argv(self, mode: str, prompt: str, budget: float,
                   write: bool = False, workspace: str = "",
                   include_dirs: Optional[List[str]] = None,
                   mcp: bool = False) -> List[str]:
        if self.dialect == "v2":
            return self._argv_v2(mode, prompt, budget, write, workspace, mcp)
        return self._argv_v1(mode, prompt, budget, write, include_dirs)

    def _argv_v2(self, mode, prompt, budget, write, workspace,
                 mcp=False) -> List[str]:
        caps = self.capabilities()

        def sup(flag):
            return caps.get(flag, True)

        argv = [self.binary, "run"]
        if sup("--mode"):
            argv += ["--mode", mode]
        if sup("--format"):
            argv += ["--format", self.output_format]
        if sup("--max-cost"):
            argv += ["--max-cost", str(budget)]
        if sup("--max-turns"):
            argv += ["--max-turns", str(self.max_turns)]
        if workspace and sup("--workspace"):
            argv += ["--workspace", workspace]
        # Trim the blast radius to what a stage actually needs. Subagents and
        # MCP servers are both ways for a stage to reach capabilities the mode
        # never declared, which is the thing the mode's `groups` exist to bound.
        # `mcp=True` is the per-stage opt-in (TapiForge mapping only). It lifts
        # the MCP restriction for that one invocation and leaves the rest.
        groups = [g for g in self.disable_tool_groups if not (mcp and g == "mcp")]
        if groups and sup("--disable-tool-groups"):
            argv += ["--disable-tool-groups", ",".join(groups)]
        if self.team_id and sup("--team-id"):
            argv += ["--team-id", self.team_id]
        if sup("--trust"):
            argv += ["--trust"]
        if self.accept_license and sup("--accept-license"):
            argv += ["--accept-license"]
        argv.append(prompt)
        return argv

    def _argv_v1(self, mode: str, prompt: str, budget: float,
                 write: bool = False,
                 include_dirs: Optional[List[str]] = None) -> List[str]:
        caps = self.capabilities()

        def sup(flag):
            return caps.get(flag, True)

        argv = [self.binary]
        if sup("--chat-mode"):
            argv += ["--chat-mode", mode]
        if self.model and sup("--model"):
            argv += ["--model", self.model]
        if sup("--output-format"):
            argv += ["--output-format", self.output_format]
        if self.hide_intermediary and sup("--hide-intermediary-output"):
            argv += ["--hide-intermediary-output"]
        if sup("--max-coins"):
            argv += ["--max-coins", str(int(budget))]

        tools = self.write_tools if write else self.read_tools
        if tools and sup("--allowed-tools"):
            argv += ["--allowed-tools"] + list(tools)
        if sup("--approval-mode"):
            # A write stage cannot stop for a confirmation: there is no TTY, so
            # a prompt is a hang, and a hang in cron is an outage. It is safe
            # only because the sandbox is disposable and the diff is audited
            # afterwards by the harness.
            argv += ["--approval-mode",
                     self.write_approval_mode if write else self.approval_mode]
        if self.sandbox and sup("--sandbox"):
            argv += ["--sandbox"]
        if self.accept_license and sup("--accept-license"):
            argv += ["--accept-license"]
        if sup("--trust"):
            argv += ["--trust"]
        if include_dirs and sup("--include-directories"):
            argv += ["--include-directories", ",".join(include_dirs)]

        argv.append(prompt)          # positional LAST; -p is deprecated in 1.0.6
        return argv

    def run(self, mode: str, prompt: str, cwd: str, budget: int,
            timeout_sec: int = 1800, write: bool = False,
            include_dirs: Optional[List[str]] = None,
            mcp: bool = False) -> BobResult:
        argv = self.build_argv(mode, prompt, budget, write,
                               workspace=cwd, include_dirs=include_dirs, mcp=mcp)
        started = time.time()
        try:
            proc = subprocess.run(
                argv, cwd=cwd, env=self._child_env(), capture_output=True,
                text=True, timeout=timeout_sec,
                # Never inherit a TTY: a confirmation prompt must fail the run
                # rather than block a scheduled job forever.
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return BobResult(False, None, "", "", -1, time.time() - started,
                             "timed out after %ss" % timeout_sec, "timeout")
        except FileNotFoundError:
            return BobResult(False, None, "", "", -1, time.time() - started,
                             "bob binary not found", "not_found")

        dur = time.time() - started
        if proc.returncode != 0 and not (proc.stdout or "").strip():
            blob = ((proc.stderr or "") + " " + (proc.stdout or "")).strip()
            low = blob.lower()
            if "coin" in low or "budget" in low:
                kind, msg = "budget_exceeded", "max-coins budget exceeded"
            elif "invalid values" in low or "not enough non-option" in low:
                kind = "bad_invocation"
                msg = ("bob rejected the invocation — is the mode installed in "
                       "the workspace? Run extension/install.sh against the "
                       "checkout. bob said: " + blob[:300])
            elif "auth" in low or "credential" in low or "token" in low:
                kind, msg = "auth_failed", "authentication failed: " + blob[:300]
            else:
                kind = "nonzero_exit"
                msg = "exit %d: %s" % (proc.returncode, blob[:300] or "(no output)")
            return BobResult(False, None, proc.stdout, proc.stderr,
                             proc.returncode, dur, msg, kind)

        cost, calls = extract_stats(proc.stdout)
        payload = extract_json(proc.stdout)
        if payload is None:
            return BobResult(False, None, proc.stdout, proc.stderr,
                             proc.returncode, dur,
                             "no parseable JSON object in stdout", "bad_json",
                             cost, calls)
        if proc.returncode != 0:
            return BobResult(False, payload, proc.stdout, proc.stderr,
                             proc.returncode, dur,
                             "non-zero exit %d" % proc.returncode,
                             "nonzero_exit", cost, calls)
        return BobResult(True, payload, proc.stdout, proc.stderr, 0, dur,
                         cost=cost, tool_calls=calls)

    def _child_env(self) -> Dict[str, str]:
        """Minimal environment for the child.

        The model tier gets its own API key and nothing else. In particular it
        never sees JIRA_API_TOKEN or GITHUB_WRITE_TOKEN — it is reasoning about
        ticket text written by people outside the team, and that text must not
        be one clever paragraph away from a credential.
        """
        keep = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SHELL",
                "BOB_API_KEY", "BOBSHELL_API_KEY", "BOB_DIR", "BOB_TEAM_ID",
                "NO_COLOR")
        env = {k: v for k, v in os.environ.items() if k in keep}
        # 1.0.x reads BOBSHELL_API_KEY, 2.x reads BOB_API_KEY. Set whichever
        # the operator did not, so one .env works across a mixed fleet.
        key = api_key()
        if key:
            env.setdefault("BOB_API_KEY", key)
            env.setdefault("BOBSHELL_API_KEY", key)
        env["NO_COLOR"] = "1"
        env["CI"] = "1"
        env["GIT_TERMINAL_PROMPT"] = "0"
        return env


# ─────────────────────────────────────────────────────────────────── parsing ─
_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)
_ENVELOPE_KEYS = ("analysis", "plan", "implementation", "verification",
                  "review", "findings", "verdict")


def extract_json(text: str) -> Optional[Dict[str, Any]]:
    """Recover the result object from stdout.

    Tolerant by design: the modes are instructed to emit bare JSON, but a stray
    fence or a line of prose must not discard a whole stage's work.
    """
    if not text or not text.strip():
        return None
    t = text.strip()
    try:
        obj = json.loads(t)
        if isinstance(obj, dict):
            return _unwrap(obj)
    except Exception:
        pass
    m = _FENCE.search(t)
    if m:
        try:
            return _unwrap(json.loads(m.group(1)))
        except Exception:
            pass
    best = None
    for cand in _balanced_objects(t):
        try:
            obj = json.loads(cand)
        except Exception:
            continue
        if isinstance(obj, dict):
            obj = _unwrap(obj)
            if any(k in obj for k in _ENVELOPE_KEYS):
                best = obj
    return best


def extract_stats(text: str):
    """Pull cost and tool-call count out of the 2.x result envelope."""
    try:
        obj = json.loads((text or "").strip())
    except Exception:
        return 0.0, 0
    stats = obj.get("stats") if isinstance(obj, dict) else None
    if not isinstance(stats, dict):
        return 0.0, 0
    try:
        return float(stats.get("session_costs") or 0.0), \
            int(stats.get("tool_calls") or 0)
    except (TypeError, ValueError):
        return 0.0, 0


def _unwrap(obj: Dict[str, Any]) -> Dict[str, Any]:
    """bob --output-format json may wrap the model's answer in an envelope."""
    if any(k in obj for k in _ENVELOPE_KEYS):
        return obj
    # `last_message` is where Bob Shell 2.x puts the model's final text, inside
    # a result envelope that also carries status and cost. 1.0.x used the
    # others. Checking all of them is what lets one adapter serve both.
    for key in ("last_message", "response", "result", "output", "content",
                "message", "data"):
        v = obj.get(key)
        if isinstance(v, dict) and any(k in v for k in _ENVELOPE_KEYS):
            return v
        if isinstance(v, str):
            inner = extract_json(v)
            if inner:
                return inner
    return obj


def _balanced_objects(text: str):
    depth, start, in_str, esc = 0, None, False, False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                yield text[start:i + 1]
                start = None
