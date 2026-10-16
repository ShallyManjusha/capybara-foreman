"""foreman CLI. Run `python3 -m foreman --help`."""
import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from . import __version__, gate as gate_mod, guardrails as guard_mod
from . import machine, render, stages, yamlish
from .adapter import BobAdapter, api_key
from .env import csv, flag, load_env, num, redact, require
from .github import GitHubClient, GitHubError
from .jira import JiraClient, JiraError
from .store import Store


# ──────────────────────────────────────────────────────────────── plumbing ──
def load_cfg(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    return yamlish.load(path) or {}


def jira_from_env(readonly: bool = False) -> JiraClient:
    # JIRA_EMAIL is deliberately not required: on Data Center the credential
    # is a personal access token used as a bearer, with no email at all.
    # Requiring it would force operators to invent a value that is then sent
    # in a Basic header the server ignores.
    err = require("JIRA_SITE", "JIRA_API_TOKEN")
    if err:
        raise SystemExit("FATAL: " + err)
    return JiraClient(os.environ["JIRA_SITE"],
                      os.environ.get("JIRA_EMAIL", ""),
                      os.environ["JIRA_API_TOKEN"], readonly=readonly,
                      deployment=os.environ.get("JIRA_DEPLOYMENT", "auto"))


def github_from_env() -> Optional[GitHubClient]:
    token = os.environ.get("GITHUB_WRITE_TOKEN", "").strip()
    if not token or os.environ.get("FOREMAN_GIT_MODE", "local") != "github":
        return None
    return GitHubClient(os.environ.get("GITHUB_API", "https://api.github.com"),
                        token)


def paths_from_env(root: str) -> Dict[str, str]:
    def p(name, default):
        raw = os.environ.get(name, default)
        return raw if os.path.isabs(raw) else os.path.normpath(
            os.path.join(root, raw.lstrip("./")))
    out = {
        "workdir": p("FOREMAN_WORKDIR", "./var/work"),
        "logdir": p("FOREMAN_LOGDIR", "./var/logs"),
        "outdir": p("FOREMAN_OUTDIR", "./var/out"),
        "reportdir": p("FOREMAN_REPORTDIR", "./reports"),
        "auditdir": p("FOREMAN_AUDITDIR", "./audit"),
        "db": p("FOREMAN_DB", "./var/foreman.db"),
        "install_kit": os.path.join(root, "extension", "install.sh"),
        "guardrails": os.path.join(root, "policy", "guardrails.yaml"),
        "root": root,
    }
    for k in ("workdir", "logdir", "outdir", "reportdir", "auditdir"):
        os.makedirs(out[k], exist_ok=True)
    return out


COMMENT_MODES = ("append", "update")


def comment_mode() -> Tuple[str, bool]:
    """How a stage's comment reaches the ticket, and whether the setting was valid.

    `append` posts every revision as a new comment, so the ticket keeps the
    full history of what Foreman proposed and what reviewers approved against.
    `update` rewrites Foreman's previous comment for that stage in place: a
    quieter ticket, but an earlier plan is gone once a revision replaces it.
    An unrecognised value falls back to `append` — the mode that cannot lose
    anything — and `doctor` reports it.
    """
    raw = (os.environ.get("FOREMAN_COMMENT_MODE") or "append").strip().lower()
    return (raw, True) if raw in COMMENT_MODES else ("append", False)


def repo_source(root: str = "") -> str:
    """Where the code lives: a local sandbox, or an https remote.

    A relative local path is resolved against the project root, not the
    process's working directory — every entry point runs from `harness/`, and
    a path that resolves differently per caller is a path that silently points
    somewhere empty.
    """
    if os.environ.get("FOREMAN_GIT_MODE", "local") == "github":
        repo = os.environ.get("GITHUB_REPO", "").strip()
        host = os.environ.get("GITHUB_HOST", "github.com").strip()
        return "https://%s/%s.git" % (host, repo) if repo else ""
    local = os.environ.get("FOREMAN_LOCAL_REPO", "./var/sandbox/service")
    if root and not os.path.isabs(local):
        return os.path.normpath(os.path.join(root, local.lstrip("./")))
    return local


def build_context(args, live: bool) -> stages.Context:
    root = os.path.abspath(args.root)
    cfg = load_cfg(args.config)
    paths = paths_from_env(root)
    store = Store(paths["db"])

    src = repo_source(root)

    jira = jira_from_env()
    me = jira.myself()

    gcfg = {}
    if os.path.exists(paths["guardrails"]):
        gcfg = yamlish.load(paths["guardrails"]) or {}

    gate_cfg = gate_mod.GateConfig(
        approvers=csv("FOREMAN_APPROVERS"),
        cmd_approve=os.environ.get("FOREMAN_CMD_APPROVE", "/foreman approve"),
        cmd_reject=os.environ.get("FOREMAN_CMD_REJECT", "/foreman reject"),
        cmd_changes=os.environ.get("FOREMAN_CMD_CHANGES", "/foreman changes"),
        bot_account_id=me.get("accountId", ""),
        timeout_hours=num("FOREMAN_APPROVAL_TIMEOUT_HOURS", 72))

    deploy_cfg = {
        "mode": os.environ.get("FOREMAN_DEPLOY_MODE", "none"),
        "workflow": os.environ.get("FOREMAN_DEPLOY_WORKFLOW", ""),
        "ref": os.environ.get("FOREMAN_DEPLOY_REF", ""),
        "env": os.environ.get("FOREMAN_DEPLOY_ENV", "nonprod"),
        "webhook": os.environ.get("FOREMAN_DEPLOY_WEBHOOK", ""),
        "timeout_sec": num("FOREMAN_DEPLOY_TIMEOUT_SEC", 1800),
        "script": os.path.join(root, "policy", "deploy-simulate.sh"),
        "okd_namespace": os.environ.get("FOREMAN_OKD_NAMESPACE", ""),
        "okd_label_selector": os.environ.get("FOREMAN_OKD_LABEL_SELECTOR", ""),
    }

    return stages.Context(
        cfg=cfg, jira=jira, store=store, run_id=args.run_id, live=live,
        paths=paths, gate_cfg=gate_cfg, bot_account_id=me.get("accountId", ""),
        gh=github_from_env(), repo_source=src,
        github_repo=os.environ.get("GITHUB_REPO", ""),
        base_branch=os.environ.get("FOREMAN_BASE_BRANCH", "main"),
        branch_prefix=os.environ.get("FOREMAN_BRANCH_PREFIX", "foreman/"),
        test_command=os.environ.get("FOREMAN_TEST_COMMAND", ""),
        # Unset -> same command as the DEV run. Set (even to "") -> used as is.
        build_test_command=os.environ.get("FOREMAN_BUILD_TEST_COMMAND"),
        deploy_cfg=deploy_cfg, guardrails=guard_mod.Guardrails(gcfg),
        log_dir=paths["logdir"])


# ────────────────────────────────────────────────────────────────── doctor ──
def cmd_doctor(args):
    cfg = load_cfg(args.config)
    print("AI-SDLC Foreman %s\n" % __version__)
    problems: List[str] = []

    ad = BobAdapter(cfg)
    caps = ad.capabilities()
    print("── Bob Shell")
    print("   binary        : %s" % ad.binary)
    print("   version       : %s" % str(caps.get("_version", "not found")).splitlines()[0])
    print("   CLI dialect   : %s" % caps.get("_dialect", "?"))
    print("   api key       : %s" % redact(api_key()))
    print("   team id       : %s" % (ad.team_id or "<unset>"))
    print("   model         : %s" % (ad.model or "<unpinned — pin before measuring>"))
    for p in ad.preflight():
        problems.append("bob: " + p)

    print("\n── Jira")
    try:
        jira = jira_from_env()
        me = jira.myself()
        print("   site          : %s" % jira.base)
        print("   deployment    : %s (REST v%s, %s bodies)"
              % (jira.deployment, jira.api_version,
                 "wiki markup" if jira.api_version == "2" else "ADF"))
        print("   auth          : %s" % ("API token (Basic)" if jira.email
                                         else "personal access token (Bearer)"))
        print("   authenticated : %s <%s>" % (me.get("displayName"),
                                              me.get("emailAddress")
                                              or me.get("name") or "—"))
        print("   account id    : %s" % me.get("accountId"))
        project = os.environ.get("JIRA_PROJECT", "")
        proj = jira.get_project(project) if project else None
        print("   project       : %s" % (("%s — %s" % (project, proj.get("name")))
                                         if proj else "%s NOT FOUND" % project))
        if project and not proj:
            problems.append("jira: project %s is not visible to this account" % project)
        jql = os.environ.get("JIRA_INTAKE_JQL", "")
        if jql:
            try:
                found = jira.search(jql, limit=5)
                print("   intake JQL    : %d ticket(s) match" % len(found))
            except JiraError as e:
                print("   intake JQL    : INVALID — %s" % e.message)
                problems.append("jira: intake JQL is invalid")
    except (SystemExit, JiraError) as e:
        print("   FAILED: %s" % e)
        problems.append("jira: %s" % e)
        me = {}

    print("\n── Approval gate")
    approvers = csv("FOREMAN_APPROVERS")
    print("   approvers     : %s" % (", ".join(approvers) or "NONE"))
    if me.get("accountId") and me["accountId"] in approvers:
        print("   ⚠ Foreman authenticates as an account that is also on the")
        print("     approver list. Approvals will work, but the ticket history")
        print("     cannot show who approved — both are the same name. Give")
        print("     Foreman its own Atlassian service account before rollout.")
    if not approvers:
        problems.append("gate: FOREMAN_APPROVERS is empty — no account could "
                        "ever approve, so every ticket would stall at the "
                        "first gate")
    print("   commands      : %s | %s | %s"
          % (os.environ.get("FOREMAN_CMD_APPROVE", "/foreman approve"),
             os.environ.get("FOREMAN_CMD_CHANGES", "/foreman changes"),
             os.environ.get("FOREMAN_CMD_REJECT", "/foreman reject")))
    print("   timeout       : %dh" % num("FOREMAN_APPROVAL_TIMEOUT_HOURS", 72))
    print("   max revisions : %d" % num("FOREMAN_MAX_REVISIONS", 3))
    cmode, cmode_ok = comment_mode()
    print("   comment mode  : %s" % (cmode if cmode_ok else "%s (INVALID value %r)"
                                     % (cmode, os.environ.get("FOREMAN_COMMENT_MODE"))))
    if not cmode_ok:
        problems.append("comments: FOREMAN_COMMENT_MODE must be one of %s"
                        % " | ".join(COMMENT_MODES))

    print("\n── Code")
    mode = os.environ.get("FOREMAN_GIT_MODE", "local")
    src = repo_source(os.path.abspath(args.root))
    print("   git mode      : %s" % mode)
    print("   source        : %s" % (src or "<unset>"))
    if mode == "local":
        from . import gitops
        ok = src and os.path.isdir(src) and gitops.ensure_repo(src)
        print("   sandbox repo  : %s" % ("ok" if ok else "NOT a git repository"))
        if not ok:
            problems.append("git: FOREMAN_LOCAL_REPO is not a git repository. "
                            "Run `foreman provision --sandbox` to create one.")
    else:
        gh = github_from_env()
        if gh is None:
            problems.append("git: FOREMAN_GIT_MODE=github but GITHUB_WRITE_TOKEN is unset")
        else:
            try:
                print("   github user   : %s" % gh.whoami().get("login"))
            except GitHubError as e:
                problems.append("github: %s" % e)
    print("   base branch   : %s" % os.environ.get("FOREMAN_BASE_BRANCH", "main"))
    print("   test command  : %s" % (os.environ.get("FOREMAN_TEST_COMMAND")
                                     or "<unset — verification cannot run>"))
    if not os.environ.get("FOREMAN_TEST_COMMAND"):
        problems.append("test: FOREMAN_TEST_COMMAND is unset. The verify stage "
                        "would have nothing to run, and the sign-off gate "
                        "would show no evidence.")

    print("\n── Guardrails")
    root = os.path.abspath(args.root)
    gpath = os.path.join(root, "policy", "guardrails.yaml")
    g = guard_mod.Guardrails(yamlish.load(gpath) if os.path.exists(gpath) else {})
    print("   allow paths   : %s" % ", ".join(g.allow[:6]))
    print("   budget        : %d files / %d lines (%d deleted)"
          % (g.max_files, g.max_lines, g.max_deleted_lines))
    print("   test edits    : %s" % ("ALLOWED — see policy/guardrails.yaml"
                                     if g.allow_test_edits else "blocked"))
    if g.allow_test_edits:
        problems.append("guardrails: allow_test_edits is true. The cheapest way "
                        "to make a failing test pass is to change the test.")

    _doctor_ai-sdlc(problems)

    print("\n── Write interlocks")
    print("   FOREMAN_WRITE : %s" % os.environ.get("FOREMAN_WRITE", "false"))
    print("   deploy mode   : %s" % os.environ.get("FOREMAN_DEPLOY_MODE", "none"))
    print("   auto merge    : %s" % os.environ.get("FOREMAN_AUTO_MERGE", "false"))
    print("   max tickets   : %s" % os.environ.get("FOREMAN_MAX_TICKETS", "5"))

    print()
    if problems:
        print("%d problem(s):" % len(problems))
        for p in problems:
            print("  ✗ %s" % p)
        return 1
    print("✓ everything checks out")
    return 0


def _doctor_ai-sdlc(problems: List[str]) -> None:
    import shutil
    from . import mcpcfg, stories
    print("\n── AI-SDLC flow")
    base = os.environ.get("FOREMAN_BASE_BRANCH", "main")
    print("   DEV branch    : %s   (PRs target it; merging it starts your pipeline)" % base)
    types = stories.issue_types()
    print("   issue types   : feature=%s epic=%s story=%s"
          % (types["feature"] or "-", types["epic"], types["story"]))
    if not os.environ.get("FOREMAN_EPIC_LINK_FIELD") and \
            os.environ.get("JIRA_DEPLOYMENT", "auto").lower() == "datacenter":
        problems.append("ai-sdlc: Jira Data Center usually needs FOREMAN_EPIC_LINK_FIELD "
                        "(the Epic Link custom field id) to attach stories to epics")
    jql = os.environ.get("JIRA_INTAKE_JQL", "")
    if jql and "foreman-requirement" not in jql and "labels in" not in jql.lower():
        problems.append("ai-sdlc: JIRA_INTAKE_JQL does not select `foreman-requirement` "
                        "tickets, so requirements will never be picked up")
    if jql and "foreman-blocked" not in jql:
        print("   ⚠ intake JQL does not exclude `foreman-blocked`; a story waiting on "
              "another would be worked too early")

    print("   merge         : %s" % ("Foreman merges after Gate 3"
                                    if flag("FOREMAN_AUTO_MERGE")
                                    else "a person merges (FOREMAN_AUTO_MERGE off); Foreman waits"))
    mode = os.environ.get("FOREMAN_DEPLOY_MODE", "none")
    print("   deploy mode   : %s" % mode)
    if mode == "okd":
        ns, sel = (os.environ.get("FOREMAN_OKD_NAMESPACE", ""),
                   os.environ.get("FOREMAN_OKD_LABEL_SELECTOR", ""))
        oc = shutil.which(os.environ.get("FOREMAN_OC_BINARY", "oc")) or shutil.which("kubectl")
        print("   oc binary     : %s" % (oc or "NOT FOUND"))
        print("   namespace     : %s   selector: %s" % (ns or "<unset>", sel or "<unset>"))
        if not (ns and sel):
            problems.append("okd: FOREMAN_OKD_NAMESPACE and FOREMAN_OKD_LABEL_SELECTOR must be set")
        if not oc:
            problems.append("okd: neither `oc` nor `kubectl` is on PATH")
        elif ns and sel:
            from . import deploy as deploy_mod
            items, err = deploy_mod.list_pods(ns, sel)
            if err:
                print("   pods          : cannot read -- %s" % err[:120])
                problems.append("okd: `oc get pods -n %s -l %s` failed: %s" % (ns, sel, err[:120]))
            else:
                print("   pods          : %d matched, %d Ready"
                      % (len(items), sum(1 for p in items if p["ready"])))
                if not items:
                    problems.append("okd: the label selector matches no pods")

    m = mcpcfg.mode()
    print("   mappings      : %s" % ("TapiForge MCP" if m == "mcp" else
                                     "Excel/JSON diff for a person to apply (no TapiForge MCP)"))
    if m == "mcp":
        r, w = mcpcfg._csv("TAPIFORGE_MCP_READ_TOOLS"), mcpcfg._csv("TAPIFORGE_MCP_WRITE_TOOLS")
        print("   tapiforge     : %s" % (os.environ.get("TAPIFORGE_MCP_URL")
                                         or os.environ.get("TAPIFORGE_MCP_COMMAND")))
        print("   read tools    : %s" % (", ".join(r) or "<none listed>"))
        print("   write tools   : %s" % (", ".join(w) or "<none> -- a person will apply confirmed mappings"))
        if not r:
            problems.append("tapiforge: TAPIFORGE_MCP_READ_TOOLS is empty -- the AI would "
                            "not be allowed to read any mapping")
        if os.environ.get("TAPIFORGE_MCP_COMMAND") and not shutil.which(
                os.environ["TAPIFORGE_MCP_COMMAND"].split()[0]):
            problems.append("tapiforge: command %r is not on PATH"
                            % os.environ["TAPIFORGE_MCP_COMMAND"])

    for name, needed in (("servicenow", ("SNOW_INSTANCE", "SNOW_USER", "SNOW_PASSWORD")),
                         ("email", ("IMAP_HOST", "IMAP_USER", "IMAP_PASSWORD"))):
        have = [n for n in needed if os.environ.get(n)]
        if have and len(have) != len(needed):
            problems.append("intake: %s is half configured (missing %s)"
                            % (name, ", ".join(n for n in needed if n not in have)))
    if os.environ.get("SNOW_DONE_STATE", "").strip() in ("6", "7"):
        problems.append("intake: SNOW_DONE_STATE resolves/closes incidents on import, "
                        "telling the business a requirement is fixed before it is built")


def cmd_states(args):
    print(machine.describe())
    print("\nGates:")
    for name, spec in machine.GATES.items():
        print("  %-20s %s" % (name, spec["question"]))
        for verdict in ("approve", "changes", "reject"):
            print("      %-8s → %s" % (verdict, spec[verdict]))
    return 0


# ───────────────────────────────────────────────────────────────── the loop ─
def cmd_run(args):
    live = bool(args.live)
    if live and not flag("FOREMAN_WRITE"):
        print("NOTE: --live given but FOREMAN_WRITE is '%s'. Both interlocks "
              "must be set. Continuing as a DRY RUN."
              % os.environ.get("FOREMAN_WRITE", "false"))
        live = False

    ctx = build_context(args, live)
    started = time.time()
    print("═══ AI-SDLC Foreman run %s ═══" % ctx.run_id)
    print("mode        : %s" % ("LIVE — the board will be written to"
                                if live else "DRY RUN — nothing is written"))
    print("site        : %s" % ctx.jira.site)
    print("repository  : %s" % (ctx.repo_source or "<unset>"))
    print("bob         : %s (%s)" % (str(ctx.bob.capabilities().get("_version", "?")
                                         ).splitlines()[0], ctx.bob.dialect))

    if not args.skip_preflight:
        problems = ctx.bob.preflight()
        if problems and not args.force:
            print("\nPREFLIGHT FAILED:")
            for p in problems:
                print("  ✗ %s" % p)
            print("\nRun `foreman doctor` for the full picture, or --force to "
                  "proceed anyway.")
            return 1

    if args.ticket:
        issues = []
        for key in args.ticket:
            issue = ctx.jira.get_issue(key, expand="renderedFields")
            if issue:
                issues.append(issue)
            else:
                print("  %s not found or not visible" % key)
    else:
        jql = args.jql or os.environ.get("JIRA_INTAKE_JQL", "")
        if not jql:
            print("FATAL: no JQL. Set JIRA_INTAKE_JQL or pass --jql.")
            return 2
        print("intake JQL  : %s" % jql)
        issues = ctx.jira.search(jql, limit=200)

    cap = args.max_tickets or num("FOREMAN_MAX_TICKETS", 5)
    print("matched     : %d ticket(s), cap %d\n" % (len(issues), cap))

    rows, advanced = [], 0
    for issue in issues:
        if advanced >= cap:
            print("  cap reached — %d ticket(s) left for the next run"
                  % (len(issues) - advanced))
            break
        row = _work_one(ctx, issue, live)
        rows.append(row)
        if row["moved"]:
            advanced += 1

    _summary(rows, ctx, started, advanced, live)
    ctx.store.record_run(ctx.run_id, started, live, len(rows), advanced,
                         {"rows": rows})
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump({"run_id": ctx.run_id, "live": live, "rows": rows},
                      fh, indent=2, default=str)
        print("result      : %s" % args.out)
    return 0


def _work_one(ctx: stages.Context, issue: Dict[str, Any],
              live: bool) -> Dict[str, Any]:
    key = issue.get("key", "?")
    try:
        ticket = stages.load_ticket(ctx, issue)
    except JiraError as e:
        print("── %-10s  LOAD FAILED: %s" % (key, e))
        return {"key": key, "from": "?", "to": "?", "moved": False,
                "reason": "load failed: %s" % e, "ok": False}

    frm = ticket["fsm"]
    ctx.store.seen(key, os.environ.get("JIRA_PROJECT", ""), ticket["summary"],
                   frm, ticket["revision"])
    print("── %-10s %-18s %s" % (key, frm, ticket["summary"][:54]))

    outcome = stages.advance(ctx, ticket)
    moved = outcome.next_state != frm

    if moved:
        ctx.store.record_transition(key, frm, outcome.next_state,
                                    outcome.reason, "foreman", ctx.run_id)
        # `seen` above recorded the state the ticket arrived in. Without this,
        # `foreman report` shows every ticket one step behind reality.
        if live:
            ctx.store.set_ticket(key, state=outcome.next_state)

    if live:
        _apply(ctx, ticket, outcome, moved)
    elif outcome.comment:
        verb = ("post or update" if outcome.sticky
                and comment_mode()[0] == "update" else "post a new")
        print("      [dry run] would %s '%s' comment (%d chars)"
              % (verb, outcome.sticky or "note", len(outcome.comment)))

    marker = "→" if moved else "·"
    print("   %s %-18s %s" % (marker, outcome.next_state if moved else "(held)",
                              outcome.reason))
    if not outcome.ok and outcome.detail:
        print("      detail: %s" % outcome.detail[:200].replace("\n", " "))

    return {"key": key, "summary": ticket["summary"], "from": frm,
            "to": outcome.next_state, "moved": moved, "ok": outcome.ok,
            "reason": outcome.reason, "revision": ticket["revision"],
            "detail": outcome.detail[:400],
            "artifacts": outcome.as_dict()["artifacts"]}


def _apply(ctx: stages.Context, ticket: Dict[str, Any],
           outcome: stages.Outcome, moved: bool):
    """Write the outcome to the board: comment, labels, mirrored status."""
    key = ticket["key"]

    if outcome.comment.strip():
        try:
            existing = None
            if outcome.sticky and comment_mode()[0] == "update":
                existing = ctx.jira.find_marked_comment(
                    key, render.marker(outcome.sticky))
            if existing:
                ctx.jira.update_comment(key, existing["id"], outcome.comment)
            else:
                ctx.jira.add_comment(key, outcome.comment)
        except JiraError as e:
            print("      comment FAILED: %s" % e)

    if not moved:
        return

    target = machine.get(outcome.next_state)
    try:
        ctx.jira.set_labels(key, add=[target.label],
                            remove=machine.labels_to_clear(target.label))
    except JiraError as e:
        print("      label FAILED: %s" % e)

    mirror = ((ctx.cfg.get("jira") or {}).get("status_map") or {})
    want = mirror.get(outcome.next_state)
    if want:
        try:
            applied = ctx.jira.transition_to(key, want)
            if applied:
                print("      status → %s" % want)
        except JiraError:
            pass


def _summary(rows, ctx, started, advanced, live):
    print("\n═══ summary ═══")
    print("%-10s %-18s %-18s %s" % ("TICKET", "FROM", "TO", "WHY"))
    print("%-10s %-18s %-18s %s" % ("-" * 10, "-" * 18, "-" * 18, "-" * 40))
    for r in rows:
        print("%-10s %-18s %-18s %s"
              % (r["key"], r["from"], r["to"] if r["moved"] else "(held)",
                 r["reason"][:52]))
    gates = [r for r in rows if r["to"] in machine.GATES and not r["moved"]]
    failed = [r for r in rows if not r["ok"]]
    print("\nseen %d · advanced %d · waiting at a gate %d · failed %d · "
          "%.0fs · $%.4f"
          % (len(rows), advanced, len(gates), len(failed),
             time.time() - started, ctx.spend))
    if gates:
        print("\nWaiting on a human:")
        for r in gates:
            print("  %s — %s" % (r["key"], r["reason"]))
    if not live:
        print("\nThis was a DRY RUN. To act: ./start.sh --live "
              "(with FOREMAN_WRITE=true)")


# ───────────────────────────────────────────────────────────────── reports ──
def cmd_show(args):
    ctx = build_context(args, live=False)
    for key in args.ticket:
        issue = ctx.jira.get_issue(key)
        if not issue:
            print("%s: not found" % key)
            continue
        t = stages.load_ticket(ctx, issue)
        st = t["state"]
        print("\n═══ %s  %s" % (key, t["summary"]))
        print("  jira status : %s" % t["status"])
        print("  foreman     : %s (revision %d)" % (t["fsm"], t["revision"]))
        print("  labels      : %s" % ", ".join(t["labels"]))
        for field, label in (("requirement", "requirement"), ("story_type", "story type"),
                             ("epic", "epic"), ("branch", "branch"),
                             ("pr_url", "pull request"), ("commit_sha", "commit"),
                             ("merge_sha", "merged as"), ("failed_from", "failed in"),
                             ("fix_version", "fixVersion"), ("prod_ticket", "prod ticket")):
            if st.get(field):
                print("  %-11s : %s" % (label, st[field]))
        if st.get("test"):
            print("  tests       : %s (`%s`)"
                  % ("pass" if st["test"].get("passed") else "FAIL",
                     st["test"].get("command")))
        if st.get("tracking"):
            tr = st["tracking"]
            print("  stories     : %d  (%d Foreman-owned)" % (
                len(tr), sum(1 for v in tr.values() if v.get("owner") == "foreman")))
            for k, v in tr.items():
                print("    %-10s %-12s %s" % (k, v.get("type"),
                      "waiting on " + ",".join(v["blocked_by"]) if not v.get("activated") else ""))
        if st.get("dev_test"):
            print("  DEV tests   : %s (`%s`)" % (
                "pass" if st["dev_test"].get("passed") else "FAIL", st["dev_test"].get("command")))
        if st.get("decision"):
            d = st["decision"]
            print("  decision    : %s by %s at %s"
                  % (d.get("verdict"), d.get("author"), d.get("created")))
        hist = ctx.store.history(key)
        if hist:
            print("\n  history:")
            for h in hist:
                print("    %s  %-18s → %-18s %s"
                      % (time.strftime("%Y-%m-%d %H:%M",
                                       time.localtime(h["at"])),
                         h["from_state"], h["to_state"], h["reason"][:44]))
    return 0


def cmd_report(args):
    store = Store(paths_from_env(os.path.abspath(args.root))["db"])
    print("Tickets by state:")
    for state, n in sorted(store.summary().items()):
        print("  %-20s %d" % (state, n))
    print("\nRecent runs:")
    for r in store.recent_runs(15):
        print("  %s  %-6s %3d seen  %3d advanced  %5.0fs"
              % (r["run_id"], "LIVE" if r["live"] else "dry", r["tickets"],
                 r["advanced"], r["duration"]))
    return 0


# ───────────────────────────────────────────────────────────────────── main ─
def main(argv=None):
    p = argparse.ArgumentParser(
        prog="foreman",
        description="AI-SDLC Foreman — Jira-driven delivery automation over "
                    "Bob Shell.")
    p.add_argument("--config", default="foreman.yaml")
    p.add_argument("--env", default="../.env",
                   help=".env file; real environment variables win over it")
    p.add_argument("--root", default="..",
                   help="project root (holds policy/, extension/, var/)")
    p.add_argument("--run-id", default=time.strftime("%Y%m%dT%H%M%SZ",
                                                     time.gmtime()))
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("doctor", help="verify Bob, Jira, git, policy and interlocks")
    d.set_defaults(fn=cmd_doctor)

    s = sub.add_parser("states", help="print the state machine")
    s.set_defaults(fn=cmd_states)

    r = sub.add_parser("run", help="advance every ticket the intake JQL matches")
    r.add_argument("--live", action="store_true",
                   help="write to Jira and git (also needs FOREMAN_WRITE=true)")
    r.add_argument("--ticket", nargs="*", help="specific keys instead of the JQL")
    r.add_argument("--jql", help="override JIRA_INTAKE_JQL for this run")
    r.add_argument("--max-tickets", type=int)
    r.add_argument("--out", help="write the run result JSON here")
    r.add_argument("--force", action="store_true",
                   help="run despite preflight problems")
    r.add_argument("--skip-preflight", action="store_true")
    r.set_defaults(fn=cmd_run)

    sh = sub.add_parser("show", help="what Foreman knows about a ticket")
    sh.add_argument("ticket", nargs="+")
    sh.set_defaults(fn=cmd_show)

    rp = sub.add_parser("report", help="cross-ticket summary from the store")
    rp.set_defaults(fn=cmd_report)

    from .provision import add_parser as add_provision
    add_provision(sub)

    from .intake import add_parser as add_intake
    add_intake(sub)

    args = p.parse_args(argv)
    load_env(args.env)
    try:
        return args.fn(args) or 0
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
