# Installation

Python 3.9+, `git`, `bash`, and Bob Shell. No third-party Python packages.

## 1. Bob Shell

```bash
curl -fsSL https://bob.ibm.com/download/bobshell.sh | bash
bob --version
```

Foreman supports both CLI generations and detects which you have:

| | 1.0.x | 2.x |
|---|---|---|
| headless | `bob --chat-mode S …` | `bob run --mode S …` |
| budget | `--max-coins N` (coins) | `--max-cost N` (currency) |
| format | `--output-format json` | `--format json` |
| tools | `--allowed-tools` | the mode's `groups` |
| key | `BOBSHELL_API_KEY` | `BOB_API_KEY` |

Set both key variables in `.env` and one file works across a fleet mid-upgrade.

**2.x with a general API key also needs a team id.** Without it every headless
run fails with `Team ID is required for general API keys`. It is on your Bob
team page; put it in `BOB_TEAM_ID`.

> Standing this up for a client rather than yourself?
> **[PORTABILITY.md](PORTABILITY.md)** has the pre-engagement checklist and the
> Data Center smoke test.

## 2. Atlassian API token

**Jira Cloud:** create a token at
<https://id.atlassian.com/manage-profile/security/api-tokens> and set both
`JIRA_EMAIL` and `JIRA_API_TOKEN`. Foreman authenticates as that account with
Basic auth.

**Jira Data Center / Server:** create a personal access token, put it in
`JIRA_API_TOKEN`, and **leave `JIRA_EMAIL` empty** — Foreman then sends it as a
bearer, which is what DC expects. Set `JIRA_SITE` to the full base URL
including scheme, port and any context path
(`http://jira.corp:8080/jira`). The deployment is detected from `/serverInfo`;
`JIRA_DEPLOYMENT=datacenter` forces it if a proxy rewrites that endpoint.

**Create a separate Atlassian account for Foreman before you roll out.** When
the bot and the approver share an account, the ticket history cannot tell you
who approved — which is most of the point of having a gate. `doctor` warns
about it. For a first trial your own account is fine.

Rights needed: browse and edit issues in the target project, add comments, set
issue properties. Creating the demo project additionally needs Jira
administrator; if you do not have it, point Foreman at an existing project with
`provision --project KEY`.

## 3. Configure

```bash
cp .env.example .env && chmod 600 .env
```

Values with spaces must be quoted — `start.sh` sources the file with bash and
the harness parses it in Python, and both need to agree:

```bash
JIRA_INTAKE_JQL="project = FORE AND labels = foreman AND labels != prod-deployment AND statusCategory != Done"
FOREMAN_TEST_COMMAND="python3 -m pytest -q"
```

### The approver allowlist

```bash
FOREMAN_APPROVERS=5f8a...,lead@example.com
```

Account ids are preferred: display names and emails can collide across
directories, account ids cannot. Find yours:

```bash
cd harness && python3 -c "
import os; from foreman.env import load_env; from foreman.jira import JiraClient
load_env('../.env')
c = JiraClient(os.environ['JIRA_SITE'], os.environ['JIRA_EMAIL'], os.environ['JIRA_API_TOKEN'])
for u in c.find_users('lead@example.com'): print(u['accountId'], u['displayName'])"
```

**An empty allowlist authorises nobody.** Every ticket stalls at the first
gate and `doctor` reports it as a problem. That is deliberate: the alternative
default — an empty list meaning "anyone" — turns a misconfiguration into an
open door.

### The intake query

The outer boundary of what Foreman can act on. Keep it narrow; everything it
does not match is invisible to it.

Exclude `prod-deployment`: Foreman raises those tickets itself, and without the
exclusion it would find its own output on the next run and start triaging it.

### Where the code lives

For a first run, leave `FOREMAN_GIT_MODE=local`. The sandbox needs no tokens
and lets you rehearse the whole loop, including the failure paths.

For a real repository:

```bash
FOREMAN_GIT_MODE=github
GITHUB_REPO=your-org/your-service
GITHUB_READ_TOKEN=ghp_...     # clones for analysis
GITHUB_WRITE_TOKEN=ghp_...    # pushes and opens PRs — publisher only
```

Keep them separate. The tier that reads ticket text must never hold a token
that can write to your repository.

### Deployment

| `FOREMAN_DEPLOY_MODE` | Behaviour |
|---|---|
| `simulated` | runs `policy/deploy-simulate.sh`; for rehearsal |
| `github-actions` | `workflow_dispatch`, then polls the run to a conclusion |
| `webhook` | POSTs to `FOREMAN_DEPLOY_WEBHOOK` and trusts the 2xx |
| `none` | skips the stage |

For `github-actions`, the workflow must exist on `FOREMAN_DEPLOY_REF` and
declare `workflow_dispatch` with `ticket`, `branch`, `sha` and `environment`
inputs. See [`ci/deploy-nonprod.yml`](../ci/deploy-nonprod.yml).

## 4. Provision

```bash
cd harness
python3 -m foreman --root .. provision
```

Creates the `FORE` Jira project with four seed tickets, and a sandbox Python
service with a real defect in it. Two of the tickets are the awkward cases:
one too vague to act on, one carrying a prompt-injection payload aimed at the
agent.

`--dry-run` to see what it would do. `--sandbox` or `--jira-only` for one half.
`--reset` to delete previously seeded tickets and start over.

## 5. Check

```bash
./start.sh --doctor
```

Checks Bob's version and dialect, the API key and team id, Jira authentication
and project visibility, whether the intake JQL parses and what it matches, the
approver allowlist, the sandbox or GitHub repository, the test command, the
guardrail policy, and the write interlocks. It tells you what is wrong and what
to do about it. Fix everything before going live.

## 6. Run

```bash
./start.sh                       # dry run — nothing is written anywhere
./start.sh --ticket FORE-1       # one ticket
./start.sh --live                # act, with FOREMAN_WRITE=true
./start.sh --show FORE-1         # state, branch, PR, decisions, history
```

A dry run takes exactly the same path through the code and stops at the moment
of writing. A rehearsal that takes a different route rehearses a different
system.

Driving the demo end to end: run `--live` repeatedly. When a ticket reaches
`awaiting-approval`, comment `/foreman approve` on it and run again.

```cron
*/10 8-19 * * 1-5  /opt/ai-sdlc-foreman/start.sh --live >> /opt/ai-sdlc-foreman/var/cron.log 2>&1
```

Runs take a lock, so an overlapping cron exits immediately rather than
double-advancing a ticket.

## 7. Before a real repository

1. **Fill in `extension/rules/01-repo-policy.md`.** The single highest-value
   thing you will do. Unfilled, the model does not know your error handling,
   your logging, or which helpers exist, so it invents its own and every
   change reads as foreign.
2. **Pin `FOREMAN_MODEL`.**
3. **Narrow `policy/guardrails.yaml`** to the directories the team actually
   expects to change.
4. **Set `FOREMAN_MAX_TICKETS` low** — 2 or 3 — for the first weeks.
5. **Leave `FOREMAN_AUTO_MERGE=false`.** Most teams want the merge button to
   stay human even when everything else does not.

## Troubleshooting

**`Access denied: …/.bob/custom_modes.yaml matches an ignore pattern`** — a
`.bobignore` in the target repository excludes `.bob/`. Bob 2.x refuses to read
a file matching an ignore pattern, including its own modes. Remove that line.

**`bob rejected the invocation`** — the modes are not installed in the
checkout. `extension/install.sh <path>` puts them there; Foreman does this
automatically, so this usually means the script failed. Check `var/logs/`.

**Every gate stalls** — `FOREMAN_APPROVERS` is empty or has the wrong id, or
the approval predates the gate. `./start.sh --show KEY` shows what was seen.

**Approval is recorded as a rejected attempt** — the ticket comment says why:
`not_authorised`, `stale`, or `self`.

**`too_many_lines` on every build** — the change budget in
`policy/guardrails.yaml` is smaller than the work. Raise it, or ask the plan
stage for a narrower change.

**Status never changes on the board** — the names in `foreman.yaml`'s
`status_map` do not exist on your board. Mirroring is best-effort by design.
List the real ones:

```bash
cd harness && python3 -c "
import os; from foreman.env import load_env; from foreman.jira import JiraClient
load_env('../.env')
c = JiraClient(os.environ['JIRA_SITE'], os.environ['JIRA_EMAIL'], os.environ['JIRA_API_TOKEN'])
for t in c.transitions('FORE-1'): print(t['to']['name'])"
```

## What is where when something goes wrong

| Path | Contents |
|---|---|
| `var/logs/run-<id>.log` | the run transcript |
| `var/logs/<TICKET>/<stage>[.N].json` | every prompt and raw response, never overwritten |
| `var/out/run-<id>.json` | machine-readable run result |
| `var/foreman.db` | transitions, stage costs, durations |
| `var/work/<TICKET>*/` | the worktrees, if `--keep-src` |
