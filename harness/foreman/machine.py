"""
The Foreman state machine.

One ticket is in exactly one state. Each state either has a *stage* that
Foreman runs, or it is a *gate* where Foreman stops and waits for a person.
Nothing else moves a ticket. That constraint is the whole design: an agent that
can invent its own next step is an agent nobody can reason about at 2am.

The canonical definition lives here, in Python, not in the YAML. Policy tunes
which Jira status each state mirrors onto and what the labels are called; it
cannot add a transition, remove a gate, or make a stage skip its guard. A
config file that can delete a human approval gate is not a config file, it is a
vulnerability.

State is persisted in the Jira issue property (see `jira.STATE_PROPERTY`), so
the board — not this process — is the source of truth.

A *requirement* (from Jira BR / ServiceNow / email) fans out into stories:

    intake ─triage─▶ analyzed ─▶ decomposing ─▶ ┌─────────────────────────┐
                                                │ AWAITING-EPIC-APPROVAL  │ ◀ Gate 1
                                                └──┬──────────┬───────────┘
                                          approve  │          │ changes → decomposing
                                                   ▼
                          materialising ──▶ tracking ──(all stories done)──▶ done
                          (Feature/Epic/Stories created in Jira)

Each *story* follows the flow for its type (see `stories.py`):

  mapping     intake ─▶ mapping ─▶ ┌──────────────────────────┐ ◀ Gate 2
                          ▲        │ AWAITING-MAPPING-REVIEW  │
                          └changes─┴────────────┬─────────────┘
                                         approve ▼
                                  applying-mapping ─▶ closing ─▶ done

  dev+unit    intake ─triage─▶ analyzed ─plan─▶ awaiting-approval ─▶ approved
              ─build─▶ committed ─code review─▶ ┌────────────────────┐ ◀ Gate 3
                          ▲                      │  AWAITING-SIGNOFF  │
                          └──── reworking ◀──────┴─────────┬──────────┘
                                                    approve ▼
              merging ─▶ deploying ─▶ deployed ─DEV tests─▶ closing ─▶ done
              (PR→Release_DEV)  (pipeline+pods)  (ACE kit)

  pipeline · QA · regression · prod deploy · prod validations are done by
  people. They carry no `foreman` label; the requirement waits in `tracking`
  until Jira says they are Done. The code-review story is closed by Foreman
  once Gate 3 passes.
"""
from typing import Dict, List, Optional

LABEL_PREFIX = "foreman:"

# Sentinel meaning "Foreman has never touched this ticket".
INTAKE = "intake"


class State:
    def __init__(self, name, stage=None, gate=False, terminal=False,
                 human=False, blurb=""):
        self.name = name
        # The stage Foreman runs while the ticket sits in this state. None for
        # gates and terminals — those advance only on an external event.
        self.stage = stage
        self.gate = gate
        self.terminal = terminal
        # `human` states need a person to do something before anything moves,
        # but unlike a gate there is no decision to record — the ticket simply
        # re-enters intake once it is edited.
        self.human = human
        self.blurb = blurb

    @property
    def label(self) -> str:
        return LABEL_PREFIX + self.name

    def __repr__(self):
        return "<State %s>" % self.name


STATES: Dict[str, State] = {s.name: s for s in [
    State(INTAKE, stage="triage",
          blurb="picked up from the intake query; not yet analysed"),
    State("analyzed", stage="plan",
          blurb="root cause understood; producing a change plan"),
    State("awaiting-approval", gate=True,
          blurb="analysis and plan posted; waiting for a human decision"),
    State("revising", stage="plan",
          blurb="reviewer asked for changes; re-planning with their feedback"),
    State("approved", stage="build",
          blurb="plan approved; writing the code and its tests, opening the PR"),
    State("committed", stage="codereview",
          blurb="change pushed and PR open; AI self-review of the diff"),
    State("awaiting-signoff", gate=True,
          blurb="code and review posted; waiting for the human sign-off (Gate 3)"),
    State("reworking", stage="build",
          blurb="sign-off returned with feedback; reworking the change"),
    State("merging", stage="merge",
          blurb="signed off; merging the PR into the DEV release branch"),
    State("deploying", stage="deploy",
          blurb="merged; the pipeline is deploying and the pods are being restarted"),
    State("deployed", stage="devtest",
          blurb="running on DEV; executing the test suite against it"),
    State("closing", stage="close",
          blurb="work complete; closing the Jira ticket"),
    State("done", terminal=True,
          blurb="complete"),
    State("needs-info", human=True,
          blurb="the ticket does not contain enough to act on; a human must add detail"),
    State("rejected", terminal=True,
          blurb="a reviewer rejected the proposal; Foreman will not act further"),
    State("failed", human=True,
          blurb="a stage failed in a way Foreman will not retry on its own; needs a human"),
    State("stalled", human=True,
          blurb="waiting at a gate for longer than the approval timeout"),
    # ── AI-SDLC: requirement → Feature / Epic / Stories ─────────────────────
    State("decomposing", stage="decompose",
          blurb="drafting the Feature, Epics and Stories for this requirement"),
    State("awaiting-epic-approval", gate=True,
          blurb="decomposition posted; waiting for human sign-off before anything is created (Gate 1)"),
    State("materialising", stage="materialise",
          blurb="approved; creating the Feature, Epics and Stories in Jira"),
    State("tracking", stage="track",
          blurb="stories created; waiting for every story to be done"),
    # ── AI-SDLC: mapping stories (TapiForge) ────────────────────────────────
    State("mapping", stage="mapping",
          blurb="reading the current mappings and drafting the changes"),
    State("awaiting-mapping-review", gate=True,
          blurb="mapping changes posted; waiting for a human to confirm them (Gate 2)"),
    State("applying-mapping", stage="mapping-apply",
          blurb="mappings confirmed; applying them (or handing the diff to a person)"),
]}

ALL_LABELS = [s.label for s in STATES.values()]

# Every legal move. Anything not listed here cannot happen, and the engine
# raises rather than guessing. Exhaustiveness is the point: a reader can audit
# the full set of ways a ticket can reach `done` by reading one dictionary.
# Stages that are safe to simply run again once a human has fixed whatever made
# them fail (credentials, a refused merge, a broken pipeline). Anything not in
# this set restarts from intake instead, because its premises may have changed.
RESUMABLE = ["materialising", "tracking", "applying-mapping", "committed", "merging",
             "deploying", "deployed", "closing"]

TRANSITIONS: Dict[str, List[str]] = {
    INTAKE:              ["analyzed", "decomposing", "mapping", "needs-info", "failed"],
    "analyzed":          ["awaiting-approval", "needs-info", "failed"],
    "decomposing":       ["awaiting-epic-approval", "needs-info", "failed"],
    "awaiting-epic-approval": ["materialising", "decomposing", "rejected",
                               "stalled", "failed"],
    "materialising":     ["tracking", "failed"],
    "tracking":          ["done", "failed"],
    "awaiting-approval": ["approved", "revising", "rejected", "stalled", "failed"],
    "revising":          ["awaiting-approval", "needs-info", "failed"],
    "approved":          ["committed", "failed"],
    "committed":         ["awaiting-signoff", "failed"],
    "awaiting-signoff":  ["merging", "reworking", "rejected", "stalled", "failed"],
    "reworking":         ["committed", "failed"],
    "merging":           ["deploying", "failed"],
    "deploying":         ["deployed", "failed"],
    "deployed":          ["closing", "failed"],
    "closing":           ["done", "failed"],
    "mapping":           ["awaiting-mapping-review", "needs-info", "failed"],
    "awaiting-mapping-review": ["applying-mapping", "mapping", "rejected",
                                "stalled", "failed"],
    "applying-mapping":  ["closing", "failed"],
    "done":              [],
    "rejected":          [],
    # Recovery paths. A human who adds the missing detail, or fixes whatever
    # broke, puts the ticket back at the top rather than mid-flight — Foreman
    # must never resume from a state whose premises a human has since changed.
    # The one exception is RESUMABLE: stages that only ever *do* something
    # already decided, so running them again is exactly what was asked for.
    "needs-info":        [INTAKE, "rejected"],
    "failed":            [INTAKE, "rejected"] + RESUMABLE,
    "stalled":           ["approved", "revising", "rejected", INTAKE],
}

# Which gate each gate-state is, and what a decision there means.
GATES = {
    "awaiting-epic-approval": {
        "question": "Approve this Feature / Epic / Story decomposition?",
        "approve": "materialising",
        "changes": "decomposing",
        "reject": "rejected",
    },
    "awaiting-mapping-review": {
        "question": "Confirm these mapping changes?",
        "approve": "applying-mapping",
        "changes": "mapping",
        "reject": "rejected",
    },
    "awaiting-approval": {
        "question": "Approve this analysis and plan?",
        "approve": "approved",
        "changes": "revising",
        "reject": "rejected",
    },
    "awaiting-signoff": {
        "question": "Sign off this change and its code review (then merge to DEV)?",
        "approve": "merging",
        "changes": "reworking",
        "reject": "rejected",
    },
}


class IllegalTransition(Exception):
    def __init__(self, frm, to):
        super().__init__(
            "illegal transition %s -> %s. Legal from %s: %s"
            % (frm, to, frm, ", ".join(TRANSITIONS.get(frm, [])) or "(none)"))
        self.frm = frm
        self.to = to


def get(name: Optional[str]) -> State:
    return STATES.get((name or INTAKE), STATES[INTAKE])


def can(frm: str, to: str) -> bool:
    return to in TRANSITIONS.get(frm or INTAKE, [])


def check(frm: str, to: str) -> None:
    if not can(frm, to):
        raise IllegalTransition(frm or INTAKE, to)


def is_gate(name: str) -> bool:
    return get(name).gate


def stage_for(name: str) -> Optional[str]:
    return get(name).stage


def labels_to_clear(keep: str) -> List[str]:
    """Every Foreman label except the one the ticket should now carry.

    Removing the others explicitly, rather than trusting the previous state to
    have cleaned up, means a ticket that was hand-edited on the board converges
    back to one label instead of accumulating a history of them.
    """
    return [l for l in ALL_LABELS if l != keep]


def describe() -> str:
    """Human-readable map of the machine. Printed by `foreman states`."""
    rows = ["%-25s %-9s %-14s %s" % ("STATE", "KIND", "STAGE", "MEANING"),
            "%-25s %-9s %-14s %s" % ("-" * 25, "-" * 9, "-" * 14, "-" * 44)]
    for name, s in STATES.items():
        kind = ("GATE" if s.gate else "terminal" if s.terminal
                else "human" if s.human else "auto")
        rows.append("%-25s %-9s %-14s %s" % (name, kind, s.stage or "-", s.blurb))
    return "\n".join(rows)
