"""Output contracts. The harness requires the evidence the prompt merely asks for."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from foreman import contract


def repo_with(rel, content):
    root = tempfile.mkdtemp()
    full = os.path.join(root, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w") as fh:
        fh.write(content)
    return root


SOURCE = "def f():\n    x = 1\n    return x\n"


def analysis(**over):
    a = {"understood": True, "summary": "s", "problem_statement": "p",
         "root_cause": "r", "risk": "medium",
         "evidence": [{"file": "src/a.py", "line_start": 2, "line_end": 3,
                       "snippet": "x = 1\n    return x", "why": "here"}]}
    a.update(over)
    return {"analysis": a}


class Evidence(unittest.TestCase):
    def setUp(self):
        self.root = repo_with("src/a.py", SOURCE)

    def test_a_true_citation_is_accepted(self):
        v = contract.validate_triage(analysis(), self.root)
        self.assertTrue(v.data["evidence"][0]["evidence_verified"])

    def test_wrong_line_numbers_are_corrected_not_rejected(self):
        # Models quote real code reliably and count lines unreliably. Throwing
        # away a true finding over an off-by-two throws away the work.
        v = contract.validate_triage(
            analysis(evidence=[{"file": "src/a.py", "line_start": 40,
                                "line_end": 41, "snippet": "return x",
                                "why": "here"}]), self.root)
        self.assertEqual(v.data["evidence"][0]["line_start"], 3)
        self.assertTrue(v.corrections)

    def test_fabricated_code_is_deleted(self):
        with self.assertRaises(contract.Rejection) as e:
            contract.validate_triage(
                analysis(evidence=[{"file": "src/a.py", "line_start": 1,
                                    "line_end": 2,
                                    "snippet": "launch_the_missiles()",
                                    "why": "invented"}]), self.root)
        self.assertEqual(e.exception.code, "no_verified_evidence")

    def test_path_traversal_is_refused(self):
        with self.assertRaises(contract.Rejection):
            contract.validate_triage(
                analysis(evidence=[{"file": "../../../etc/passwd",
                                    "line_start": 1, "line_end": 1,
                                    "snippet": "root", "why": "x"}]), self.root)

    def test_not_understood_needs_questions(self):
        with self.assertRaises(contract.Rejection):
            contract.validate_triage(
                analysis(understood=False, clarifications_needed=[]), self.root)

    def test_not_understood_with_questions_needs_no_evidence(self):
        v = contract.validate_triage(
            analysis(understood=False, evidence=[],
                     clarifications_needed=["which endpoint?"]), self.root)
        self.assertFalse(v.data["understood"])


class Plan(unittest.TestCase):
    def base(self, **over):
        p = {"approach": "a", "steps": [{"n": 1, "intent": "do"}],
             "files_to_change": [{"path": "src/a.py", "action": "modify"}],
             "acceptance_criteria": ["it works"]}
        p.update(over)
        return {"plan": p}

    def test_valid(self):
        self.assertEqual(len(contract.validate_plan(self.base()).data["steps"]), 1)

    def test_acceptance_criteria_are_mandatory(self):
        # Without them the sign-off gate has nothing to judge.
        with self.assertRaises(contract.Rejection) as e:
            contract.validate_plan(self.base(acceptance_criteria=[]))
        self.assertEqual(e.exception.code, "no_acceptance_criteria")

    def test_an_absolute_path_is_rejected(self):
        with self.assertRaises(contract.Rejection):
            contract.validate_plan(
                self.base(files_to_change=[{"path": "/etc/hosts",
                                            "action": "modify"}]))

    def test_an_unknown_action_is_rejected(self):
        with self.assertRaises(contract.Rejection):
            contract.validate_plan(
                self.base(files_to_change=[{"path": "a.py",
                                            "action": "obliterate"}]))


class Verification(unittest.TestCase):
    def test_no_pass_fail_field_is_carried_through(self):
        # Whether the suite passes is established by running it. A model-
        # reported result would be indistinguishable from a real one.
        v = contract.validate_verification({"verification": {
            "summary": "s", "passed": True, "all_tests_pass": True,
            "tests_added": [{"path": "tests/test_a.py"}]}})
        self.assertNotIn("passed", v.data)
        self.assertNotIn("all_tests_pass", v.data)

    def test_a_stage_that_added_no_tests_is_rejected(self):
        with self.assertRaises(contract.Rejection):
            contract.validate_verification(
                {"verification": {"summary": "s", "tests_added": []}})


class Review(unittest.TestCase):
    def test_a_rejection_must_be_reasoned(self):
        with self.assertRaises(contract.Rejection):
            contract.validate_review({"review": {"verdict": "reject"}})

    def test_accept_needs_no_reasons(self):
        self.assertEqual(
            contract.validate_review({"review": {"verdict": "accept"}}).data["verdict"],
            "accept")


class Implementation(unittest.TestCase):
    def test_a_long_subject_is_truncated_not_rejected(self):
        v = contract.validate_implementation({"implementation": {
            "summary": "s", "commit_message": "x" * 200}})
        self.assertLessEqual(len(v.data["commit_subject"]), 72)
        self.assertTrue(v.corrections)

    def test_body_is_split_from_subject(self):
        v = contract.validate_implementation({"implementation": {
            "summary": "s", "commit_message": "fix: thing\n\nthe body"}})
        self.assertEqual(v.data["commit_subject"], "fix: thing")
        self.assertIn("the body", v.data["commit_body"])


if __name__ == "__main__":
    unittest.main()
