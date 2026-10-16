# Scope and the change budget

## The plan is a contract

A human read the plan and approved it. That approval is specific: it covers the
files named in `files_to_change`, the approach described in `approach`, and
nothing else.

The build stage may change **only** the files the plan names. The harness
compares the diff against the plan and reverts anything outside it, in full.
Not the offending file — the whole change. A partial revert leaves a
half-applied change on a branch, which is worse than none.

## The budget

Defaults, set in `policy/guardrails.yaml`:

| Limit | Default | Why it exists |
|---|---|---|
| Files changed | 15 | A change ten times the size of the plan is not the change that was approved |
| Lines changed | 600 | The reviewer at the sign-off gate has to read this |
| Lines in one file | 400 | A rewrite disguised as a fix |
| Lines deleted | 200 | Deleting code is how "make the problem go away" differs from "fix it" |

Exceeding any of them is blocking. If the fix genuinely needs more, say so in
the plan — a reviewer can raise the budget for a ticket. Writing the change
first and asking afterwards wastes everyone's time, including yours.

## Paths nothing can authorise

These are denied regardless of the plan, the approval, or the ticket:

- `.github/workflows/`, `.gitlab-ci.yml`, `Jenkinsfile` — the pipeline that
  gates you
- `.bob/`, `.foreman/`, `AGENTS.md` — your own modes, rules and guardrails
- `.env`, `*.pem`, `*.key` — credentials
- Dependency manifests and lockfiles — adding a dependency is a supply-chain
  decision with a different approver
- Infrastructure and deployment manifests — a different blast radius and a
  different reviewer

The pattern: **you may not edit the things that constrain you.** An agent that
can rewrite its own guardrails does not have guardrails.

## Existing tests

You may not modify or delete an existing test. Not in the build stage, not in
the verify stage, not because it is "clearly wrong", not because it is
"testing the old behaviour".

If a test fails against your change, one of two things is true and both are
useful:

1. Your change is wrong. Fix the change.
2. The test encodes behaviour that was deliberately changed. Say so, in
   `deviations`, and stop. A human decides whether a behavioural contract
   should move; that is precisely the kind of decision this pipeline routes to
   a person.

Adding a new test is always fine and is the point of the verify stage.

## Smaller is the goal

Every file in the plan is a file a reviewer must read and a file that can
regress. In a change that is written semi-automatically and reviewed under time
pressure, breadth is not thoroughness. It is risk.

If you see three problems, fix the one in the ticket and list the other two in
`out_of_scope`. They become their own tickets, with their own approvals, and
each gets the attention it deserves. Bundling them means the reviewer approves
one thing and receives three.
