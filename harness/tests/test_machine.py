"""The state machine. Its value is that the set of legal moves is small and closed."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from foreman import machine


class Transitions(unittest.TestCase):
    def test_every_target_is_a_real_state(self):
        for frm, targets in machine.TRANSITIONS.items():
            self.assertIn(frm, machine.STATES, frm)
            for to in targets:
                self.assertIn(to, machine.STATES, "%s -> %s" % (frm, to))

    def test_every_state_has_a_transition_entry(self):
        for name in machine.STATES:
            self.assertIn(name, machine.TRANSITIONS, name)

    def test_a_gate_cannot_be_skipped(self):
        # The whole point. There is no path from analysis to code that does
        # not pass through a human.
        self.assertFalse(machine.can("analyzed", "approved"))
        self.assertFalse(machine.can("analyzed", "committed"))
        # Merge and deploy are only reachable through the sign-off gate.
        self.assertFalse(machine.can("committed", "merging"))
        self.assertFalse(machine.can("awaiting-signoff", "deploying"))
        self.assertFalse(machine.can(machine.INTAKE, "done"))

    def test_the_only_route_to_done_is_through_both_gates(self):
        route = [machine.INTAKE, "analyzed", "awaiting-approval", "approved",
                 "committed", "awaiting-signoff", "merging", "deploying",
                 "deployed", "closing", "done"]
        for a, b in zip(route, route[1:]):
            self.assertTrue(machine.can(a, b), "%s -> %s" % (a, b))
        self.assertEqual(
            [s for s in route if machine.get(s).gate],
            ["awaiting-approval", "awaiting-signoff"])

    def test_illegal_transitions_raise(self):
        with self.assertRaises(machine.IllegalTransition):
            machine.check("done", machine.INTAKE)

    def test_terminals_have_no_exits(self):
        for name in ("done", "rejected"):
            self.assertTrue(machine.get(name).terminal)
            self.assertEqual(machine.TRANSITIONS[name], [])

    def test_human_holds_return_to_intake_not_mid_flight(self):
        # Resuming mid-flight would continue from premises a human has since
        # changed.
        for name in ("needs-info", "failed"):
            self.assertIn(machine.INTAKE, machine.TRANSITIONS[name])


class Labels(unittest.TestCase):
    def test_labels_are_prefixed_and_unique(self):
        labels = [s.label for s in machine.STATES.values()]
        self.assertEqual(len(labels), len(set(labels)))
        self.assertTrue(all(l.startswith(machine.LABEL_PREFIX) for l in labels))

    def test_clearing_leaves_exactly_one(self):
        keep = machine.get("approved").label
        self.assertNotIn(keep, machine.labels_to_clear(keep))
        self.assertEqual(len(machine.labels_to_clear(keep)),
                         len(machine.ALL_LABELS) - 1)

    def test_jira_label_length_is_safe(self):
        # Jira rejects labels over 255 characters and labels containing spaces.
        for l in machine.ALL_LABELS:
            self.assertLess(len(l), 255)
            self.assertNotIn(" ", l)


class Gates(unittest.TestCase):
    def test_every_gate_state_has_a_spec(self):
        gate_states = {n for n, s in machine.STATES.items() if s.gate}
        self.assertEqual(gate_states, set(machine.GATES))

    def test_gate_outcomes_are_legal_transitions(self):
        for name, spec in machine.GATES.items():
            for verdict in ("approve", "changes", "reject"):
                self.assertTrue(machine.can(name, spec[verdict]),
                                "%s %s -> %s" % (name, verdict, spec[verdict]))


if __name__ == "__main__":
    unittest.main()
