"""How Foreman writes to the ticket. Each test here is a way the ticket's history can be lost or flooded."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from foreman import cli, render, stages
from foreman.gate import GateConfig

APPROVER = "acct-approver"
STRANGER = "acct-stranger"


class FakeJira:
    """Records writes; holds comments oldest-first, as Jira returns them."""

    def __init__(self, comments=None):
        self.store = list(comments or [])
        self.added, self.updated = [], []

    def comments(self, key, limit=200, newest_first=False):
        return list(reversed(self.store)) if newest_first else list(self.store)

    def text_of(self, body):
        return body if isinstance(body, str) else ""

    def find_marked_comment(self, key, marker):
        from foreman.jira import JiraClient
        return JiraClient.find_marked_comment(self, key, marker)

    def add_comment(self, key, body):
        self.added.append(body)
        self.store.append({"id": str(len(self.store) + 1), "body": body})

    def update_comment(self, key, cid, body):
        self.updated.append((cid, body))


def ctx_with(jira):
    gate_cfg = GateConfig(approvers=[APPROVER], cmd_approve="/foreman approve",
                          cmd_reject="/foreman reject",
                          cmd_changes="/foreman changes")
    return mock.Mock(jira=jira, live=False, run_id="t", gate_cfg=gate_cfg)


def analysis(rev):
    return "Plan revision %d\n\n%s" % (rev, render.marker("analysis"))


class CommentMode(unittest.TestCase):
    def apply(self, mode, jira):
        out = stages.Outcome("awaiting-approval", "x", comment=analysis(2),
                             sticky="analysis")
        with mock.patch.dict(os.environ, {"FOREMAN_COMMENT_MODE": mode}):
            cli._apply(ctx_with(jira), {"key": "FORE-1"}, out, moved=False)

    def test_append_is_the_default_and_keeps_the_earlier_plan(self):
        jira = FakeJira([{"id": "1", "body": analysis(1)}])
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FOREMAN_COMMENT_MODE", None)
            self.assertEqual(cli.comment_mode(), ("append", True))
        self.apply("append", jira)
        self.assertEqual(len(jira.added), 1)
        self.assertEqual(jira.updated, [])

    def test_update_rewrites_in_place(self):
        jira = FakeJira([{"id": "1", "body": analysis(1)}])
        self.apply("update", jira)
        self.assertEqual(jira.added, [])
        self.assertEqual([cid for cid, _ in jira.updated], ["1"])

    def test_update_after_append_targets_the_latest_revision(self):
        # A board that ran in append mode and was switched to update must not
        # rewrite the plan from two revisions ago.
        jira = FakeJira([{"id": "1", "body": analysis(0)},
                         {"id": "2", "body": analysis(1)}])
        self.apply("update", jira)
        self.assertEqual([cid for cid, _ in jira.updated], ["2"])

    def test_unknown_mode_falls_back_to_append_and_is_flagged(self):
        with mock.patch.dict(os.environ, {"FOREMAN_COMMENT_MODE": "overwrite"}):
            self.assertEqual(cli.comment_mode(), ("append", False))


def cmd(cid, text, account, when):
    return {"id": cid, "created": when,
            "author": {"accountId": account, "displayName": account},
            "body": {"type": "doc", "version": 1, "content": [
                {"type": "paragraph",
                 "content": [{"type": "text", "text": text}]}]}}


class RefusedAttemptNote(unittest.TestCase):
    """The gate re-reads the ticket every run; a refusal is news only once."""

    def ticket(self, comments, **state):
        state.setdefault("gate_opened", "2026-09-20T10:00:00.000+0000")
        return {"key": "FORE-1", "fsm": "awaiting-approval", "revision": 1,
                "comments": comments, "state": state}

    def test_a_refused_attempt_is_reported_once_not_every_run(self):
        t = self.ticket([cmd("10", "/foreman approve", STRANGER,
                             "2026-09-20T11:00:00.000+0000")])
        ctx = ctx_with(FakeJira())
        first = stages._gate(ctx, t)
        second = stages._gate(ctx, t)
        self.assertIn("not honoured", first.comment)
        self.assertEqual(second.comment, "")

    def test_a_new_attempt_is_still_reported(self):
        t = self.ticket([cmd("10", "/foreman approve", STRANGER,
                             "2026-09-20T11:00:00.000+0000")])
        ctx = ctx_with(FakeJira())
        stages._gate(ctx, t)
        t["comments"].append(cmd("11", "/foreman approve", STRANGER,
                                 "2026-09-20T12:00:00.000+0000"))
        again = stages._gate(ctx, t)
        self.assertIn("1 comment(s)", again.comment)

    def test_the_decision_that_started_this_revision_is_not_a_refusal(self):
        # The reviewer's `/foreman changes` produced this revision. When the
        # revised plan reopens the gate it is "stale", but it was honoured.
        t = self.ticket([cmd("10", "/foreman changes add a test", APPROVER,
                             "2026-09-20T09:00:00.000+0000")],
                        decision={"verdict": "changes", "comment_id": "10"})
        out = stages._gate(ctx_with(FakeJira()), t)
        self.assertEqual(out.comment, "")


if __name__ == "__main__":
    unittest.main()
