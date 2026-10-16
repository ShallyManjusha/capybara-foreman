# Architecture

> There is an illustrated version of this document —
> **[`ai-sdlc-foreman-architecture.html`](ai-sdlc-foreman-architecture.html)** —
> with eleven drawings of the trust boundaries, the state machine, gate
> evaluation and the diff inspection. Open it in a browser; it is a single
> self-contained file. This markdown version is the one to read in a terminal
> or diff in a pull request.

## The shape of the problem

A delivery pipeline driven by an agent has three jobs, and they have very
different risk profiles.

| | What it is | What happens when it is wrong |
|---|---|---|
| **Reasoning** | read the ticket, read the code, find the defect, write the fix | a bad change, caught by review |
| **Judgement** | is this the right fix? is it good enough to ship? | the wrong thing ships, confidently |
| **Mechanics** | clone, branch, commit, push, dispatch, poll, close | a stuck ticket |

Language models are good at the first, unreliable at the second, and wasteful
at the third. Foreman assigns each job to whatever handles it best: Bob Shell
reasons, humans judge, Python does the mechanics. The interesting engineering
is in the seams between them.

## Tiers

```
┌─ ANALYSIS TIER ──────────────────────────────────────────────────┐
│ credentials: BOB_API_KEY only                                     │
│ tools:       read-only (mode `groups: [read]`)                    │
│ input:       a read-only clone + fenced ticket text               │
│ output:      one JSON envelope                                    │
│                                                                    │
│   foreman-triage → foreman-plan → foreman-critic                  │
└───────────────────────────────┬──────────────────────────────────┘
                                │  validated JSON only
┌─ CONTROL TIER ─────────────────▼──────────────────────────────────┐
│ credentials: JIRA_API_TOKEN, GITHUB_WRITE_TOKEN                   │
│ the state machine, the gates, the contracts, the guardrails       │
│ never passes a credential downward, never passes prose upward     │
└───────────────────────────────┬──────────────────────────────────┘
                                │  an approved plan
┌─ BUILD TIER ───────────────────▼──────────────────────────────────┐
│ credentials: BOB_API_KEY only                                     │
│ tools:       read + command + edit(fileRegex)                     │
│ workspace:   a disposable worktree with no git remote             │
│ output:      files on disk — inspected, never trusted             │
└───────────────────────────────────────────────────────────────────┘
```

The separation is not decoration. The analysis tier reads text written by
people outside the organisation. If that tier held a token that could write to
Jira or push to a branch, then a sufficiently persuasive paragraph in a ticket
would be a credential-use primitive. It holds one credential, for the model
API, and the child environment is built by allowlist in
`adapter._child_env()` — not by unsetting known-bad names, which fails open
the moment someone adds a variable.

## Where state lives

Per-ticket state is a **Jira issue property** (`capybara.foreman.state`), with
the current state mirrored to a **label** (`foreman:awaiting-approval`) for
humans.

Not in the local database. The consequences of that choice:

- Delete `var/`, and nothing is lost.
- Move Foreman to another machine and it resumes mid-flight.
- Two operators looking at the board see the same thing Foreman sees.
- A human can force a state by editing the label, and Foreman will honour it.

`var/foreman.db` holds run history: stage durations, costs, transitions,
rejections. It answers "why did Foreman do that three weeks ago". It is an
audit cache, and losing it costs you history, not correctness.

## The contract boundary

Every stage returns one JSON envelope, and every envelope passes through
`contract.py` before anything acts on it. Three levels of scrutiny:

**Shape.** Required fields, enumerated values. Catches a mode whose
instructions have drifted.

**Evidence.** Any citation of the repository is re-read from disk and the
quoted text byte-compared (whitespace-normalised). Line numbers are
*corrected* rather than trusted — models quote real code reliably and count
lines unreliably, and rejecting a true finding over an off-by-two throws away
the work. But the code must exist. An analysis whose every citation fails is
rejected whole and the ticket goes to a human.

**Refusal to accept self-assessment.** Two fields are conspicuously absent
from the contracts:

- The build stage has no "what I changed" field that anything reads. The
  `git diff` is the record.
- The verify stage has no pass/fail field. `gitops.run_command` runs the suite
  and its exit code is the record.

Both omissions are load-bearing. A pipeline where the model grades its own
homework has no error detection at all — every failure mode produces the same
output as success.

## The gates

`gate.py`, and the four checks in the README. The one worth expanding on is
**recency**.

Each time Foreman posts something for approval it records `gate_opened`. A
decision counts only if its comment post-dates that instant. Without it:

1. Foreman posts plan v1. A reviewer types `/foreman approve`.
2. Something fails; the ticket goes round the revise loop.
3. Foreman posts plan v3, substantially different.
4. On the next run the gate sees the approval from step 1 and proceeds.

The reviewer would believe they had examined the change they authorised, and
they would be wrong, and nothing anywhere would say so. That is the worst
available failure of a system like this, because it produces a *false record
of human oversight* — the thing every other control is justified by.

## Guardrails

`guardrails.py` collects the diff (`git add -A` then `git diff --cached`, so
new files count) and applies the rules. Two implementation notes:

**Foreman's own scaffolding is removed before the diff is taken.** The
installer writes `.bob/`, `AGENTS.md` and `.bobignore` into the worktree so
Bob can resolve its modes. Left in place, every build stage "changes" eight
files under a denied path, and the harness fails every ticket on its own
footprint — with an error that looks exactly like the model misbehaving.

**Secrets are scanned in added lines only.** Scanning removed lines would flag
the very commit that takes a credential out of the codebase.

A blocking violation triggers `revert()` — `git reset --hard` plus `git clean
-fdx`. In full, not per-file: a partially reverted change is a half-applied
change on a branch, which is worse than none. Leaving the rejected work on
disk would also invite a later stage to find it and commit it.

## Failure behaviour

Fail loudly, never silently, and never in the direction of "proceed".

| Failure | Result |
|---|---|
| Bob times out or blows its budget | stage `errored`, ticket → `failed`, reason on the ticket |
| Unparseable output | stage `errored`; the raw stdout is in `var/logs/<TICKET>/` |
| Every citation fails verification | ticket → `failed`; no plan is built on unverifiable analysis |
| Diff violates the guardrails | worktree reverted in full; ticket → `failed` with the violation table |
| Existing tests fail after the build | reverted, not published |
| New tests fail | not pushed; the failure is the finding, reported at the gate |
| Non-prod deploy fails | ticket → `failed`; nothing proceeds to verification |
| Gate times out | ticket → `stalled`, reported in the summary. **Never auto-approved.** |
| Revise loop exceeds the limit | ticket → `failed`; "Foreman is not understanding this, a human should look" |
| Approver allowlist empty | the gate stops the run rather than opening. Empty means nobody. |

`failed`, `needs-info` and `stalled` are *human-hold* states, not terminals.
When a person comments or edits the ticket, Foreman returns it to `intake` and
re-analyses from the top — it does not resume mid-flight, because the premises
it stopped on may no longer hold.

## AI-SDLC: requirement flow

A ticket with the `foreman-requirement` label takes a different route from an
ordinary one. `_triage` chooses the route from the ticket's labels
(`stories.kind_of`); a ticket with no AI-SDLC label is unchanged.

```
requirement:  intake -> decomposing -> [Gate 1] -> materialising -> tracking -> done
mapping:      intake -> mapping -> [Gate 2] -> applying-mapping -> closing -> done
dev story:    intake -> analyzed -> [plan gate] -> approved -> committed -> [Gate 3]
                     -> merging -> deploying -> deployed -> closing -> done
```

Design points worth knowing before changing it:

- **Nothing is created in Jira before Gate 1.** `materialise` then records every
  created key after *each* Jira call, so a failure half-way leaves a map of what
  exists and the retry skips it.
- **Dependencies are labels.** A Foreman-owned story that must wait is created
  with `foreman-blocked` instead of `foreman`, and `tracking` swaps the label
  when its blockers are Done. That survives restarts and a person dragging a card.
- **People-owned stories are never given the intake label**, so the intake query
  cannot pick them up.
- **`failed` remembers where.** `advance()` persists `failed_from`; for the stages
  in `machine.RESUMABLE` (which only carry out something already decided) a human
  fix resumes that stage instead of re-analysing from intake.
- **Pod readiness compares timestamps**, not just readiness: pods created before
  the merge do not count, so a deploy that has not started cannot look finished.
- **TapiForge MCP is per-stage.** MCP is disabled for every stage; the mapping
  stages alone opt in, with a checkout-local `.bob/mcp.json` that is deleted before
  any diff and an `alwaysAllow` list of tools named by the client. Write tools are
  only listed for the apply stage, which can only be reached through Gate 2.
- **The merge is the deploy trigger.** Foreman merges (or waits for a person) and
  then *observes* the rollout; it does not run the pipeline.

## Why one state per run

`advance()` performs exactly one transition per ticket per run, even when the
next stage could start immediately.

- Every run is bounded in time and cost, so cron scheduling is predictable.
- The board is legible while work moves — someone watching sees each step.
- An operator can stop the pipeline between any two stages by stopping the
  cron, with no in-flight state to reconcile.
- A bug in one stage cannot cascade through five before anyone notices.

The cost is latency: a ticket needs six runs to reach `done`. At a ten-minute
cron that is an hour of wall clock against days of queue time, and the two
human gates dominate anyway.

## Threat model

**A malicious ticket.** The attacker can write arbitrary text into something a
model reads. Mitigations, in order of how much they are relied on: the diff
inspection and the approval gate (controls), the credential separation
(control), the fencing and `rules/04-untrusted-input.md` (mitigations).
Demonstrated on the seed board — Foreman reports the attempt on the ticket.

**A malicious or compromised repository.** Same tier separation. The analysis
tier has no write credential; the build tier's worktree has no git remote
configured, so even shell access cannot push. `AGENTS.md` from the repository
is explicitly *not* merged into Foreman's — it is preserved as
`AGENTS.repo.md` and read as data. Concatenating it would hand any repository
a direct instruction channel into the agent about to write code in it.

**A model that misbehaves without malice.** The common case, and what most of
the guardrails address: edits the test instead of the code, rewrites more than
asked, wanders outside the plan, fabricates a citation.

**An insider approving their own agent's work.** Not solved here, and it
should not pretend to be. Foreman records who approved and when, verifies the
identity, and refuses to treat its own comments as approvals. Whether one
person may both raise and approve is your change-management policy, expressed
in `FOREMAN_APPROVERS`.

**What is explicitly out of scope: production.** Foreman sets a fixVersion and
raises a deployment ticket. A person schedules the release. Everything
upstream is reversible in minutes; a production deploy is not, and a comment
on a Jira ticket is not informed consent for one on a team's behalf.

## Extending it

- **A new stage** — add a `State` and its transitions in `machine.py`, a
  handler in `stages.py`, a validator in `contract.py`, a mode in
  `extension/custom_modes.yaml`. The dispatcher finds it by name.
- **A different tracker** — `jira.py` is the only module that knows about
  Jira. The interface it presents is small: search, comments, labels, a
  key-value property, transitions.
- **A different deploy system** — add a mode to `deploy.py`. It returns a
  `DeployResult`; nothing else cares how it was produced.
- **A different model runner** — `adapter.py` is the only module that knows
  about Bob Shell, and it already spans two incompatible CLI generations.
