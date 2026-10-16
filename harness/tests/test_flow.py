"""The whole flow, end to end, offline.

Real: the state machine, every stage handler, the gate evaluation, the contract
validators, the guardrails, git (a throw-away local repository), the comment
renderers.  Faked: Jira (an in-memory board), GitHub (local mode needs none) and
Bob (a scripted stand-in that returns the JSON a mode would and writes the files
the build mode would).

If a stage hands the next one something it cannot use, this is where it shows.
"""
import copy
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from foreman import gate as gate_mod, guardrails as guard_mod, machine, stages, stories
from foreman.adapter import BobResult
from foreman.store import Store

APPROVER = "acct-approver"
BOT = "acct-bot"


class Board:
    """Enough of a Jira for the stages: issues, comments, labels, state, status."""
    def __init__(self):
        self.issues = {}
        self.n = 0
        self.attachments = []
        self.log = []

    # ── helpers for the tests ──
    def add(self, summary, labels, status="To Do", key=None):
        self.n += 1
        key = key or "TAP-%d" % self.n
        self.issues[key] = {"summary": summary, "labels": set(labels), "status": status,
                            "comments": [], "state": {}, "type": "Task"}
        return key

    def approve(self, key, text="/foreman approve"):
        """A reviewer replying now -- which is always after the gate they are
        answering opened, and before any gate that opens later."""
        opened = gate_mod.parse_ts(self.issues[key]["state"].get("gate_opened"))
        now = datetime.now(timezone.utc)
        if opened and now <= opened:
            now = opened + timedelta(milliseconds=1)
        self._comment_at(key, APPROVER, text, now)

    def _comment_at(self, key, author, body, when):
        c = self.issues[key]["comments"]
        c.append({"id": str(len(c) + 1), "body": body,
                  "author": {"accountId": author, "displayName": author},
                  "created": when.isoformat()})

    def _comment(self, key, author, body, delta=0):
        c = self.issues[key]["comments"]
        c.append({"id": str(len(c) + 1), "body": body,
                  "author": {"accountId": author, "displayName": author},
                  "created": (datetime.now(timezone.utc) + timedelta(seconds=delta)).isoformat()})

    def state(self, key):
        return self.issues[key]["state"].get("state", machine.INTAKE)

    # ── the JiraClient surface ──
    def comments(self, key, limit=200, newest_first=False):
        return list(self.issues[key]["comments"])

    def get_state(self, key):
        return copy.deepcopy(self.issues[key]["state"])

    def put_state(self, key, value):
        self.issues[key]["state"] = copy.deepcopy(value)
        return True

    def add_comment(self, key, body):
        self._comment(key, BOT, body)

    def set_labels(self, key, add=None, remove=None):
        l = self.issues[key]["labels"]
        l.update(add or [])
        l.difference_update(remove or [])
        return True

    def create_issue(self, project, summary, description, issue_type="Task",
                     labels=None, priority="", parent="", extra_fields=None):
        key = self.add(summary, labels or [])
        self.issues[key]["type"] = issue_type
        self.issues[key]["parent"] = parent
        return {"key": key}

    def transition_to(self, key, status):
        self.issues[key]["status"] = status
        return status

    def is_done(self, key):
        return self.issues[key]["status"] == "Done"

    def get_issue(self, key, fields=None, expand=None):
        return {"key": key, "fields": {"summary": self.issues[key]["summary"]}}

    def add_attachment(self, key, filename, content, mime="text/plain"):
        self.attachments.append(filename)
        return True

    def add_remote_link(self, *a, **k):
        return True

    def set_fix_version(self, *a, **k):
        return True

    def issue_json(self, key):
        i = self.issues[key]
        return {"key": key, "fields": {"summary": i["summary"], "labels": sorted(i["labels"]),
                                       "issuetype": {"name": i["type"]},
                                       "status": {"name": i["status"]},
                                       "updated": datetime.now(timezone.utc).isoformat()}}


def git(repo, *args):
    subprocess.run(["git", "-C", repo] + list(args), check=True, capture_output=True,
                   env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                            GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t"))


def make_repo():
    d = tempfile.mkdtemp()
    subprocess.run(["git", "init", "-q", "-b", "main", d], check=True)
    os.makedirs(os.path.join(d, "src"))
    os.makedirs(os.path.join(d, "tests"))
    with open(os.path.join(d, "src", "app.py"), "w") as fh:
        fh.write("def add(a, b):\n    return a + b\n")
    with open(os.path.join(d, "tests", "test_app.py"), "w") as fh:
        fh.write("from src.app import add\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    git(d, "add", "-A")
    git(d, "commit", "-q", "-m", "init")
    return d


DECOMP = {"decomposition": {
    "understood": True,
    "impact_analysis": "touches src/app.py only",
    "feature": {"summary": "Add subtraction", "description": "needed by OMS"},
    "epics": [{"id": "e1", "summary": "Arithmetic interface", "interface": "calc",
               "new_integration": True,
               "stories": [
                   {"story_type": "mapping", "summary": "Map fields",
                    "acceptance_criteria": ["mapping confirmed"]},
                   {"story_type": "dev", "summary": "Add sub()",
                    "acceptance_criteria": ["sub(3,1) == 2"]}]}]}}

TRIAGE = {"analysis": {
    "understood": True, "summary": "no sub", "problem_statement": "add only",
    "risk": "low", "root_cause": "src/app.py defines add but no sub",
    "evidence": [{"file": "src/app.py", "line_start": 1, "line_end": 1,
                  "snippet": "def add(a, b):"}]}}

PLAN = {"plan": {
    "approach": "add sub()", "steps": [{"n": 1, "intent": "add function"}],
    "files_to_change": [{"path": "src/app.py", "action": "modify", "why": "add sub"}],
    "acceptance_criteria": ["sub(3,1) == 2"], "rollback": "revert"}}

IMPL = {"implementation": {"summary": "added sub", "commit_message": "Add sub()",
                           "files_changed": [{"path": "src/app.py"}]}}

REVIEW = {"code_review": {"verdict": "pass", "summary": "fine",
                          "findings": [], "checked": ["logic"]}}

MAPPING = {"mapping": {"summary": "one change", "interface": "calc",
                       "changes": [{"action": "modify", "target": "Calc/Result",
                                    "source": "A", "rule": "copy", "current": "B"}]}}


class ScriptedBob:
    """Stands in for `bob run`: JSON per mode, and files for the build mode."""
    def __init__(self):
        self.calls = []

    def __call__(self, mode, prompt, cwd, budget, timeout_sec=1800, write=False,
                 include_dirs=None, mcp=False):
        self.calls.append({"mode": mode, "write": write, "mcp": mcp, "cwd": cwd})
        payload = {"foreman-decompose": DECOMP, "foreman-triage": TRIAGE,
                   "foreman-plan": PLAN, "foreman-build": IMPL,
                   "foreman-codereview": REVIEW, "foreman-mapping": MAPPING}[mode]
        if mode == "foreman-build":
            with open(os.path.join(cwd, "src", "app.py"), "a") as fh:
                fh.write("\n\ndef sub(a, b):\n    return a - b\n")
            with open(os.path.join(cwd, "tests", "test_sub.py"), "w") as fh:
                fh.write("from src.app import sub\n\ndef test_sub():\n    assert sub(3, 1) == 2\n")
        return BobResult(True, copy.deepcopy(payload), "", "", 0, 0.1, cost=0.01, tool_calls=1)


class FlowCase(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {
            "JIRA_PROJECT": "TAP", "FOREMAN_GIT_MODE": "local", "FOREMAN_AUTO_MERGE": "true",
            "TAPIFORGE_MCP_URL": "", "TAPIFORGE_MCP_COMMAND": "", "FOREMAN_MAPPING_MODE": "",
            "FOREMAN_EPIC_LINK_FIELD": "", "FOREMAN_FEATURE_LINK_FIELD": "",
            "FOREMAN_EPIC_NAME_FIELD": "", "FOREMAN_FEATURE_ISSUETYPE": "Feature"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.repo = make_repo()
        self.work = tempfile.mkdtemp()
        self.board = Board()
        self.bob = ScriptedBob()
        gcfg = gate_mod.GateConfig([APPROVER], "/foreman approve", "/foreman reject",
                                   "/foreman changes", bot_account_id=BOT)
        self.ctx = stages.Context(
            cfg={"jira": {"status_map": {"done": "Done"}}}, jira=self.board,
            store=Store(os.path.join(self.work, "t.db")), run_id="run-flow", live=True,
            paths={"workdir": os.path.join(self.work, "w")}, gate_cfg=gcfg,
            bot_account_id=BOT, repo_source=self.repo, base_branch="main",
            test_command="true", deploy_cfg={"mode": "none"},
            guardrails=guard_mod.Guardrails({}), log_dir=os.path.join(self.work, "logs"))
        self.ctx.bob.run = self.bob

    def step(self, key):
        ticket = stages.load_ticket(self.ctx, self.board.issue_json(key))
        out = stages.advance(self.ctx, ticket)
        self.assertTrue(out.ok, "%s -> %s: %s | %s" % (ticket["fsm"], out.next_state,
                                                       out.reason, out.detail))
        if out.comment.strip():                 # what cli._apply does
            self.board.add_comment(key, out.comment)
        return out

    def walk(self, key, until, limit=30):
        trail = [self.board.state(key)]
        for _ in range(limit):
            if self.board.state(key) == until:
                return trail
            before = self.board.state(key)
            self.step(key)
            after = self.board.state(key)
            if after != before:
                trail.append(after)
            elif machine.get(after).gate:
                return trail                 # parked at a gate
        self.fail("did not reach %s; trail %s" % (until, trail))


class RequirementFlow(FlowCase):
    def test_requirement_to_tracked_stories(self):
        req = self.board.add("Need subtraction", ["foreman", "foreman-requirement"])

        trail = self.walk(req, "awaiting-epic-approval")
        self.assertEqual(trail, ["intake", "decomposing", "awaiting-epic-approval"])
        self.assertEqual(len(self.board.issues), 1, "nothing may be created before Gate 1")
        self.assertEqual(self.bob.calls[-1]["write"], False)

        # Foreman's own comment contains the approve command; it must not count.
        self.step(req)
        self.assertEqual(self.board.state(req), "awaiting-epic-approval")

        self.board.approve(req)
        self.walk(req, "tracking")
        self.assertEqual(self.board.state(req), "tracking")

        made = {k: v for k, v in self.board.issues.items() if k != req}
        types = [v["type"] for v in made.values()]
        self.assertEqual(types.count("Feature"), 1)
        self.assertEqual(types.count("Epic"), 1)
        stories_ = {k: v for k, v in made.items() if v["type"] == "Story"}
        self.assertGreaterEqual(len(stories_), 7)    # mapping dev pipeline cr reg qa pd pv

        def by(t):
            return next(k for k, v in stories_.items() if stories.type_label(t) in v["labels"])
        self.assertIn("foreman", made[by("mapping")]["labels"])
        self.assertIn("foreman-blocked", made[by("dev")]["labels"])
        self.assertNotIn("foreman", made[by("dev")]["labels"])
        for human in ("pipeline", "regression", "qa", "proddeploy", "prodvalidate"):
            self.assertNotIn("foreman", made[by(human)]["labels"], human)

        # Dev is released only when Mapping AND Pipeline are Done.
        self.step(req)
        self.assertIn("foreman-blocked", made[by("dev")]["labels"])
        self.board.issues[by("mapping")]["status"] = "Done"
        self.step(req)
        self.assertIn("foreman-blocked", made[by("dev")]["labels"], "pipeline not done yet")
        self.board.issues[by("pipeline")]["status"] = "Done"
        self.step(req)
        self.assertIn("foreman", made[by("dev")]["labels"])
        self.assertNotIn("foreman-blocked", made[by("dev")]["labels"])

        # The requirement closes only when every story is Done.
        self.step(req)
        self.assertEqual(self.board.state(req), "tracking")
        for k in stories_:
            self.board.issues[k]["status"] = "Done"
        self.step(req)
        self.assertEqual(self.board.state(req), "done")


    def test_changes_sends_the_breakdown_back_without_creating_anything(self):
        req = self.board.add("Need subtraction", ["foreman", "foreman-requirement"])
        self.walk(req, "awaiting-epic-approval")
        self.board.approve(req, "/foreman changes split it per interface")
        self.step(req)
        self.assertEqual(self.board.state(req), "decomposing")
        self.assertEqual(len(self.board.issues), 1)


class DevStoryFlow(FlowCase):
    def test_story_through_every_gate_to_done(self):
        key = self.board.add("Add sub()", ["foreman"])
        sib = self.board.add("code review story", [], status="To Do")
        self.board.issues[key]["state"] = {"siblings": {"codereview": sib},
                                           "requirement": "TAP-0"}

        self.walk(key, "awaiting-approval")
        self.board.approve(key)
        self.walk(key, "awaiting-signoff")

        # Code and its tests were written in one pass and reached the branch.
        branches = subprocess.run(["git", "-C", self.repo, "branch", "--list", "foreman/*"],
                                  capture_output=True, text=True).stdout
        self.assertIn("foreman/%s" % key, branches)
        shown = subprocess.run(["git", "-C", self.repo, "show", "--stat", "foreman/%s" % key],
                               capture_output=True, text=True).stdout
        self.assertIn("src/app.py", shown)
        self.assertIn("tests/test_sub.py", shown)
        self.assertEqual([c["mode"] for c in self.bob.calls],
                         ["foreman-triage", "foreman-plan", "foreman-build",
                          "foreman-codereview"])
        self.assertEqual([c["write"] for c in self.bob.calls], [False, False, True, False])

        # Nothing merges, deploys or closes before the sign-off.
        self.assertFalse(self.board.is_done(sib))
        self.step(key)
        self.assertEqual(self.board.state(key), "awaiting-signoff")

        self.board.approve(key)
        trail = self.walk(key, "done")
        self.assertEqual(trail[-6:], ["awaiting-signoff", "merging", "deploying",
                                      "deployed", "closing", "done"])
        self.assertTrue(self.board.is_done(sib), "Foreman closes the Code Review story")
        self.assertTrue(self.board.is_done(key))
        self.assertFalse([k for k, v in self.board.issues.items()
                          if "prod-deployment" in v["labels"]],
                         "no prod ticket for a requirement story")

    def test_reject_at_signoff_stops_everything(self):
        key = self.board.add("Add sub()", ["foreman"])
        self.walk(key, "awaiting-approval")
        self.board.approve(key)
        self.walk(key, "awaiting-signoff")
        self.board.approve(key, "/foreman reject wrong approach")
        self.step(key)
        self.assertEqual(self.board.state(key), "rejected")
        self.assertFalse(self.board.is_done(key))

    def test_failed_merge_resumes_at_merge_not_at_intake(self):
        key = self.board.add("Add sub()", ["foreman"])
        self.walk(key, "awaiting-approval")
        self.board.approve(key)
        self.walk(key, "awaiting-signoff")
        self.board.approve(key)

        self.step(key)                                  # signoff -> merging
        self.assertEqual(self.board.state(key), "merging")
        real = stages._merge
        with mock.patch.object(stages, "_merge",
                               side_effect=lambda c, t: stages._fail(c, t, "merge", "refused", "x")):
            ticket = stages.load_ticket(self.ctx, self.board.issue_json(key))
            # advance() looks the handler up from a dict built at call time
            out = stages.advance(self.ctx, ticket)
        self.assertEqual(out.next_state, "failed")
        st = self.board.get_state(key)
        self.assertEqual((st["state"], st["failed_from"]), ("failed", "merging"))

        # A human responds; the next run resumes the merge.
        self.board._comment(key, "someone", "fixed branch protection", delta=400)
        self.step(key)
        self.assertEqual(self.board.state(key), "merging")
        self.assertIs(stages._merge, real)


class MappingFlow(FlowCase):
    def test_mapping_story_file_mode(self):
        key = self.board.add("Map fields", ["foreman", stories.type_label("mapping")])
        self.walk(key, "awaiting-mapping-review")
        self.assertEqual(self.bob.calls[-1]["mcp"], False)
        self.assertTrue(any(a.endswith(".csv") for a in self.board.attachments))
        self.assertIn("not checked against TapiForge",
                      self.board.issues[key]["comments"][-1]["body"])

        self.step(key)
        self.assertEqual(self.board.state(key), "awaiting-mapping-review")
        self.board.approve(key)
        trail = self.walk(key, "done")
        self.assertEqual(trail[-4:], ["awaiting-mapping-review", "applying-mapping",
                                      "closing", "done"])
        self.assertTrue(self.board.is_done(key))
        last = self.board.issues[key]["comments"][-1]["body"]
        self.assertIn("Mapping done", last)
        self.assertNotIn("Merged by Foreman", last)
        self.assertFalse([k for k, v in self.board.issues.items()
                          if "prod-deployment" in v["labels"]],
                         "a mapping story has nothing to deploy to production")


if __name__ == "__main__":
    unittest.main()
