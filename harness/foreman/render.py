"""
What Foreman says on the ticket.

A reviewer approves or rejects a code change based on what they read here, on
their phone, between meetings. If the analysis is a wall of JSON they will
approve it without reading it, and the gate becomes ceremony. So these are
written as briefings: the decision first, the evidence under it, the machinery
folded away in an expander.

Three conventions run through all of them:

* **Every comment states what happens next, and what the reader must type.**
  A gate that does not say how to pass it is a gate people escalate around.
* **Every comment carries a marker**, so Foreman can recognise its own output:
  the gate never mistakes it for a human decision, and in
  `FOREMAN_COMMENT_MODE=update` Foreman finds and rewrites its previous comment
  for a stage instead of adding another. The default, `append`, posts each
  revision as a new comment, because a reviewer asked "what did you approve?"
  must be able to point at the plan they actually read.
* **Uncertainty is stated, not smoothed.** Where the harness corrected a line
  number, rejected a citation, or could not verify something, the comment says
  so. A reviewer who discovers that on their own stops trusting the rest.
"""
from typing import Any, Dict, List, Optional

MARK = "⟦ai-sdlc-foreman/%s⟧"


def marker(kind: str) -> str:
    return MARK % kind


def _footer(kind: str, ticket: str, revision: int, run_id: str) -> str:
    return ("\n\n---\n_%s · %s · revision %d · run `%s`_"
            % (marker(kind), ticket, revision, run_id or "-"))


def _bullets(items: List[str], empty: str = "_none_") -> str:
    items = [str(i).strip() for i in (items or []) if str(i).strip()]
    return "\n".join("- %s" % i for i in items) if items else empty


def _evidence(evidence: List[Dict[str, Any]], limit: int = 6) -> str:
    if not evidence:
        return "_no code citations survived verification._"
    out = []
    for e in evidence[:limit]:
        loc = "`%s:%s-%s`" % (e.get("file"), e.get("line_start"),
                              e.get("line_end"))
        corrected = " _(line numbers corrected by the harness)_" \
            if e.get("line_numbers_corrected") else ""
        out.append("**%s**%s\n\n%s\n\n```\n%s\n```"
                   % (loc, corrected, e.get("why", "").strip(),
                      str(e.get("snippet", ""))[:1200]))
    if len(evidence) > limit:
        out.append("_…and %d further citation(s); see the run report._"
                   % (len(evidence) - limit))
    return "\n\n".join(out)


# ──────────────────────────────────────────────────────────────── analysis ──
def analysis_comment(ticket: str, analysis: Dict[str, Any],
                     plan: Dict[str, Any], review: Optional[Dict[str, Any]],
                     repo: str, commit: str, revision: int, run_id: str,
                     approve_cmd: str, changes_cmd: str, reject_cmd: str,
                     rejections: Optional[List[Dict[str, str]]] = None) -> str:
    """The plan gate. This is the comment that gets a code change authorised."""
    files = plan.get("files_to_change", [])
    est = plan.get("estimated_diff", {}) or {}

    body = ["# Analysis and proposed change",
            "",
            "**%s**" % analysis.get("problem_statement", "").strip(),
            "",
            analysis.get("summary", "").strip(),
            "",
            "## Root cause",
            analysis.get("root_cause", "_not established_").strip(),
            "",
            "## Evidence",
            _evidence(analysis.get("evidence", [])),
            "",
            "## Proposed change",
            plan.get("approach", "").strip(),
            "",
            "| File | Action | Why |",
            "|---|---|---|"]
    for f in files[:20]:
        body.append("| `%s` | %s | %s |" % (f.get("path"), f.get("action"),
                                            str(f.get("why", ""))[:160]))
    body += ["",
             "Estimated diff: **%s file(s), ~%s line(s)**. Risk: **%s**."
             % (est.get("files", len(files)), est.get("lines", "?"),
                analysis.get("risk", "unknown")),
             "",
             "## How this will be judged",
             _bullets(plan.get("acceptance_criteria", [])),
             "",
             "## Deliberately not doing",
             _bullets(analysis.get("out_of_scope", []),
                      "_nothing recorded as out of scope._")]

    risks = plan.get("risks", [])
    if risks:
        body += ["", ":::warning", "**Risks identified**", ""]
        body += ["- **%s** — mitigation: %s" % (r.get("risk"), r.get("mitigation"))
                 for r in risks[:6]]
        body += [":::"]

    if review and review.get("verdict") != "accept":
        body += ["", ":::note",
                 "**The adversarial reviewer did not fully accept this plan** "
                 "(verdict: `%s`). It is being put to you anyway, with its "
                 "objections, because that is your call and not the pipeline's."
                 % review.get("verdict"), ""]
        body += ["- %s" % r for r in
                 (review.get("blocking") or review.get("reasons") or [])[:6]]
        body += [":::"]

    body += ["", ":::expand Machinery — repository, verification, what was discarded",
             "",
             "- Repository: `%s` at `%s`" % (repo, (commit or "")[:12]),
             "- Citations verified against the checkout: **%d**"
             % len(analysis.get("evidence", [])),
             "- Adversarial review verdict: **%s**"
             % ((review or {}).get("verdict", "not run"))]
    if rejections:
        body += ["- Discarded by the output contract:"]
        body += ["    - `%s` %s" % (r.get("code"), str(r.get("detail", ""))[:120])
                 for r in rejections[:10]]
    body += ["",
             "Steps the build stage will follow:", ""]
    for s in plan.get("steps", [])[:12]:
        body.append("%d. %s" % (s.get("n", 0), s.get("intent", "")))
    body += ["", ":::"]

    body += ["", "---", "", "## Your decision",
             "",
             "Nothing further happens on this ticket until you reply. Comment "
             "with one of:",
             "",
             "| Comment | Result |",
             "|---|---|",
             "| `%s` | Foreman writes the code, opens a pull request and "
             "deploys to non-prod |" % approve_cmd,
             "| `%s <what to change>` | Foreman re-plans with your feedback |"
             % changes_cmd,
             "| `%s <why>` | Foreman stops and touches this ticket no further |"
             % reject_cmd,
             "",
             "_Only accounts on the approver allowlist can decide. A comment "
             "from anyone else is recorded and ignored._"]

    return "\n".join(body) + _footer("analysis", ticket, revision, run_id)


# ─────────────────────────────────────────────────────────────────── build ──
def build_comment(ticket: str, impl: Dict[str, Any], diff: Dict[str, Any],
                  pr_url: str, branch: str, commit_sha: str,
                  test_result: Optional[Dict[str, Any]],
                  deploy: Optional[Dict[str, Any]],
                  revision: int, run_id: str) -> str:
    body = ["# Change written",
            "",
            impl.get("summary", "").strip(),
            "",
            "| | |",
            "|---|---|",
            "| Branch | `%s` |" % branch,
            "| Commit | `%s` |" % (commit_sha or "")[:12],
            "| Diff | %d file(s), +%d / −%d |"
            % (diff.get("file_count", 0), diff.get("added", 0),
               diff.get("removed", 0))]
    if pr_url:
        body.append("| Pull request | %s |" % pr_url)
    if test_result:
        body.append("| Tests | %s (`%s`) |"
                    % ("passing" if test_result.get("passed") else "**FAILING**",
                       test_result.get("command", "")))
    if deploy and not deploy.get("skipped"):
        body.append("| Non-prod deploy | %s%s |"
                    % (deploy.get("status"),
                       (" — " + deploy["url"]) if deploy.get("url") else ""))

    body += ["", "## Files changed", "",
             "| File | Action | Lines |", "|---|---|---|"]
    for f in (diff.get("files") or [])[:25]:
        body.append("| `%s` | %s | +%d / −%d |"
                    % (f.get("path"), f.get("action"), f.get("added", 0),
                       f.get("removed", 0)))

    dev = impl.get("deviations") or []
    if dev:
        body += ["", ":::warning", "**Deviations from the approved plan**", ""]
        body += ["- %s — %s" % (d.get("from_plan"), d.get("why")) for d in dev[:6]]
        body += [":::"]

    warnings = [v for v in (diff.get("violations") or []) if not v.get("blocking")]
    if warnings:
        body += ["", ":::note",
                 "**Non-blocking guardrail notes** — the change proceeded, but "
                 "these are worth your eye at sign-off.", ""]
        body += ["- `%s` `%s` — %s" % (v.get("code"), v.get("path"),
                                       str(v.get("detail", ""))[:140])
                 for v in warnings[:8]]
        body += [":::"]

    return "\n".join(body) + _footer("build", ticket, revision, run_id)


# ────────────────────────────────────────────────────────────── verification ─
def verify_comment(ticket: str, verification: Dict[str, Any],
                   test_result: Dict[str, Any], criteria: List[str],
                   pr_url: str, deploy: Optional[Dict[str, Any]],
                   revision: int, run_id: str,
                   approve_cmd: str, changes_cmd: str, reject_cmd: str) -> str:
    """The sign-off gate. The test result here is the harness's, not the model's."""
    passed = bool(test_result.get("passed"))
    body = ["# Verification",
            "",
            ":::%s" % ("success" if passed else "error"),
            "**%s** — `%s` exited %s in %ss."
            % ("Test suite passes" if passed else "TEST SUITE FAILS",
               test_result.get("command", ""), test_result.get("exit_code"),
               test_result.get("duration_sec")),
            ":::",
            "",
            "This result was produced by the harness running the suite itself. "
            "It is not the model's assessment of its own work.",
            "",
            verification.get("summary", "").strip(),
            "",
            "## Tests added", "",
            "| Test | Covers |", "|---|---|"]
    for t in verification.get("tests_added", [])[:20]:
        body.append("| `%s`%s | %s |"
                    % (t.get("path"),
                       (" · " + t["name"]) if t.get("name") else "",
                       str(t.get("criterion", ""))[:160]))

    cov = verification.get("coverage_of_criteria") or []
    if cov:
        body += ["", "## Acceptance criteria", "",
                 "| Criterion | Covered | By |", "|---|---|---|"]
        for c in cov[:20]:
            body.append("| %s | %s | %s |"
                        % (str(c.get("criterion", ""))[:180],
                           "yes" if c.get("covered") else "**no**",
                           str(c.get("covered_by", ""))[:120]))
        uncovered = [c for c in cov if not c.get("covered")]
        if uncovered:
            body += ["", ":::warning",
                     "**%d acceptance criterion/criteria are not covered by a "
                     "test.** Read these before signing off — this is the part "
                     "the automation cannot vouch for." % len(uncovered), ":::"]
    elif criteria:
        body += ["", "## Acceptance criteria", "", _bullets(criteria)]

    if not passed:
        body += ["", ":::expand Test output (tail)", "",
                 "```", str(test_result.get("stdout", ""))[-3000:], "```", ":::"]

    if deploy and not deploy.get("skipped"):
        body += ["", "## Non-prod deployment", "",
                 "Status: **%s**%s" % (deploy.get("status"),
                                       (" — " + deploy["url"]) if deploy.get("url") else "")]
    if pr_url:
        body += ["", "Pull request: %s" % pr_url]

    body += ["", "---", "", "## Sign-off",
             "",
             "| Comment | Result |", "|---|---|",
             "| `%s` | Ticket closes; the change is scheduled for production |"
             % approve_cmd,
             "| `%s <what to change>` | Foreman reworks the change |" % changes_cmd,
             "| `%s <why>` | Foreman stops; the branch is left for a human |"
             % reject_cmd]

    return "\n".join(body) + _footer("verify", ticket, revision, run_id)


# ──────────────────────────────────────────────────────────────────── misc ──
def needs_info_comment(ticket: str, analysis: Dict[str, Any], revision: int,
                       run_id: str) -> str:
    body = ["# Not enough to act on",
            "",
            "Foreman read this ticket and the repository, and stopped rather "
            "than guess. Building the wrong thing confidently is more expensive "
            "than asking.",
            "",
            analysis.get("summary", "").strip(),
            "",
            "## What would unblock this",
            _bullets(analysis.get("clarifications_needed", [])),
            "",
            "Add the detail to the ticket — description or a comment — and "
            "Foreman picks it up on the next run. Nothing else is needed from "
            "you; there is no command to type."]
    return "\n".join(body) + _footer("needs-info", ticket, revision, run_id)


def failure_comment(ticket: str, stage: str, reason: str, detail: str,
                    revision: int, run_id: str,
                    violations: Optional[List[Dict[str, Any]]] = None) -> str:
    body = ["# Stopped at the `%s` stage" % stage,
            "",
            ":::error",
            reason.strip() or "The stage failed.",
            ":::",
            ""]
    if violations:
        body += ["## Guardrail violations",
                 "",
                 "The change was written and then **reverted in full**. Nothing "
                 "was committed or pushed.",
                 "",
                 "| Rule | File | Detail |", "|---|---|---|"]
        for v in violations[:15]:
            body.append("| `%s`%s | `%s` | %s |"
                        % (v.get("code"), " **(blocking)**" if v.get("blocking") else "",
                           v.get("path") or "—", str(v.get("detail", ""))[:200]))
        body += [""]
    if detail:
        body += [":::expand Detail", "", "```", detail[:4000], "```", ":::", ""]
    body += ["Foreman will not retry this automatically. A human needs to look "
             "at it. Once it is addressed, remove the `foreman:failed` label "
             "and Foreman picks the ticket up from the beginning."]
    return "\n".join(body) + _footer("failed", ticket, revision, run_id)


def rejected_attempts_note(attempts: List[Dict[str, Any]]) -> str:
    """Appended to a gate comment when someone tried to decide and could not.

    Surfaced rather than swallowed. An unauthorised approval attempt is a thing
    the team should see on the ticket, not a line in a log nobody opens.
    """
    if not attempts:
        return ""
    reasons = {"not_authorised": "not on the approver allowlist",
               "stale": "posted before this revision existed",
               "self": "posted by Foreman itself"}
    rows = ["", ":::warning",
            "**%d comment(s) looked like a decision and were not honoured:**"
            % len(attempts), ""]
    for a in attempts[:8]:
        rows.append("- `%s` by **%s** — %s"
                    % (a.get("verdict"), a.get("author"),
                       reasons.get(a.get("reason"), a.get("reason"))))
    rows += [":::"]
    return "\n".join(rows)


def closing_comment(ticket: str, pr_url: str, merged: bool, fix_version: str,
                    prod_ticket: str, decision_by: str, revision: int,
                    run_id: str) -> str:
    body = ["# Done",
            "",
            "Signed off by **%s**." % decision_by,
            "",
            "| | |", "|---|---|"]
    if pr_url:
        body.append("| Pull request | %s |" % pr_url)
    body.append("| Merged by Foreman | %s |" % ("yes" if merged else
                                                "no — left for a human"))
    if fix_version:
        body.append("| Fix version | `%s` |" % fix_version)
    if prod_ticket:
        body.append("| Production deployment | %s |" % prod_ticket)
    body += ["",
             "Foreman does not deploy to production. The change is scheduled; "
             "a person releases it."]
    return "\n".join(body) + _footer("closed", ticket, revision, run_id)


# ───────────────────────────────────────────────────────── AI-SDLC comments ──
def _gate_table(approve_cmd, changes_cmd, reject_cmd, on_approve, on_changes):
    return ["", "---", "", "## Your decision", "",
            "| Comment | Result |", "|---|---|",
            "| `%s` | %s |" % (approve_cmd, on_approve),
            "| `%s <what to change>` | %s |" % (changes_cmd, on_changes),
            "| `%s <why>` | Foreman stops; nothing further is created or changed |"
            % reject_cmd]


def simple_comment(title: str, body: str, ticket: str, kind: str,
                   revision: int, run_id: str) -> str:
    return "# %s\n\n%s" % (title, body.strip()) + _footer(kind, ticket, revision, run_id)


def decomposition_comment(ticket, d, revision, run_id, approve_cmd, changes_cmd,
                          reject_cmd, corrections=None, rejections=None) -> str:
    from . import stories
    n = sum(len(e["stories"]) for e in d["epics"])
    body = ["# Proposed Feature / Epic / Story breakdown",
            "",
            "Nothing has been created in Jira yet. Approving creates **%d epic(s)** "
            "and **%d stories**; Foreman works only the stories marked *Foreman*, "
            "in the order shown, and waits for a person to finish the rest."
            % (len(d["epics"]), n), ""]
    if d.get("impact_analysis"):
        body += ["## Impact analysis", "", d["impact_analysis"][:4000], ""]
    body += ["## Breakdown", "", stories.tree_markdown(d)]
    notes = []
    for c in (corrections or []):
        if c.get("added"):
            notes.append("added a **%s** story to epic `%s` (%s)"
                         % (c["added"], c["epic"], c["why"]))
        elif c.get("dropped"):
            notes.append("dropped the **%s** story from epic `%s` (%s)"
                         % (c["dropped"], c["epic"], c["why"]))
    for x in (rejections or [])[:6]:
        notes.append("ignored an entry: %s (%s)" % (x["code"], x["detail"][:80]))
    if notes:
        body += [":::warning", "**The harness adjusted the draft:**", ""]
        body += ["- %s" % x for x in notes] + [":::", ""]
    body += _gate_table(approve_cmd, changes_cmd, reject_cmd,
                        "Foreman creates the Feature, Epics and Stories",
                        "Foreman redraws the breakdown")
    return "\n".join(body) + _footer("decompose", ticket, revision, run_id)


def materialised_comment(ticket, tree_md, revision, run_id) -> str:
    return simple_comment(
        "Feature, Epics and Stories created",
        tree_md + "\n\nStories that depend on another (development waits for "
        "mapping and pipeline) are released automatically when their blockers "
        "are **Done**. This ticket closes when every story is Done.",
        ticket, "materialised", revision, run_id)


def tracking_done_comment(ticket, keys, revision, run_id) -> str:
    return simple_comment("Requirement complete",
                          "All %d stories are Done:\n\n%s"
                          % (len(keys), "\n".join("- %s" % k for k in keys)),
                          ticket, "tracking-done", revision, run_id)


def mapping_comment(ticket, m, via_mcp, attached, revision, run_id,
                    approve_cmd, changes_cmd, reject_cmd) -> str:
    body = ["# Proposed mapping changes", ""]
    if m.get("interface"):
        body += ["**Interface:** %s" % m["interface"], ""]
    if m.get("summary"):
        body += [m["summary"], ""]
    if m.get("unverified"):
        body += [":::warning",
                 "**These were not checked against TapiForge.** The AI did not read "
                 "the current mappings (%s), so every `current` value is "
                 "unverified. Check each row against TapiForge before approving."
                 % ("TapiForge is not connected" if not via_mcp
                    else "it reports it did not consult TapiForge"),
                 ":::", ""]
    body += ["| Action | Target | Source | Rule | Currently |", "|---|---|---|---|---|"]
    for c in m["changes"][:60]:
        body.append("| %s | `%s` | %s | %s | %s |" % (
            c["action"], c["target"][:80], (c["source"] or "—")[:60],
            (c["rule"] or "—")[:120].replace("|", "/"),
            (c["current"] or "—")[:80].replace("|", "/")))
    if len(m["changes"]) > 60:
        body.append("_…and %d more; see the attachment._" % (len(m["changes"]) - 60))
    if m.get("open_questions"):
        body += ["", "## Open questions", _bullets(m["open_questions"])]
    if attached:
        body += ["", "Attached for download: %s" % attached]
    if via_mcp:
        on_ok = "Foreman writes these changes to TapiForge, then closes this story"
    else:
        on_ok = ("Foreman takes it that **you have applied these in TapiForge** and "
                 "closes this story; development is released after that")
    body += _gate_table(approve_cmd, changes_cmd, reject_cmd, on_ok,
                        "Foreman redrafts the mappings")
    return "\n".join(body) + _footer("mapping", ticket, revision, run_id)


def codereview_comment(ticket, review, impl, diff, pr_url, branch, build_test,
                       base_branch, revision, run_id, approve_cmd, changes_cmd,
                       reject_cmd) -> str:
    v = review.get("verdict")
    body = ["# Code ready for sign-off", ""]
    if pr_url:
        body += ["Pull request: %s (into `%s`)" % (pr_url, base_branch), ""]
    else:
        body += ["Branch: `%s` (into `%s`)" % (branch, base_branch), ""]
    if impl.get("summary"):
        body += [impl["summary"].strip(), ""]
    if diff:
        body += ["%d file(s), +%s / -%s" % (diff.get("file_count", 0),
                                           diff.get("added", 0), diff.get("removed", 0)), ""]
    if build_test:
        body += [":::%s" % ("success" if build_test.get("passed") else "error"),
                 "Tests run before publishing: **%s** (`%s`, %ss). This is the "
                 "harness's own result."
                 % ("pass" if build_test.get("passed") else "FAIL",
                    build_test.get("command"), build_test.get("duration_sec")),
                 ":::", ""]
    body += ["## AI code review", "",
             {"pass": ":::success\nThe AI found no blocking concerns.\n:::",
              "concerns": ":::warning\nThe AI raised concerns. Read them before signing off.\n:::",
              }.get(v, ":::warning\nThe AI review is unavailable.\n:::"), "",
             review.get("summary", "").strip(), ""]
    fs = review.get("findings") or []
    if fs:
        body += ["| Severity | File | Finding |", "|---|---|---|"]
        for f in fs[:25]:
            body.append("| %s | %s | %s |" % (f["severity"], ("`%s`" % f["file"]) if f["file"] else "—",
                                              f["detail"][:220].replace("|", "/")))
        body.append("")
    body += ["_This review is the AI checking its own work. It is an aid, not a "
             "substitute for you reading the pull request._"]
    body += _gate_table(approve_cmd, changes_cmd, reject_cmd,
                        "Foreman merges the pull request into `%s`, deploys to DEV and runs the tests"
                        % base_branch, "Foreman reworks the change")
    return "\n".join(body) + _footer("codereview", ticket, revision, run_id)


def devtest_comment(ticket, result, criteria, pr_url, revision, run_id) -> str:
    body = [":::success",
            "**DEV tests pass** — `%s` exited %s in %ss. Result produced by the "
            "harness, not by the model." % (result.get("command"),
                                           result.get("exit_code"),
                                           result.get("duration_sec")),
            ":::", ""]
    if criteria:
        body += ["## Acceptance criteria", "", _bullets(criteria)]
    if pr_url:
        body += ["", "Pull request: %s" % pr_url]
    return simple_comment("Tested on DEV", "\n".join(body), ticket, "devtest",
                          revision, run_id)
