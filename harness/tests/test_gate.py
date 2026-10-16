"""The approval gate. Each test here corresponds to a way an approval can be forged."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from foreman.gate import GateConfig, evaluate, parse_command, parse_ts

APPROVER = "acct-approver"
BOT = "acct-foreman"


def cfg(**kw):
    return GateConfig(approvers=kw.pop("approvers", [APPROVER, "lead@example.com"]),
                      cmd_approve="/foreman approve", cmd_reject="/foreman reject",
                      cmd_changes="/foreman changes", bot_account_id=BOT,
                      timeout_hours=kw.pop("timeout_hours", 72))


def comment(cid, text, account, when, email="", name="Someone"):
    return {"id": str(cid), "created": when,
            "author": {"accountId": account, "displayName": name,
                       "emailAddress": email},
            "body": {"type": "doc", "version": 1, "content": [
                {"type": "paragraph",
                 "content": [{"type": "text", "text": text}]}]}}


OPENED = parse_ts("2026-09-20T10:00:00.000+0000")
NOW = parse_ts("2026-09-20T13:00:00.000+0000")


class CommandShape(unittest.TestCase):
    def test_command_must_open_the_comment(self):
        c = cfg()
        self.assertIsNone(parse_command("I think we should /foreman approve", c))
        self.assertIsNone(parse_command("> /foreman approve  (quoting Bob)", c))
        self.assertEqual(parse_command("/foreman approve", c)[0], "approve")

    def test_feedback_is_captured(self):
        verdict, feedback = parse_command(
            "/foreman changes please add a regression test for LOYAL5", cfg())
        self.assertEqual(verdict, "changes")
        self.assertIn("LOYAL5", feedback)

    def test_prefix_collision_is_not_a_match(self):
        # "/foreman approved-by-jane" must not read as "/foreman approve".
        self.assertIsNone(parse_command("/foreman approved-by-jane", cfg()))

    def test_case_and_leading_space_are_tolerated(self):
        self.assertEqual(parse_command("  /FOREMAN Approve", cfg())[0], "approve")


class Identity(unittest.TestCase):
    def test_stranger_cannot_approve(self):
        r = evaluate([comment(1, "/foreman approve", "acct-mallory",
                              "2026-09-20T11:00:00.000+0000")],
                     cfg(), OPENED, NOW)
        self.assertIsNone(r.decision)
        self.assertEqual(r.attempts[0].reason, "not_authorised")

    def test_approver_by_account_id(self):
        r = evaluate([comment(1, "/foreman approve", APPROVER,
                              "2026-09-20T11:00:00.000+0000")],
                     cfg(), OPENED, NOW)
        self.assertEqual(r.decision.verdict, "approve")

    def test_approver_by_email_is_case_insensitive(self):
        r = evaluate([comment(1, "/foreman approve", "acct-x",
                              "2026-09-20T11:00:00.000+0000",
                              email="LEAD@Example.com")],
                     cfg(), OPENED, NOW)
        self.assertEqual(r.decision.verdict, "approve")

    def test_foremans_own_comment_cannot_approve(self):
        # The rendered plan tells the reviewer to type "/foreman approve", so
        # it necessarily contains the command. Reading it back would be a
        # self-approval channel. Foreman's comments are identified by the
        # marker they carry, not by the account that posted them.
        from foreman.render import marker
        own = "/foreman approve is how you approve\n\n" + marker("analysis")
        r = evaluate([comment(1, own, APPROVER,
                              "2026-09-20T11:00:00.000+0000")],
                     cfg(), OPENED, NOW)
        self.assertIsNone(r.decision)
        self.assertEqual(r.attempts[0].reason, "self")

    def test_a_human_on_the_bots_own_account_can_still_approve(self):
        # Small teams run Foreman under an account a human also uses. Keying
        # the self-check on the account id would silently discard every
        # approval they ever make, with no error anywhere.
        c = cfg(approvers=[BOT])
        r = evaluate([comment(1, "/foreman approve", BOT,
                              "2026-09-20T11:00:00.000+0000")], c, OPENED, NOW)
        self.assertIsNotNone(r.decision)
        self.assertEqual(r.decision.verdict, "approve")
        # ...but doctor should still tell them to separate the accounts,
        # because the ticket history cannot show who approved.
        self.assertTrue(c.bot_is_an_approver)

    def test_empty_allowlist_authorises_nobody(self):
        c = cfg(approvers=[])
        self.assertFalse(c.configured)
        r = evaluate([comment(1, "/foreman approve", APPROVER,
                              "2026-09-20T11:00:00.000+0000")], c, OPENED, NOW)
        self.assertIsNone(r.decision)


class Recency(unittest.TestCase):
    def test_approval_predating_the_gate_is_stale(self):
        # The failure this prevents: an approve from revision one silently
        # authorising revision three, which the reviewer never saw.
        r = evaluate([comment(1, "/foreman approve", APPROVER,
                              "2026-09-20T09:00:00.000+0000")],
                     cfg(), OPENED, NOW)
        self.assertIsNone(r.decision)
        self.assertEqual(r.attempts[0].reason, "stale")

    def test_latest_valid_decision_wins(self):
        r = evaluate([
            comment(1, "/foreman approve", APPROVER, "2026-09-20T11:00:00.000+0000"),
            comment(2, "/foreman changes on reflection, no", APPROVER,
                    "2026-09-20T12:00:00.000+0000"),
        ], cfg(), OPENED, NOW)
        self.assertEqual(r.decision.verdict, "changes")

    def test_timeout_is_reported_never_auto_approved(self):
        r = evaluate([], cfg(timeout_hours=1), OPENED, NOW)
        self.assertTrue(r.timed_out)
        self.assertIsNone(r.decision)


class Timestamps(unittest.TestCase):
    def test_jira_offset_without_colon(self):
        # datetime.fromisoformat rejects "+0200" before Python 3.11.
        ts = parse_ts("2026-09-20T12:00:00.000+0200")
        self.assertEqual(ts.hour, 10)

    def test_garbage_is_none_not_an_exception(self):
        self.assertIsNone(parse_ts("not a date"))
        self.assertIsNone(parse_ts(None))


if __name__ == "__main__":
    unittest.main()
