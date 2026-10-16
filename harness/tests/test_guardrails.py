"""Guardrails. Each test is a way an autonomous build stage goes wrong."""
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from foreman.guardrails import (Guardrails, collect_diff, inspect, revert,
                                scan_patch_for_secrets)


class Sandbox:
    def __init__(self):
        self.path = tempfile.mkdtemp()
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.email", "t@t.invalid")
        self._git("config", "user.name", "t")
        self.write("src/app.py", "def f():\n    return 1\n")
        self.write("tests/test_app.py", "def test_f():\n    assert True\n")
        self.write(".github/workflows/ci.yml", "on: push\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "base")

    def _git(self, *a):
        return subprocess.run(["git", "-C", self.path] + list(a),
                              capture_output=True, text=True)

    def write(self, rel, content):
        full = os.path.join(self.path, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as fh:
            fh.write(content)

    def report(self, rules=None, planned=None):
        return inspect(collect_diff(self.path),
                       rules or Guardrails({"allow_paths": ["src/*", "tests/*"]}),
                       planned)


def codes(report):
    return {v.code for v in report.violations if v.blocking}


class TestGuardrails(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()

    def test_a_clean_planned_change_passes(self):
        self.s.write("src/app.py", "def f():\n    return 2\n")
        r = self.s.report(planned=["src/app.py"])
        self.assertFalse(r.blocked, codes(r))

    def test_editing_an_existing_test_is_blocked(self):
        # The cheapest way to make a failing test pass is to change the test.
        self.s.write("tests/test_app.py", "def test_f():\n    pass\n")
        r = self.s.report(planned=["src/app.py"])
        self.assertIn("test_modified", codes(r))

    def test_deleting_an_existing_test_is_blocked(self):
        os.remove(os.path.join(self.s.path, "tests/test_app.py"))
        r = self.s.report(planned=["src/app.py"])
        self.assertIn("test_modified", codes(r))

    def test_ci_configuration_is_denied_even_when_planned(self):
        # An agent that can edit the pipeline that gates it has no gate.
        self.s.write(".github/workflows/ci.yml", "on: []\n")
        r = self.s.report(planned=[".github/workflows/ci.yml"])
        self.assertIn("denied_path", codes(r))

    def test_files_outside_the_plan_are_blocked(self):
        self.s.write("src/other.py", "x = 1\n")
        r = self.s.report(planned=["src/app.py"])
        self.assertIn("not_in_plan", codes(r))

    def test_a_new_test_file_outside_the_plan_is_allowed(self):
        # The verify stage exists to add tests.
        self.s.write("tests/test_new.py", "def test_new():\n    assert True\n")
        r = self.s.report(planned=["src/app.py"])
        blocking = [v for v in r.violations if v.blocking and
                    v.path == "tests/test_new.py"]
        self.assertEqual(blocking, [])

    def test_secrets_in_added_lines_block(self):
        self.s.write("src/app.py",
                     'TOKEN = "ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"\n')
        r = self.s.report(planned=["src/app.py"])
        self.assertIn("secret_in_diff", codes(r))

    def test_removing_a_secret_is_not_flagged(self):
        # Scanning removed lines would flag the very commit that fixes the leak.
        self.s.write("src/leak.py", 'K = "AKIAAAAAAAAAAAAAAAAA"\n')
        self.s._git("add", "-A")
        self.s._git("commit", "-qm", "leak")
        self.s.write("src/leak.py", "K = read_from_vault()\n")
        r = inspect(collect_diff(self.s.path),
                    Guardrails({"allow_paths": ["src/*"]}), ["src/leak.py"])
        self.assertNotIn("secret_in_diff", codes(r))

    def test_the_file_budget_is_enforced(self):
        for n in range(6):
            self.s.write("src/f%d.py" % n, "x = %d\n" % n)
        r = self.s.report(rules=Guardrails({"allow_paths": ["src/*"],
                                            "max_files": 3,
                                            "require_plan_adherence": False}))
        self.assertIn("too_many_files", codes(r))

    def test_the_deletion_budget_is_tighter(self):
        self.s.write("src/big.py", "\n".join("line %d" % i for i in range(300)))
        self.s._git("add", "-A")
        self.s._git("commit", "-qm", "big")
        self.s.write("src/big.py", "gone\n")
        r = inspect(collect_diff(self.s.path),
                    Guardrails({"allow_paths": ["src/*"], "max_deleted_lines": 50,
                                "require_plan_adherence": False}))
        self.assertIn("too_much_deleted", codes(r))

    def test_no_changes_at_all_is_a_violation(self):
        # A build stage that produced nothing has failed, not succeeded.
        r = self.s.report(planned=["src/app.py"])
        self.assertIn("empty_diff", codes(r))

    def test_revert_restores_the_worktree(self):
        self.s.write("src/app.py", "broken\n")
        self.s.write("src/new.py", "junk\n")
        self.assertTrue(revert(self.s.path))
        self.assertEqual(collect_diff(self.s.path).files, [])
        self.assertFalse(os.path.exists(os.path.join(self.s.path, "src/new.py")))


class TestSecretScan(unittest.TestCase):
    def test_known_shapes(self):
        patch = "\n".join([
            "+++ b/a.py",
            '+ATLASSIAN = "ATATT3xFfGF0aaaaaaaaaaaaaaaaaaaaaaaaa"',
            '+BOB = "bob_prod_bob-admin_aaaaaaaaaaaaaaaaaaaaaaaa"',
            "+-----BEGIN RSA PRIVATE KEY-----",
        ])
        found = {v.detail.split(" detected")[0]
                 for v in scan_patch_for_secrets(patch)}
        self.assertTrue({"Atlassian API token", "Bob Shell API key",
                         "private key"} <= found, found)


if __name__ == "__main__":
    unittest.main()
