"""
The stages, and what the harness does with what each one returns.

One rule organises this module: **Bob Shell proposes, the harness disposes.**

  triage   the model reads the code      → the harness re-reads every citation
  plan     the model proposes a change   → an adversarial pass argues against it
  (gate)   a human decides               → the harness verifies who, and when
  build    the model writes the code     → the harness diffs it and can revert
  deploy   your pipeline deploys         → the harness records its verdict
  verify   the model writes tests        → the harness runs them
  (gate)   a human signs off             → the harness verifies who, and when
  close    the harness closes the ticket and schedules production

Nothing in the middle column is trusted. Everything in the right column is
either a file read, a subprocess exit code, or an identity check — things a
model cannot argue with.

Dry runs go through exactly the same code path and stop at the moment of
writing. That is on purpose: a rehearsal that takes a different route through
the code rehearses a different system.
"""
import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from . import contract, deploy as deploy_mod, gate as gate_mod, gitops
from . import guardrails as guard_mod, machine, mcpcfg, prompts, render, stories
from .adapter import BobAdapter
from .jira import JiraError

MAX_STATE_BYTES = 28000          # Jira issue properties are not a database


class Context:
    """Everything a stage needs, assembled once per run."""
    def __init__(self, cfg, jira, store, run_id, live, paths, gate_cfg,
                 bot_account_id="", gh=None, repo_source="", base_branch="main",
                 branch_prefix="foreman/", test_command="", deploy_cfg=None,
                 guardrails=None, github_repo="", log_dir="",
                 build_test_command=None):
        self.cfg = cfg
        self.jira = jira
        self.store = store
        self.run_id = run_id
        self.live = live
        self.paths = paths
        self.gate_cfg = gate_cfg
        self.bot_account_id = bot_account_id
        self.gh = gh
        self.repo_source = repo_source
        self.github_repo = github_repo
        self.base_branch = base_branch
        self.branch_prefix = branch_prefix
        self.test_command = test_command
        # What the build stage runs before publishing a branch. Separate from
        # `test_command` (run against DEV after the deploy) because a suite that
        # needs a live environment cannot gate a branch that is not deployed
        # yet. None means "not configured": fall back to `test_command`, which
        # is what a single-environment setup wants.
        self.build_test_command = test_command if build_test_command is None \
            else build_test_command
        self.deploy_cfg = deploy_cfg or {}
        self.guardrails = guardrails or guard_mod.Guardrails()
        self.log_dir = log_dir
        self.bob = BobAdapter(cfg)
        self.modes = (cfg.get("modes") or {})
        self.budget = (cfg.get("budget") or {})
        # What this run has actually cost, as reported by Bob. A budget you
        # cannot account for afterwards is a budget you cannot tune.
        self.spend = 0.0

    def budget_for(self, stage: str) -> float:
        key = "max_cost_per_stage" if self.bob.dialect == "v2" \
            else "max_coins_per_stage"
        table = self.budget.get(key) or {}
        return float(table.get(stage, table.get("default", 2.0)))

    def timeout_for(self, stage: str) -> int:
        table = self.budget.get("timeout_sec") or {}
        return int(table.get(stage, table.get("default", 1800)))


class Outcome:
    def __init__(self, next_state, reason, comment="", sticky="", ok=True,
                 detail="", artifacts=None, cost=0.0):
        self.next_state = next_state
        self.reason = reason
        self.comment = comment
        self.sticky = sticky        # marker kind; rewritten in place in FOREMAN_COMMENT_MODE=update
        self.ok = ok
        self.detail = detail
        self.artifacts = artifacts or {}
        self.cost = cost

    def as_dict(self):
        return {"next_state": self.next_state, "reason": self.reason,
                "ok": self.ok, "detail": self.detail[:600],
                "artifacts": {k: v for k, v in self.artifacts.items()
                              if k not in ("patch", "stdout")}}


# ───────────────────────────────────────────────────────────── ticket model ──
def load_ticket(ctx: Context, issue: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise a Jira issue into the flat shape the stages and prompts use."""
    from .markup import to_text as body_text
    f = issue.get("fields") or {}
    key = issue.get("key", "")
    comments = ctx.jira.comments(key)
    state = ctx.jira.get_state(key)
    return {
        "key": key,
        "summary": f.get("summary") or "",
        "description": body_text(f.get("description")),
        "issue_type": ((f.get("issuetype") or {}).get("name") or ""),
        "priority": ((f.get("priority") or {}).get("name") or ""),
        "reporter": ((f.get("reporter") or {}).get("displayName") or ""),
        "status": ((f.get("status") or {}).get("name") or ""),
        "labels": list(f.get("labels") or []),
        "updated": f.get("updated"),
        "comments": comments,
        "comment_digest": prompts.comment_digest(comments, bot_account_id=ctx.bot_account_id),
        "state": state,
        "fsm": state.get("state") or machine.INTAKE,
        "revision": int(state.get("revision") or 0),
    }


def save_state(ctx: Context, ticket: Dict[str, Any], **updates) -> bool:
    """Persist Foreman's state onto the ticket itself.

    Trimmed to fit: Jira issue properties are a small key-value store, not a
    database. The full record of every stage lives in the audit archive; what
    goes here is only what a *resuming* run needs to continue correctly.
    """
    state = dict(ticket.get("state") or {})
    state.update(updates)
    state["updated"] = gate_mod.now_iso()
    state["run_id"] = ctx.run_id

    if len(json.dumps(state)) > MAX_STATE_BYTES:
        # Shed the bulkiest, most reconstructible parts first. Code snippets
        # and plan prose are in the audit archive; what a resuming run needs is
        # the shape of the analysis, not every quoted line of it.
        analysis = state.get("analysis")
        if isinstance(analysis, dict):
            for ev in (analysis.get("evidence") or []):
                ev["snippet"] = str(ev.get("snippet", ""))[:200]
        plan = state.get("plan")
        if isinstance(plan, dict) and len(json.dumps(state)) > MAX_STATE_BYTES:
            plan.pop("steps", None)
        decomp = state.get("decomposition")
        if isinstance(decomp, dict) and len(json.dumps(state)) > MAX_STATE_BYTES:
            for ep in decomp.get("epics", []):
                ep["description"] = ep.get("description", "")[:200]
                for sd in ep.get("stories", []):
                    sd["description"] = sd.get("description", "")[:160]
        if len(json.dumps(state)) > MAX_STATE_BYTES:
            state.pop("diff", None)
            state.pop("review", None)
        state["_trimmed"] = True
    ticket["state"] = state
    if not ctx.live:
        return True
    return ctx.jira.put_state(ticket["key"], state)


# ─────────────────────────────────────────────────────────────── dispatcher ──
def advance(ctx: Context, ticket: Dict[str, Any]) -> Outcome:
    """Run whatever the ticket's current state calls for. One step, never two.

    Deliberately single-step. A ticket moves at most one state per run, even
    when the next stage could start immediately. It keeps every run bounded,
    it makes the board legible to anyone watching it move, and it means an
    operator can stop the pipeline between any two stages by stopping the cron.
    """
    current = ticket["fsm"]
    state = machine.get(current)

    if state.terminal:
        return Outcome(current, "terminal state; nothing to do", ok=True)
    if state.gate:
        return _gate(ctx, ticket)
    if state.human:
        return _human_hold(ctx, ticket)

    handler = {"triage": _triage, "decompose": _decompose,
               "materialise": _materialise, "track": _track,
               "mapping": _mapping, "mapping-apply": _mapping_apply,
               "plan": _plan, "build": _build, "codereview": _codereview,
               "merge": _merge, "deploy": _deploy, "devtest": _devtest,
               "close": _close}.get(state.stage)
    if handler is None:
        return Outcome("failed", "no handler for stage %r" % state.stage, ok=False)
    try:
        out = handler(ctx, ticket)
    except contract.Rejection as e:
        out = _fail(ctx, ticket, state.stage or "?",
                    "The %s stage produced output the harness would not accept."
                    % state.stage, "%s: %s" % (e.code, e.detail))
    except (JiraError, gitops.GitError) as e:
        out = _fail(ctx, ticket, state.stage or "?",
                    "An external system refused an operation.", str(e))
    except Exception as e:                       # never lose a whole run
        out = _fail(ctx, ticket, state.stage or "?",
                    "The harness hit an unexpected error.",
                    "%s: %s" % (type(e).__name__, e))
    if out.next_state == "failed" and not out.ok:
        # Persist the failure, and *where* it happened. Without this the board
        # label says `failed` while the issue property still says the old state,
        # and the next run happily retries the stage that just failed -- and
        # `failed_from` is what lets a human's "fixed it" resume a merge or a
        # deploy instead of re-analysing the whole ticket.
        save_state(ctx, ticket, state="failed", failed_from=current,
                   failure=out.reason[:300])
    return out


def _fail(ctx, ticket, stage, reason, detail, violations=None) -> Outcome:
    return Outcome("failed", reason, ok=False, detail=detail,
                   comment=render.failure_comment(
                       ticket["key"], stage, reason, detail,
                       ticket["revision"], ctx.run_id, violations),
                   sticky="failed")


# ───────────────────────────────────────────────────────────── bob plumbing ──
def _invoke(ctx: Context, stage: str, mode_key: str, ticket: Dict[str, Any],
            workdir: str, write: bool = False, mcp: bool = False, **prompt_kw):
    mode = ctx.modes.get(mode_key, "foreman-%s" % mode_key)
    task = prompts.build(stage, ticket, ctx.github_repo or ctx.repo_source,
                         prompt_kw.pop("commit", ""), revision=ticket["revision"],
                         **prompt_kw)
    res = ctx.bob.run(mode, task, cwd=workdir, budget=ctx.budget_for(stage),
                      timeout_sec=ctx.timeout_for(stage), write=write, mcp=mcp)
    if mcp:
        # Whatever the MCP server echoed back must not reach a log or a comment
        # carrying the credential it was started with.
        res.raw_stdout = mcpcfg.scrub(res.raw_stdout or "")
        res.raw_stderr = mcpcfg.scrub(res.raw_stderr or "")
        res.error = mcpcfg.scrub(res.error or "") or res.error
    _log(ctx, ticket["key"], stage, mode, task, res)
    ctx.spend += res.cost
    ctx.store.record_stage(ctx.run_id, ticket["key"], stage, mode,
                           "ok" if res.ok else "errored", res.duration_sec,
                           ticket["revision"], res.error or "",
                           res.error_kind or "",
                           {"cost": res.cost, "tool_calls": res.tool_calls})
    print("      %-7s %-16s %5.1fs  $%.4f  %d tool call(s)%s"
          % (stage, mode, res.duration_sec, res.cost, res.tool_calls,
             "" if res.ok else "  ERRORED: %s" % (res.error_kind or "")))
    return res


def _log(ctx: Context, key: str, stage: str, mode: str, task: str, res):
    """Full prompt/response trail. Required for injection forensics.

    Never overwrites. When a ticket goes round the revise loop three times,
    all three prompts must survive — the interesting one is rarely the last.
    """
    if not ctx.log_dir:
        return
    d = os.path.join(ctx.log_dir, key)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "%s.json" % stage)
    seq = 1
    while os.path.exists(path):
        seq += 1
        path = os.path.join(d, "%s.%d.json" % (stage, seq))
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"ticket": key, "stage": stage, "mode": mode,
                       "run_id": ctx.run_id, "call": seq, "task": task,
                       "result": res.as_dict(),
                       "stdout": (res.raw_stdout or "")[:200000],
                       "stderr": (res.raw_stderr or "")[:20000]}, fh, indent=2)
    except OSError:
        pass


def _workspace(ctx: Context, ticket: Dict[str, Any], suffix: str = "") -> str:
    """A fresh read-only checkout for an analysis stage."""
    dest = os.path.join(ctx.paths["workdir"],
                        ticket["key"] + (("-" + suffix) if suffix else ""))
    gitops.clone(ctx.repo_source, dest, ctx.base_branch,
                 os.environ.get("GITHUB_READ_TOKEN", ""))
    _install_kit(ctx, dest)
    return dest


# What extension/install.sh writes into a checkout. Foreman's own scaffolding,
# not part of anybody's change.
KIT_PATHS = (".bob", "AGENTS.md", "AGENTS.repo.md", ".bobignore")


def _install_kit(ctx: Context, dest: str):
    """Install Foreman's modes and rules into the checkout.

    `bob --mode foreman-*` resolves modes from the workspace's own
    `.bob/custom_modes.yaml`, so without this every stage fails argument
    validation instantly — and it fails in a way whose error message does not
    mention modes at all.
    """
    kit = ctx.paths.get("install_kit")
    if kit and os.path.exists(kit):
        import subprocess
        subprocess.run(["bash", kit, dest], capture_output=True, text=True,
                       timeout=120)


def _remove_kit(work: str):
    """Take Foreman's scaffolding back out before the diff is measured.

    Without this, every build stage "changes" eight files under `.bob/` — and
    the guardrails correctly block it, because `.bob/` is a denied path. The
    harness would be failing every ticket on its own footprint, and the error
    message would look exactly like the model misbehaving.

    Tracked files the installer replaced are restored from HEAD; its own files
    are deleted. What remains in the worktree is the change and nothing else.
    """
    import shutil
    # AGENTS.md and .bobignore may have existed in the repository and been
    # replaced. `git checkout --` is a no-op for paths HEAD does not know.
    gitops.run(work, "checkout", "--", "AGENTS.md", ".bobignore")
    tracked = set(gitops.file_list(work))
    for rel in KIT_PATHS:
        if rel in tracked:
            continue
        full = os.path.join(work, rel)
        if os.path.isdir(full):
            shutil.rmtree(full, ignore_errors=True)
        elif os.path.exists(full):
            try:
                os.remove(full)
            except OSError:
                pass


# ────────────────────────────────────────────────────────────────── triage ──
def _triage(ctx: Context, ticket: Dict[str, Any]) -> Outcome:
    kind = stories.kind_of(ticket["labels"])
    if kind == "requirement":
        # A requirement is not a defect: there is no root cause to find, and a
        # brand-new integration has no code to cite. Impact analysis happens in
        # the decompose stage, which reads the repository itself.
        save_state(ctx, ticket, state="decomposing", kind=kind)
        return Outcome("decomposing", "business requirement; drafting the "
                       "Feature / Epic / Story breakdown")
    if kind == "mapping":
        save_state(ctx, ticket, state="mapping", kind=kind)
        return Outcome("mapping", "mapping story; reading the current mappings")
    if kind != "dev":
        return Outcome(ticket["fsm"],
                       "a '%s' story is done by a person; Foreman only tracks "
                       "it. Remove the `foreman` label." % kind)
    work = _workspace(ctx, ticket)
    commit = gitops.head(work)
    res = _invoke(ctx, "triage", "triage", ticket, work, commit=commit)
    if not res.ok and res.payload is None:
        return _fail(ctx, ticket, "triage",
                     "Bob Shell did not return a usable analysis.",
                     "%s — %s" % (res.error_kind, res.error))

    validated = contract.validate_triage(res.payload, work)
    a = validated.data

    if not a["understood"]:
        save_state(ctx, ticket, state="needs-info", analysis=a,
                   commit=commit, revision=ticket["revision"])
        return Outcome("needs-info",
                       "the ticket does not contain enough to act on",
                       comment=render.needs_info_comment(
                           ticket["key"], a, ticket["revision"], ctx.run_id),
                       sticky="needs-info",
                       artifacts={"analysis": a, "commit": commit})

    save_state(ctx, ticket, state="analyzed", analysis=a, commit=commit,
               rejections=validated.rejections)
    return Outcome("analyzed",
                   "root cause established with %d verified citation(s)"
                   % len(a["evidence"]),
                   artifacts={"analysis": a, "commit": commit,
                              "rejections": validated.rejections,
                              "corrections": validated.corrections})



# ─────────────────────────────────────────────────────────── decompose ──
def _decompose(ctx: Context, ticket: Dict[str, Any]) -> Outcome:
    """Impact analysis, then a Feature / Epic / Story draft for a human to approve.

    Nothing is created in Jira here. Gate 1 sits between this stage and the
    fan-out: one wrong decomposition would otherwise spawn a sprint of work
    before anyone had read it.
    """
    st = ticket["state"]
    work = _workspace(ctx, ticket)
    commit = gitops.head(work)

    res = _invoke(ctx, "decompose", "decompose", ticket, work, commit=commit,
                  candidate=st.get("decomposition") if st.get("feedback") else None,
                  feedback=st.get("feedback", ""), reviewer=st.get("reviewer", ""))
    if not res.ok and res.payload is None:
        return _fail(ctx, ticket, "decompose",
                     "Bob Shell did not return a decomposition.",
                     "%s — %s" % (res.error_kind, res.error))

    validated = contract.validate_decomposition(res.payload)
    d = validated.data

    if not d.get("understood", True):
        save_state(ctx, ticket, state="needs-info", decomposition=None)
        return Outcome("needs-info", "the requirement is not clear enough to decompose",
                       comment=render.needs_info_comment(
                           ticket["key"], d, ticket["revision"], ctx.run_id),
                       sticky="needs-info")

    revision = ticket["revision"] + (1 if st.get("feedback") else 0)
    opened = gate_mod.now_iso()
    comment = render.decomposition_comment(
        ticket["key"], d, revision, ctx.run_id, ctx.gate_cfg.cmd_approve,
        ctx.gate_cfg.cmd_changes, ctx.gate_cfg.cmd_reject,
        validated.corrections, validated.rejections)
    save_state(ctx, ticket, state="awaiting-epic-approval", decomposition=d,
               commit=commit, revision=revision, gate_opened=opened,
               feedback="", reviewer="")
    ticket["revision"] = revision
    n_stories = sum(len(e["stories"]) for e in d["epics"])
    return Outcome("awaiting-epic-approval",
                   "%d epic(s) / %d stories drafted for approval"
                   % (len(d["epics"]), n_stories),
                   comment=comment, sticky="decompose",
                   artifacts={"decomposition": d, "gate_opened": opened,
                              "corrections": validated.corrections})


def _materialise(ctx: Context, ticket: Dict[str, Any]) -> Outcome:
    """Create the Feature, Epics and Stories that Gate 1 approved.

    Resumable: every Jira call is recorded as it happens, so a failure half-way
    (a refused issue type, a permissions error) is fixed and re-run without
    creating the first half twice.
    """
    st = ticket["state"]
    d = st.get("decomposition") or {}
    project = os.environ.get("JIRA_PROJECT", "").strip()
    if not d.get("epics") or not project:
        return _fail(ctx, ticket, "materialise",
                     "Nothing to create: no approved decomposition on the ticket "
                     "or JIRA_PROJECT is unset.", "")
    n = sum(len(e["stories"]) for e in d["epics"])
    if not ctx.live:
        return Outcome("tracking", "DRY RUN — would create %d epic(s) and %d "
                       "stories in %s" % (len(d["epics"]), n, project),
                       artifacts={"dry_run": True})

    created = dict(st.get("created") or {})
    try:
        stories.materialise(ctx.jira, project, ticket["key"], d, created,
                            save=lambda c: save_state(ctx, ticket, created=c))
    except JiraError as e:
        return _fail(ctx, ticket, "materialise",
                     "Jira refused to create an issue. What was created so far "
                     "is recorded; fix the cause (usually an issue type or a "
                     "required field) and remove `foreman:failed` to continue.",
                     str(e))
    stories.link_children(ctx.jira, ticket["key"], d, created)
    tracking = stories.build_tracking(d, created)
    save_state(ctx, ticket, state="tracking", created=created, tracking=tracking)
    return Outcome("tracking", "created %d epic(s) and %d stories"
                   % (len(created.get("epics", {})), len(created.get("stories", {}))),
                   comment=render.materialised_comment(
                       ticket["key"], stories.tree_markdown(d, created),
                       ticket["revision"], ctx.run_id),
                   sticky="materialised", artifacts={"created": created})


def _track(ctx: Context, ticket: Dict[str, Any]) -> Outcome:
    """Release blocked stories as their blockers finish; close up when all are Done."""
    st = ticket["state"]
    tracking = dict(st.get("tracking") or {})
    if not tracking:
        return _fail(ctx, ticket, "track", "No stories are recorded on this "
                     "requirement, so there is nothing to track.", "")
    cache: Dict[str, Optional[bool]] = {}

    def done(key):
        if key not in cache:
            cache[key] = ctx.jira.is_done(key)
        return cache[key]

    released = stories.ready_to_activate(tracking, done)
    for key in released:
        if ctx.live:
            ctx.jira.set_labels(key, add=[stories.INTAKE_LABEL],
                                remove=[stories.BLOCKED_LABEL])
        tracking[key]["activated"] = True
    if released and ctx.live:
        save_state(ctx, ticket, tracking=tracking)

    keys = list(tracking)
    waiting = [k for k in keys if not done(k)]
    note = (" · released %s" % ", ".join(released)) if released else ""
    if waiting:
        return Outcome("tracking", "%d of %d stories done; waiting on %s%s"
                       % (len(keys) - len(waiting), len(keys),
                          ", ".join(waiting[:6]), note))

    if ctx.live:
        mirror = (ctx.cfg.get("jira") or {}).get("status_map") or {}
        created = st.get("created") or {}
        for k in list((created.get("epics") or {}).values()) + [created.get("feature")]:
            if k:
                try:
                    ctx.jira.transition_to(k, mirror.get("done", "Done"))
                except JiraError:
                    pass
        save_state(ctx, ticket, state="done")
    return Outcome("done", "all %d stories are done" % len(keys),
                   comment=render.tracking_done_comment(
                       ticket["key"], keys, ticket["revision"], ctx.run_id),
                   sticky="tracking-done")


# ───────────────────────────────────────────────────────────────── mapping ──
def _mapping(ctx: Context, ticket: Dict[str, Any]) -> Outcome:
    """Draft the mapping changes, via the TapiForge MCP or as a reviewable diff.

    Nothing is written to TapiForge here. This stage may only *read* mappings;
    the write tools are not even listed as auto-approved. A person confirms the
    draft at Gate 2 first (see `_mapping_apply`).
    """
    st = ticket["state"]
    via_mcp = mcpcfg.mode() == "mcp"
    work = _workspace(ctx, ticket, "mapping")
    try:
        if via_mcp:
            mcpcfg.install(work, allow_write=False)
        res = _invoke(ctx, "mapping", "mapping", ticket, work, mcp=via_mcp,
                      commit=gitops.head(work), via_mcp=via_mcp,
                      feedback=st.get("feedback", ""),
                      reviewer=st.get("reviewer", ""),
                      candidate=st.get("mapping") if st.get("feedback") else None,
                      requirement=_requirement_text(ctx, st))
    finally:
        _remove_kit(work)
    if not res.ok and res.payload is None:
        return _fail(ctx, ticket, "mapping", "Bob Shell did not return mapping changes.",
                     mcpcfg.scrub("%s — %s" % (res.error_kind, res.error)))

    validated = contract.validate_mapping(res.payload)
    m = validated.data
    if not via_mcp or not m.get("tapiforge_read"):
        # Either there is no TapiForge to read (file mode) or the model says it
        # did not consult it. A mapping proposed from the ticket and repository
        # alone is a guess about a system the reviewer will assume it read.
        m["unverified"] = True

    if not m["changes"]:
        save_state(ctx, ticket, state="needs-info", mapping=m)
        return Outcome("needs-info", "no mapping change could be drafted",
                       comment=render.needs_info_comment(
                           ticket["key"],
                           {"summary": m.get("summary", ""),
                            "clarifications_needed": m["open_questions"]},
                           ticket["revision"], ctx.run_id), sticky="needs-info")

    attached = ""
    if ctx.live:
        attached = _attach_mapping_files(ctx, ticket, m)

    revision = ticket["revision"] + (1 if st.get("feedback") else 0)
    opened = gate_mod.now_iso()
    save_state(ctx, ticket, state="awaiting-mapping-review", mapping=m,
               revision=revision, gate_opened=opened, feedback="", reviewer="",
               mapping_mode="mcp" if via_mcp else "file")
    ticket["revision"] = revision
    return Outcome("awaiting-mapping-review",
                   "%d mapping change(s) drafted for review" % len(m["changes"]),
                   comment=render.mapping_comment(
                       ticket["key"], m, via_mcp, attached, revision, ctx.run_id,
                       ctx.gate_cfg.cmd_approve, ctx.gate_cfg.cmd_changes,
                       ctx.gate_cfg.cmd_reject),
                   sticky="mapping", artifacts={"mapping": m, "gate_opened": opened})


def _requirement_text(ctx: Context, st: Dict[str, Any]) -> str:
    """The parent requirement's summary, so a mapping story is not read cold."""
    key = st.get("requirement")
    if not key:
        return ""
    try:
        issue = ctx.jira.get_issue(key, fields=["summary"])
        return ((issue or {}).get("fields") or {}).get("summary", "")
    except JiraError:
        return ""


def _attach_mapping_files(ctx: Context, ticket: Dict[str, Any], m: Dict[str, Any]) -> str:
    """Option 2 of the TapiForge decision: a diff a person can apply by hand."""
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["action", "target", "source", "rule", "current", "why"])
    for c in m["changes"]:
        w.writerow([c["action"], c["target"], c["source"], c["rule"],
                    c["current"], c["why"]])
    base = ticket["key"] + "-mapping-changes"
    names = []
    try:
        if ctx.jira.add_attachment(ticket["key"], base + ".csv",
                                   buf.getvalue().encode("utf-8"), "text/csv"):
            names.append(base + ".csv")
        if ctx.jira.add_attachment(ticket["key"], base + ".json",
                                   json.dumps(m, indent=2).encode("utf-8"),
                                   "application/json"):
            names.append(base + ".json")
    except JiraError:
        pass
    return ", ".join(names)


def _mapping_apply(ctx: Context, ticket: Dict[str, Any]) -> Outcome:
    """Gate 2 passed. Write the confirmed mappings, or confirm the hand-off."""
    st = ticket["state"]
    m = st.get("mapping") or {}
    changes = m.get("changes") or []
    can_write = (mcpcfg.mode() == "mcp" and bool(mcpcfg._csv("TAPIFORGE_MCP_WRITE_TOOLS"))
                 and st.get("mapping_mode") == "mcp")

    if not can_write:
        save_state(ctx, ticket, state="closing", mapping_applied="by-hand")
        return Outcome("closing",
                       "mappings confirmed; applied by the reviewer (no TapiForge write access)",
                       comment=render.simple_comment(
                           "Mappings confirmed",
                           "The reviewer confirmed the mapping changes. TapiForge write "
                           "access is not configured, so Foreman has **not** changed "
                           "TapiForge itself -- the confirmation is taken to mean the "
                           "reviewer has applied them. Dependent stories are released "
                           "when this ticket is Done.", ticket["key"], "mapping-confirmed",
                           ticket["revision"], ctx.run_id),
                       sticky="mapping-confirmed")

    if not ctx.live:
        return Outcome("closing", "DRY RUN — would apply %d mapping change(s) to TapiForge"
                       % len(changes), artifacts={"dry_run": True})

    done_before = list(st.get("mapping_applied_targets") or [])
    todo = [c for c in changes if c["target"] not in done_before]
    work = _workspace(ctx, ticket, "mapping-apply")
    try:
        mcpcfg.install(work, allow_write=True)
        res = _invoke(ctx, "mapping-apply", "mapping_apply", ticket, work, mcp=True,
                      commit=gitops.head(work), mapping={"changes": todo},
                      already_applied=done_before)
    finally:
        _remove_kit(work)
    if not res.ok and res.payload is None:
        return _fail(ctx, ticket, "mapping-apply",
                     "Bob Shell did not report on applying the mappings.",
                     mcpcfg.scrub("%s — %s" % (res.error_kind, res.error)))
    applied = contract.validate_mapping_applied(res.payload, [c["target"] for c in todo]).data
    all_applied = done_before + applied["applied"]
    save_state(ctx, ticket, mapping_applied_targets=all_applied)
    if applied["failed"]:
        return _fail(ctx, ticket, "mapping-apply",
                     "%d of %d mapping change(s) could not be applied. Those already "
                     "applied are recorded and will not be repeated. Fix the cause and "
                     "comment on the ticket to retry."
                     % (len(applied["failed"]), len(todo)),
                     mcpcfg.scrub(json.dumps(applied["failed"])[:1500]))
    save_state(ctx, ticket, state="closing", mapping_applied="tapiforge")
    return Outcome("closing", "applied %d mapping change(s) to TapiForge" % len(all_applied),
                   comment=render.simple_comment(
                       "Mappings applied to TapiForge",
                       "%s\n\n%s" % (applied["summary"] or "All confirmed changes were written.",
                                      "\n".join("- `%s`" % t for t in all_applied[:40])),
                       ticket["key"], "mapping-applied", ticket["revision"], ctx.run_id),
                   sticky="mapping-applied", artifacts={"applied": all_applied})


# ──────────────────────────────────────────────────────────────────── plan ──
def _plan(ctx: Context, ticket: Dict[str, Any]) -> Outcome:
    st = ticket["state"]
    analysis = st.get("analysis") or {}
    work = _workspace(ctx, ticket)
    commit = st.get("commit") or gitops.head(work)

    budget_note = {
        "max_files": ctx.guardrails.max_files,
        "max_lines_total": ctx.guardrails.max_lines,
        "max_lines_per_file": ctx.guardrails.max_file_lines,
        "max_deleted_lines": ctx.guardrails.max_deleted_lines,
        "paths_you_may_change": ctx.guardrails.allow,
        "paths_you_may_never_change": ctx.guardrails.deny[:12],
        "existing_tests": "may not be modified or deleted",
    }

    res = _invoke(ctx, "plan", "plan", ticket, work, commit=commit,
                  analysis=analysis, guardrails=budget_note,
                  feedback=st.get("feedback", ""),
                  reviewer=st.get("reviewer", ""))
    if not res.ok and res.payload is None:
        return _fail(ctx, ticket, "plan", "Bob Shell did not return a plan.",
                     "%s — %s" % (res.error_kind, res.error))

    validated = contract.validate_plan(res.payload)
    plan = validated.data

    # Adversarial pass. Its brief is to kill the plan, not to confirm it — a
    # reviewer that agrees with the planner adds a step and no information.
    review = {}
    if ctx.modes.get("review"):
        rres = _invoke(ctx, "review", "review", ticket, work, commit=commit,
                       candidate={"analysis": analysis, "plan": plan},
                       analysis=analysis)
        if rres.payload:
            try:
                review = contract.validate_review(rres.payload).data
            except contract.Rejection as e:
                review = {"verdict": "unavailable", "reasons": [str(e)]}

    # A rejected plan does NOT stop the ticket. The reviewer's objection is put
    # in front of the human alongside the plan, and the human decides. An
    # automated veto that a person never sees is a way for the pipeline to
    # quietly refuse work nobody agreed to refuse.
    revision = ticket["revision"] + (1 if st.get("feedback") else 0)
    opened = gate_mod.now_iso()
    comment = render.analysis_comment(
        ticket["key"], analysis, plan, review,
        ctx.github_repo or ctx.repo_source, commit, revision, ctx.run_id,
        ctx.gate_cfg.cmd_approve, ctx.gate_cfg.cmd_changes,
        ctx.gate_cfg.cmd_reject, validated.rejections)

    save_state(ctx, ticket, state="awaiting-approval", plan=plan,
               review=review, revision=revision, gate_opened=opened,
               commit=commit, feedback="", reviewer="")
    ticket["revision"] = revision
    return Outcome("awaiting-approval",
                   "plan posted for approval (%d file(s); reviewer said %s)"
                   % (len(plan["files_to_change"]),
                      review.get("verdict", "nothing")),
                   comment=comment, sticky="analysis",
                   artifacts={"plan": plan, "review": review,
                              "gate_opened": opened})


# ──────────────────────────────────────────────────────────────────── gate ──
def _gate(ctx: Context, ticket: Dict[str, Any]) -> Outcome:
    st = ticket["state"]
    current = ticket["fsm"]
    spec = machine.GATES[current]
    opened = gate_mod.parse_ts(st.get("gate_opened"))
    result = gate_mod.evaluate(ticket["comments"], ctx.gate_cfg, opened)

    if not ctx.gate_cfg.configured:
        # An empty allowlist must mean "nobody can approve", never "everybody
        # can". A misconfigured approver list is the one failure here that
        # should stop the pipeline rather than open it.
        return Outcome(current,
                       "FOREMAN_APPROVERS is empty — no account can approve, "
                       "so this gate can never pass. Fix .env.", ok=False)

    fresh, reported = _unreported(st, result.attempts)
    note = render.rejected_attempts_note([a.as_dict() for a in fresh])

    if result.decision is None:
        if fresh:
            save_state(ctx, ticket, reported_attempts=reported)
        if result.timed_out:
            return Outcome("stalled",
                           "waiting %.0fh at %s, past the %dh timeout"
                           % (result.waited_hours, current,
                              ctx.gate_cfg.timeout_hours),
                           comment=note, artifacts=result.as_dict())
        return Outcome(current,
                       "waiting for a decision (%.1fh so far)" % result.waited_hours,
                       comment=note, artifacts=result.as_dict())

    d = result.decision
    target = spec[{"approve": "approve", "changes": "changes",
                   "reject": "reject"}[d.verdict]]
    machine.check(current, target)

    updates: Dict[str, Any] = {"state": target,
                               "decision": d.as_dict(),
                               "gate_opened": None,
                               "reported_attempts": reported}
    if d.verdict == "changes":
        max_rev = int(os.environ.get("FOREMAN_MAX_REVISIONS", "3") or 3)
        if ticket["revision"] >= max_rev:
            save_state(ctx, ticket, state="failed",
                       decision=d.as_dict())
            return Outcome("failed",
                           "revision limit reached (%d)" % max_rev, ok=False,
                           comment=render.failure_comment(
                               ticket["key"], current,
                               "This ticket has been round the revise loop %d "
                               "times without converging. Foreman is stopping "
                               "rather than burning more budget on a request "
                               "it is evidently not understanding." % max_rev,
                               "Last feedback: %s" % d.feedback,
                               ticket["revision"], ctx.run_id),
                           sticky="failed")
        updates["feedback"] = d.feedback
        updates["reviewer"] = d.author

    save_state(ctx, ticket, **updates)
    return Outcome(target, "%s by %s" % (d.verdict, d.author),
                   comment=note, artifacts=result.as_dict())


MAX_REPORTED_ATTEMPTS = 50


def _unreported(st: Dict[str, Any], attempts) -> Tuple[list, List[str]]:
    """Split gate attempts into ones not yet surfaced, and the updated ledger.

    The gate is re-read on every run, so without a ledger the same refused
    attempt is reported again every ten minutes for as long as the ticket
    waits — and the commonest one is the reviewer's own `/foreman changes`,
    which turns "stale" the moment the revised plan reopens the gate. The
    decision Foreman already honoured is not an attempt, so it starts in the
    ledger rather than being reported as refused.
    """
    seen = [str(i) for i in (st.get("reported_attempts") or [])]
    honoured = str((st.get("decision") or {}).get("comment_id") or "")
    if honoured and honoured not in seen:
        seen.append(honoured)
    fresh = [a for a in attempts if not a.comment_id or a.comment_id not in seen]
    seen += [a.comment_id for a in fresh if a.comment_id]
    return fresh, seen[-MAX_REPORTED_ATTEMPTS:]


def _human_hold(ctx: Context, ticket: Dict[str, Any]) -> Outcome:
    """needs-info / failed / stalled: wait, but notice when a human acts.

    A ticket edited or commented on after Foreman parked it is a ticket a human
    has responded to. It goes back to intake and is analysed afresh — not
    resumed mid-flight, because the premises Foreman stopped on may no longer
    hold.
    """
    st = ticket["state"]
    parked = gate_mod.parse_ts(st.get("updated"))
    latest = None
    for c in ticket["comments"]:
        if (c.get("author") or {}).get("accountId") == ctx.bot_account_id:
            continue
        ts = gate_mod.parse_ts(c.get("created"))
        if ts and (latest is None or ts > latest):
            latest = ts
    edited = gate_mod.parse_ts(ticket.get("updated"))

    # Foreman's own comment and label writes land seconds after it parks the
    # ticket and bump Jira's `updated`. Without a grace period every parked
    # ticket looks "edited by a human" on the very next run and bounces
    # straight back to intake.
    grace = int(os.environ.get("FOREMAN_HOLD_GRACE_SEC", "120") or 120)
    from datetime import timedelta
    moved = (latest and parked and latest > parked) or \
            (edited and parked and edited > parked + timedelta(seconds=grace))
    if not moved:
        return Outcome(ticket["fsm"], "parked, waiting for a human")

    resume = st.get("failed_from") if ticket["fsm"] == "failed" else None
    if resume in machine.RESUMABLE:
        # The stage that failed only carries out something already decided
        # (a merge, a deploy, creating the approved issues). Running it again
        # is what the human who fixed the cause is asking for; re-analysing
        # from intake would throw away an approved plan.
        machine.check("failed", resume)
        save_state(ctx, ticket, state=resume, failed_from=None, failure=None)
        return Outcome(resume, "a human responded; retrying the %s stage" % resume)

    machine.check(ticket["fsm"], machine.INTAKE)
    save_state(ctx, ticket, state=machine.INTAKE, feedback="", analysis=None,
               plan=None, revision=ticket["revision"], failed_from=None)
    return Outcome(machine.INTAKE,
                   "a human responded; re-analysing from the top")


# ─────────────────────────────────────────────────────────────────── build ──
def _build(ctx: Context, ticket: Dict[str, Any]) -> Outcome:
    st = ticket["state"]
    plan = st.get("plan") or {}
    branch = gitops.branch_name(ctx.branch_prefix, ticket["key"])
    work = os.path.join(ctx.paths["workdir"], ticket["key"] + "-build")

    gitops.clone(ctx.repo_source, work, ctx.base_branch,
                 os.environ.get("GITHUB_READ_TOKEN", ""))
    _install_kit(ctx, work)
    gitops.checkout_branch(work, branch)
    base_sha = gitops.head(work)

    res = _invoke(ctx, "build", "build", ticket, work, write=True,
                  commit=base_sha, plan=plan,
                  feedback=st.get("feedback", ""), reviewer=st.get("reviewer", ""),
                  test_command=ctx.build_test_command)

    # The diff is collected whether or not the model reported success. A stage
    # that blew its budget mid-edit still left files on disk, and those files
    # are exactly what must not reach a branch unexamined.
    _remove_kit(work)
    diff = guard_mod.collect_diff(work, base_sha)
    planned = [f["path"] for f in plan.get("files_to_change", [])]
    diff = guard_mod.inspect(diff, ctx.guardrails, planned)

    if not res.ok and res.payload is None:
        guard_mod.revert(work)
        return _fail(ctx, ticket, "build",
                     "Bob Shell did not complete the implementation.",
                     "%s — %s" % (res.error_kind, res.error))

    if diff.blocked:
        guard_mod.revert(work)
        blocking = [v.as_dict() for v in diff.violations if v.blocking]
        return _fail(ctx, ticket, "build",
                     "The change violated the guardrails and was reverted in "
                     "full. Nothing was committed or pushed.",
                     diff.summary(), violations=blocking)

    impl = contract.validate_implementation(res.payload).data

    # Run the suite before publishing anything. A branch that does not build is
    # a branch that costs someone an hour to discover.
    test_result = None
    if ctx.build_test_command:
        test_result = gitops.run_command(work, ctx.build_test_command,
                                         timeout=ctx.timeout_for("test"))
        if not test_result["passed"]:
            guard_mod.revert(work)
            return _fail(ctx, ticket, "build",
                         "The change was written but the existing test suite "
                         "fails against it. It was reverted, not published.",
                         "`%s` exited %s\n\n%s"
                         % (ctx.build_test_command, test_result["exit_code"],
                            test_result["stdout"][-2000:]))

    if not ctx.live:
        return Outcome("committed",
                       "DRY RUN — %s; nothing committed or pushed" % diff.summary(),
                       artifacts={"diff": diff.as_dict(), "branch": branch,
                                  "implementation": impl,
                                  "test": test_result, "dry_run": True})

    sha = gitops.commit(
        work, impl["commit_subject"], impl["commit_body"],
        trailers=["", "Ticket: %s" % ticket["key"],
                  "Approved-by: %s" % (st.get("decision") or {}).get("author", "?"),
                  "Generated-by: AI-SDLC Foreman (run %s)" % ctx.run_id])
    if not sha:
        return _fail(ctx, ticket, "build",
                     "Nothing to commit — the build stage changed no files.",
                     diff.summary())

    pushed, detail = gitops.push(work, ctx.repo_source, branch,
                                 os.environ.get("GITHUB_WRITE_TOKEN", ""))
    if not pushed:
        return _fail(ctx, ticket, "build", "The branch could not be pushed.",
                     detail)

    pr_url = _open_pr(ctx, ticket, branch, plan, impl, diff)

    save_state(ctx, ticket, state="committed", branch=branch, commit_sha=sha,
               pr_url=pr_url, diff=diff.as_dict(), implementation=impl,
               build_test=test_result, feedback="", reviewer="")
    ctx.store.set_ticket(ticket["key"], branch=branch, pr_url=pr_url or "")

    return Outcome("committed",
                   "committed %s and pushed `%s`%s"
                   % (sha[:12], branch, (" · PR %s" % pr_url) if pr_url else ""),
                   comment=render.build_comment(
                       ticket["key"], impl, diff.as_dict(), pr_url, branch, sha,
                       test_result, None, ticket["revision"], ctx.run_id),
                   sticky="build",
                   artifacts={"branch": branch, "sha": sha, "pr_url": pr_url,
                              "diff": diff.as_dict(), "test": test_result})


def _open_pr(ctx: Context, ticket, branch, plan, impl, diff) -> str:
    if not (ctx.gh and ctx.github_repo):
        return ""
    body = "\n".join([
        "Closes %s." % ticket["key"],
        "",
        impl.get("summary", ""),
        "",
        "## Approved plan",
        plan.get("approach", ""),
        "",
        "## Acceptance criteria",
        "\n".join("- [ ] %s" % c for c in plan.get("acceptance_criteria", [])),
        "",
        "## Diff",
        "%d file(s), +%d / -%d" % (len(diff.files), diff.added, diff.removed),
        "",
        "---",
        "_Opened by AI-SDLC Foreman, run `%s`, after human approval on %s._"
        % (ctx.run_id, ticket["key"]),
    ])
    try:
        existing = ctx.gh.find_pr(ctx.github_repo, branch)
        if existing:
            ctx.gh.update_pr(ctx.github_repo, existing["number"],
                             "%s %s" % (ticket["key"], ticket["summary"][:180]),
                             body)
            return existing.get("html_url", "")
        pr = ctx.gh.create_pr(
            ctx.github_repo, "%s %s" % (ticket["key"], ticket["summary"][:180]),
            branch, ctx.base_branch, body)
        labels = [l for l in
                  (os.environ.get("FOREMAN_PR_LABELS", "") or "").split(",")
                  if l.strip()]
        if labels:
            ctx.gh.add_labels(ctx.github_repo, pr["number"], labels)
        return pr.get("html_url", "")
    except Exception:
        # A PR that fails to open is not a reason to lose a pushed branch. The
        # ticket says what happened and a human can open it in ten seconds.
        return ""


# ─────────────────────────────────────────────────────────────── code review ──
def _codereview(ctx: Context, ticket: Dict[str, Any]) -> Outcome:
    """The AI reviews its own pushed diff, then the human sign-off gate opens.

    Advisory by design: the review is shown to the person at the gate beside the
    PR and the build-time test result, and they decide. A reviewer that could
    veto would be a way for the pipeline to quietly refuse work.
    """
    st = ticket["state"]
    branch = st.get("branch") or gitops.branch_name(ctx.branch_prefix, ticket["key"])
    work = os.path.join(ctx.paths["workdir"], ticket["key"] + "-review")
    # depth=2: the build stage publishes exactly one commit on top of the base,
    # so HEAD~1..HEAD is precisely the change under review.
    gitops.clone(ctx.repo_source, work, branch,
                 os.environ.get("GITHUB_READ_TOKEN", ""), depth=2)
    _install_kit(ctx, work)
    rc, patch, _ = gitops.run(work, "diff", "HEAD~1", "HEAD")
    commit = gitops.head(work)
    review: Dict[str, Any] = {}
    if rc == 0 and patch.strip():
        res = _invoke(ctx, "codereview", "codereview", ticket, work, commit=commit,
                      plan=st.get("plan") or {}, patch=patch[:60000])
        _remove_kit(work)
        if res.payload:
            try:
                review = contract.validate_code_review(res.payload).data
            except contract.Rejection as e:
                review = {"verdict": "unavailable", "summary": str(e),
                          "findings": [], "checked": []}
    if not review:
        review = {"verdict": "unavailable", "findings": [], "checked": [],
                  "summary": "The AI review did not produce a usable result. "
                             "Read the pull request yourself before signing off."}

    opened = gate_mod.now_iso()
    save_state(ctx, ticket, state="awaiting-signoff", code_review=review,
               gate_opened=opened)
    return Outcome("awaiting-signoff",
                   "code review %s; sign-off requested" % review["verdict"],
                   comment=render.codereview_comment(
                       ticket["key"], review, st.get("implementation") or {},
                       st.get("diff") or {}, st.get("pr_url", ""), branch,
                       st.get("build_test"), ctx.base_branch, ticket["revision"],
                       ctx.run_id, ctx.gate_cfg.cmd_approve, ctx.gate_cfg.cmd_changes,
                       ctx.gate_cfg.cmd_reject),
                   sticky="codereview",
                   artifacts={"code_review": review, "gate_opened": opened})


# ───────────────────────────────────────────────────────────────────── merge ──
def _utc_z(offset_sec: int = 0) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + offset_sec))


def _pr_number(pr_url: str) -> int:
    try:
        return int((pr_url or "").rstrip("/").rsplit("/", 1)[-1])
    except ValueError:
        return 0


def _merge(ctx: Context, ticket: Dict[str, Any]) -> Outcome:
    """Gate 3 passed: merge the PR into the DEV branch and close the code-review story.

    `FOREMAN_BASE_BRANCH` is that DEV branch (Release_DEV); the PR was opened
    against it. With FOREMAN_AUTO_MERGE=false Foreman does not press the button:
    it waits for a person to merge and carries on once the PR shows as merged.
    """
    st = ticket["state"]
    pr_url = st.get("pr_url", "")
    number = _pr_number(pr_url)
    github_mode = os.environ.get("FOREMAN_GIT_MODE", "local") == "github"

    if not ctx.live:
        return Outcome("deploying", "DRY RUN — would merge %s into %s"
                       % (pr_url or "the branch", ctx.base_branch),
                       artifacts={"dry_run": True})

    merge_sha, note = "", ""
    if github_mode:
        if not (ctx.gh and ctx.github_repo and number):
            return _fail(ctx, ticket, "merge", "There is no pull request to merge.",
                         "pr_url=%r. The build stage could not open one; open it "
                         "by hand, record it on the ticket, and retry." % pr_url)
        pr = ctx.gh.get_pr(ctx.github_repo, number) or {}
        if pr.get("merged"):
            merge_sha = pr.get("merge_commit_sha") or ""
            note = "already merged"
        elif not _flag("FOREMAN_AUTO_MERGE"):
            return Outcome("merging", "waiting for a person to merge %s into %s "
                           "(FOREMAN_AUTO_MERGE is off)" % (pr_url, ctx.base_branch))
        else:
            ok, detail = ctx.gh.merge_pr(
                ctx.github_repo, number, os.environ.get("FOREMAN_MERGE_METHOD", "squash"),
                "%s %s" % (ticket["key"], ticket["summary"][:150]))
            if not ok:
                return _fail(ctx, ticket, "merge",
                             "GitHub refused the merge (branch protection, a "
                             "conflict, or a required check). Resolve it and "
                             "remove `foreman:failed` to retry.", detail)
            merge_sha = detail
    else:
        note = "local sandbox: no pull request to merge"

    merged_at = _utc_z(-int(os.environ.get("FOREMAN_OKD_CLOCK_SKEW_SEC", "30") or 30))
    closed = _close_sibling(ctx, ticket, "codereview")
    save_state(ctx, ticket, state="deploying", merged=bool(merge_sha),
               merge_sha=merge_sha, merged_at=merged_at)
    return Outcome("deploying", "merged into %s%s%s"
                   % (ctx.base_branch, (" · " + merge_sha[:12]) if merge_sha else "",
                      (" · " + note) if note else ""),
                   comment=render.simple_comment(
                       "Merged to %s" % ctx.base_branch,
                       "Signed off by **%s**. %s\n\nThe pipeline now deploys to DEV; "
                       "Foreman will wait for the pods to roll over.%s"
                       % ((st.get("decision") or {}).get("author", "?"),
                          ("Merge commit `%s`." % merge_sha[:12]) if merge_sha else note,
                          ("\n\nCode-review story %s closed." % closed) if closed else ""),
                       ticket["key"], "merged", ticket["revision"], ctx.run_id),
                   sticky="merged", artifacts={"merge_sha": merge_sha})


def _close_sibling(ctx: Context, ticket: Dict[str, Any], story_type: str) -> str:
    """Close a sibling story (Foreman owns Code Review's closure). Best effort."""
    key = ((ticket["state"].get("siblings") or {}).get(story_type) or "")
    if not key or not ctx.live:
        return ""
    mirror = (ctx.cfg.get("jira") or {}).get("status_map") or {}
    try:
        if ctx.jira.is_done(key):
            return ""
        ctx.jira.add_comment(key, "Closed by AI-SDLC Foreman: the change was "
                             "signed off on %s and merged." % ticket["key"])
        return key if ctx.jira.transition_to(key, mirror.get("done", "Done")) else ""
    except JiraError:
        return ""


# ────────────────────────────────────────────────────────────────── deploy ──
def _deploy(ctx: Context, ticket: Dict[str, Any]) -> Outcome:
    """Hand the merged DEV branch to your pipeline and wait for the rollout."""
    st = ticket["state"]
    sha = st.get("merge_sha") or st.get("commit_sha") or ""

    if not ctx.live:
        return Outcome("deployed", "DRY RUN — would wait for the %s deploy"
                       % ctx.deploy_cfg.get("mode"), artifacts={"dry_run": True})

    cfg = dict(ctx.deploy_cfg, since=st.get("merged_at") or "")
    result = deploy_mod.trigger(cfg, ticket["key"], ctx.base_branch, sha,
                                repo=ctx.github_repo, gh=ctx.gh,
                                workdir=ctx.paths["workdir"])
    save_state(ctx, ticket, deploy=result.as_dict())
    if not result.ok:
        return _fail(ctx, ticket, "deploy",
                     "The DEV deployment did not complete (%s). Once the pipeline "
                     "or the pods are fixed, remove `foreman:failed` and Foreman "
                     "checks again without redoing the merge." % result.status,
                     result.detail)

    if result.url:
        try:
            ctx.jira.add_remote_link(ticket["key"], result.url, "DEV deployment",
                                     "AI-SDLC Foreman · %s" % ctx.run_id)
        except JiraError:
            pass
    save_state(ctx, ticket, state="deployed")
    return Outcome("deployed", "DEV deploy %s%s" % (result.status,
                                                    (" · " + result.url) if result.url else ""),
                   comment=render.simple_comment(
                       "Deployed to DEV", result.detail or "The rollout finished.",
                       ticket["key"], "deployed", ticket["revision"], ctx.run_id),
                   sticky="deployed", artifacts={"deploy": result.as_dict()})


# ───────────────────────────────────────────────────────────────── dev tests ──
def _devtest(ctx: Context, ticket: Dict[str, Any]) -> Outcome:
    """Run the test suite (bob-ace-testing-kit) against the deployed DEV build.

    Run by the harness, never by the model: the exit code of a process the model
    does not control is the only result recorded.
    """
    st = ticket["state"]
    command = ctx.test_command
    if not command:
        return _fail(ctx, ticket, "devtest",
                     "FOREMAN_TEST_COMMAND is not set, so nothing could be run "
                     "against DEV.", "Set FOREMAN_TEST_COMMAND in .env "
                     "(e.g. the bob-ace-testing-kit command).")
    if not ctx.live:
        return Outcome("closing", "DRY RUN — would run `%s` against DEV" % command,
                       artifacts={"dry_run": True})

    work = os.path.join(ctx.paths["workdir"], ticket["key"] + "-devtest")
    # The merged DEV branch, not the feature branch: test what is deployed.
    gitops.clone(ctx.repo_source, work, ctx.base_branch,
                 os.environ.get("GITHUB_READ_TOKEN", ""))
    result = gitops.run_command(work, command, timeout=ctx.timeout_for("test"))
    save_state(ctx, ticket, dev_test=result)
    if not result["passed"]:
        return _fail(ctx, ticket, "devtest",
                     "The test suite failed against DEV. Foreman does not retry a "
                     "failed run on its own: fix the cause (code or environment), "
                     "then remove `foreman:failed` to run the suite again.",
                     "`%s` exited %s\n\n%s" % (command, result["exit_code"],
                                                result["stdout"][-2500:]))
    save_state(ctx, ticket, state="closing")
    return Outcome("closing", "DEV tests passed (%ss)" % result["duration_sec"],
                   comment=render.devtest_comment(ticket["key"], result,
                                                  (st.get("plan") or {}).get("acceptance_criteria", []),
                                                  st.get("pr_url", ""), ticket["revision"],
                                                  ctx.run_id),
                   sticky="devtest", artifacts={"test": result})


# ─────────────────────────────────────────────────────────────────── close ──
def _close(ctx: Context, ticket: Dict[str, Any]) -> Outcome:
    """Close the Jira ticket. Production stays with people."""
    st = ticket["state"]
    decision = st.get("decision") or {}
    pr_url = st.get("pr_url", "")
    fix_version = os.environ.get("FOREMAN_PROD_FIXVERSION", "").strip()
    prod_ticket = ""

    if not ctx.live:
        return Outcome("done", "DRY RUN — would close the ticket",
                       artifacts={"dry_run": True})

    # A story that belongs to a requirement already has a Production
    # Deployment story of its own; raising another would double it. A mapping
    # story changes TapiForge, not deployable code, so it has nothing to
    # schedule either. Only a plain code ticket gets the base-product prod ticket.
    is_mapping = st.get("kind") == "mapping" or bool(st.get("mapping_applied"))
    if not st.get("requirement") and not is_mapping and _flag("FOREMAN_PROD_SCHEDULE", True):
        if fix_version:
            try:
                ctx.jira.set_fix_version(ticket["key"], fix_version)
            except JiraError:
                pass
        prod_ticket = _raise_prod_ticket(ctx, ticket, pr_url, fix_version)

    mirror = (ctx.cfg.get("jira") or {}).get("status_map") or {}
    target = mirror.get("done", "Done")
    moved = None
    try:
        moved = ctx.jira.transition_to(ticket["key"], target)
    except JiraError:
        pass
    warn = ""
    if not moved and ctx.jira.is_done(ticket["key"]) is not True:
        warn = ("\n\n:::warning\nForeman could not move this ticket to **%s** (no "
                "such transition from its current status). Close it by hand -- the "
                "requirement is waiting for it to be Done.\n:::" % target)

    save_state(ctx, ticket, state="done", prod_ticket=prod_ticket,
               fix_version=fix_version)
    if is_mapping:
        how = ("written to TapiForge by Foreman" if st.get("mapping_applied") == "tapiforge"
               else "applied in TapiForge by the reviewer")
        body = render.simple_comment(
            "Mapping done",
            "The mapping changes were confirmed by **%s** and %s. The stories that "
            "were waiting on this one are released." % (decision.get("author", "?"), how)
            + warn, ticket["key"], "closed", ticket["revision"], ctx.run_id)
        return Outcome("done", "closed (mapping)", comment=body, sticky="closed",
                       artifacts={"mapping_applied": st.get("mapping_applied")})
    body = render.closing_comment(
        ticket["key"], pr_url, bool(st.get("merged")), fix_version, prod_ticket,
        decision.get("author", "?"), ticket["revision"], ctx.run_id)
    return Outcome("done", "closed%s" % ((" · prod ticket " + prod_ticket) if prod_ticket else ""),
                   comment=body.replace("\n\n---\n", warn + "\n\n---\n", 1) if warn else body,
                   sticky="closed",
                   artifacts={"merged": st.get("merged"), "prod_ticket": prod_ticket})


def _raise_prod_ticket(ctx: Context, ticket, pr_url, fix_version) -> str:
    """Raise the production-deployment ticket a person will schedule.

    This is where the automation deliberately ends. Everything up to here is
    reversible in minutes; a production release is not, and no comment on a
    Jira ticket is informed consent for one on a team's behalf.
    """
    project = os.environ.get("JIRA_PROJECT", "").strip()
    if not project:
        return ""
    body = "\n".join([
        "Production deployment for %s." % ticket["key"],
        "",
        "> %s" % ticket["summary"],
        "",
        "The change has been implemented, reviewed, deployed to non-prod and "
        "signed off. It is **not** deployed to production; that is this "
        "ticket.",
        "",
        "| | |", "|---|---|",
        "| Source ticket | %s |" % ticket["key"],
        "| Pull request | %s |" % (pr_url or "—"),
        "| Fix version | %s |" % (fix_version or "—"),
        "| Branch | `%s` |" % (ticket["state"].get("branch") or "—"),
        "",
        "AI-SDLC Foreman raised this and will not act on it.",
    ])
    try:
        created = ctx.jira.create_issue(
            project, "Deploy to production: %s" % ticket["summary"][:180],
            body, issue_type=os.environ.get("FOREMAN_PROD_ISSUETYPE", "Task"),
            # Deliberately NOT the intake label. A production-deployment
            # ticket carrying `foreman` would be matched by the intake query
            # on the very next run, and Foreman would start triaging its own
            # output — a loop that looks like productivity.
            labels=["foreman-raised", "prod-deployment", "scheduled"])
        key = created.get("key", "")
        if key:
            ctx.jira.add_remote_link(
                ticket["key"], "https://%s/browse/%s" % (ctx.jira.site, key),
                "Production deployment: %s" % key)
        return key
    except JiraError:
        return ""


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")
