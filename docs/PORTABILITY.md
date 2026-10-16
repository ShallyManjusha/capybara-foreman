# Portability

What it takes to stand AI-SDLC Foreman up in someone else's environment, and
what to find out *before* you promise a date.

## Five-minute triage

Ask these first. Everything else is detail.

| Question | If the answer is… |
|---|---|
| Jira Cloud or Data Center? | Both supported. DC needs the smoke test below; Cloud is verified. |
| GitHub / GHES? | Supported. GitLab, Bitbucket or Azure DevOps: **~1 day of new code.** |
| Does the client have Bob Shell access? | If not, either you run it for them or the engagement does not work. |
| Can the runner reach Jira, git and the Bob API? | Egress through a proxy with TLS interception needs `SSL_CERT_FILE`. Untested — verify early. |
| Python 3.9+ on the runner? | No dependencies to install. Almost always yes. |
| One repository and one team for the pilot? | If they want five repositories on day one, push back. |

## Compatibility

### Verified working

- **Jira Cloud**, REST v3, ADF bodies, API-token auth. Ran end to end on a live
  site: project creation, JQL intake, issue properties, labels, transitions,
  comments, remote links.
- **Bob Shell 1.0.x and 2.x.** Dialect detected from `--help`. 2.x verified live.
- **Local git** (sandbox mode) and **GitHub / GHES** via `GITHUB_API`.
- **Python 3.9–3.13**, macOS and Linux. Zero third-party packages.

### Supported, not yet exercised against a real instance

- **Jira Data Center / Server**, REST v2, wiki-markup bodies, PAT bearer auth.
  The divergences are implemented and unit-tested (`tests/test_datacenter.py`,
  20 tests): endpoint version, body format, auth scheme, base URL with port and
  context path, `/search` instead of `/search/jql`, `/project` instead of
  `/project/search`, `username` instead of `query`, `name` instead of
  `accountId`. Run the smoke test below before committing to a date.

### Not supported

| | Effort | Where the work is |
|---|---|---|
| GitLab, Bitbucket, Azure DevOps | ~1 day | A new module beside `github.py`. Nothing else knows about GitHub. |
| Jenkins / Tekton / ArgoCD | Hours | Try `FOREMAN_DEPLOY_MODE=webhook` first. Otherwise a new branch in `deploy.py`. |
| Jira Service Management approvals | ~half a day | Foreman uses comment commands, which work everywhere. Native JSM approvals would be a new gate reader in `gate.py`. |
| Proxy with TLS interception | Hours, mostly testing | `urllib` honours `HTTPS_PROXY` and `SSL_CERT_FILE`. Neither is verified. |

## Jira Data Center smoke test

Before you promise anything, run this against their instance. It exercises
every endpoint Foreman uses, in about a minute, and writes one comment.

```bash
JIRA_SITE=https://jira.internal.example.com   # full URL: scheme, port, context path
JIRA_EMAIL=                                    # EMPTY on DC
JIRA_API_TOKEN=<personal access token>
JIRA_DEPLOYMENT=auto
```

```bash
cd harness && python3 - <<'PY'
import os
from foreman.env import load_env
from foreman.jira import JiraClient
load_env("../.env")
c = JiraClient(os.environ["JIRA_SITE"], os.environ.get("JIRA_EMAIL", ""),
               os.environ["JIRA_API_TOKEN"],
               deployment=os.environ.get("JIRA_DEPLOYMENT", "auto"))
KEY = "ABC-123"          # any issue you may edit

print("deployment :", c.deployment, "REST v" + c.api_version)
print("whoami     :", c.myself().get("displayName"))
print("projects   :", len(c.list_projects(5)))
print("search     :", len(c.search("project = %s ORDER BY created DESC"
                                   % KEY.split("-")[0], limit=3)))
print("issue      :", (c.get_issue(KEY) or {}).get("key"))
print("comments   :", len(c.comments(KEY)))
print("transitions:", [t["to"]["name"] for t in c.transitions(KEY)])

# The two that most often fail on a locked-down instance.
print("property w :", c.put_state(KEY, {"state": "smoke-test"}))
print("property r :", c.get_state(KEY))
print("remote link:", c.add_remote_link(KEY, "https://example.com", "smoke test"))

body = c.add_comment(KEY, "# Foreman smoke test\n\n"
                          "**bold**, `code`, [link](https://example.com)\n\n"
                          "| A | B |\n|---|---|\n| 1 | 2 |\n\n"
                          ":::warning\nPanel renders?\n:::")
print("comment    :", body.get("id"))
print("read back  :", c.text_of(body.get("body"))[:60])
PY
```

Check, in this order:

- [ ] `deployment` says `datacenter` and `REST v2`. If it says `cloud`, detection
      failed — set `JIRA_DEPLOYMENT=datacenter` explicitly and find out why.
- [ ] `whoami` returns a name. If not, the PAT is wrong or DC wants Basic auth
      with a username.
- [ ] `search` returns issues. DC JQL is stricter about some functions.
- [ ] **`property w` and `property r` both work.** Issue properties are where
      Foreman's state lives. If they are blocked, stop — that is architectural,
      not configuration.
- [ ] The comment **renders** in the browser. Look at it. Literal `h1.` or
      `{code}` in the output means the body format is wrong.
- [ ] Delete the smoke-test comment and clear the property afterwards.

## Pre-engagement checklist

### Access

- [ ] Jira account for Foreman — **its own**, not a person's. When the bot and
      the approver share an account, the ticket history cannot answer *who
      approved this*, which is most of the point of having a gate.
- [ ] Permissions: browse and edit issues, add comments, **set issue
      properties**, transition issues. Creating a project additionally needs
      Jira admin — usually easier to have them create it.
- [ ] Two git tokens, read and write, **separate**. The tier that reads ticket
      text must never hold a token that can push.
- [ ] Bob Shell API key and, for 2.x with a general key, the team ID.
- [ ] Egress from the runner to Jira, git and the Bob API. Get the proxy and CA
      story in writing; it is the most common day-one blocker.

### Decisions the client must make

- [ ] **Who is on `FOREMAN_APPROVERS`.** Name people, not a group. An empty
      list authorises nobody, which is the right default and will block
      everything until answered.
- [ ] **Whether one person may raise and approve.** Foreman records identity;
      it does not enforce segregation of duty. That is their policy.
- [ ] **Which repository and which team** for the pilot. One of each.
- [ ] **Which directories the agent may change** → `allow_paths`.
- [ ] **Auto-merge: yes or no.** Default no, and most teams should keep it.
- [ ] **Who owns the pipeline** once the pilot ends.

### Content work — budget real time for this

- [ ] **`extension/rules/01-repo-policy.md`.** Currently all TODO, deliberately.
      Their error handling, logging, config, money and time conventions, the
      helpers that already exist, the patterns that look wrong and are not.
      **Half a day with a senior engineer from their team**, and it is the
      highest-value half day in the whole engagement. Skip it and the model
      invents conventions, and every change reads as foreign to the maintainer
      who inherits it.
- [ ] **`policy/guardrails.yaml`** → their layout and languages.
- [ ] **Mode `fileRegex`** in `extension/custom_modes.yaml`. Currently assumes
      `src|app|lib|internal|pkg|cmd|tests|spec|__tests__` and
      py/js/ts/go/java/rb/cs/kt/rs. A Maven multi-module build, a monorepo, or
      C/C++/PHP/Scala needs this rewritten.
- [ ] **`FOREMAN_TEST_COMMAND`** — must be the real suite, must exit non-zero on
      failure, should run in under a few minutes.
- [ ] **`status_map`** in `harness/foreman.yaml` → their board's actual status
      names. List them with `c.transitions(KEY)`.
- [ ] **`JIRA_INTAKE_JQL`** — the outer boundary of what the agent can touch.
      Start narrow. Exclude `prod-deployment`, or Foreman finds its own output.

## Day-one setup

```bash
cp .env.example .env && chmod 600 .env    # fill it in
./start.sh --doctor                       # fix everything it names
cd harness && python3 -m foreman --root .. provision --sandbox
./start.sh --ticket THEIR-1               # dry run, one real ticket
```

A dry run takes the same path through the code as a live one and stops at the
moment of writing. Read the analysis it produces for five real tickets before
turning on `FOREMAN_WRITE`. If the analyses are weak, the answer is almost
always the policy file, not the prompts.

Then, in order:

- [ ] `FOREMAN_MAX_TICKETS=2` for the first two weeks.
- [ ] `FOREMAN_GIT_MODE=local` against a clone until the first live PR is boring.
- [ ] `FOREMAN_DEPLOY_MODE=none` until the rest is stable. Add the deploy last.
- [ ] Pin `FOREMAN_MODEL`.
- [ ] Read every diff for a few sprints.

## Effort, honestly

| Environment | Estimate |
|---|---|
| Jira Cloud + GitHub | **1–2 days** config, plus the policy-file half day |
| Jira DC + GHES | **2–3 days** — add a day for the smoke test and whatever it surfaces |
| Anything needing a new git host or tracker | **+1 day per adapter** |
| Air-gapped, proxied, or TLS-intercepted | **+1–2 days**, and find out on day one |

These assume one repository, one team, and someone from the client available
for the policy file. They do not include the weeks of supervised running before
anyone should trust it unattended — that is calendar time, not effort, but it
belongs in the plan.

## Where to make the change

The architecture ports cleanly because each domain module is the only thing
that knows its system:

| Module | Knows about | Replace it to support |
|---|---|---|
| `jira.py` | Jira Cloud + DC | another tracker |
| `markup.py` / `adf.py` | Jira body formats | another tracker's format |
| `github.py` | GitHub REST | GitLab, Bitbucket, ADO |
| `deploy.py` | Actions, webhooks | another CI system |
| `adapter.py` | Bob Shell 1.x + 2.x | another model runner |
| `gitops.py` | git | — |

`machine.py`, `gate.py`, `contract.py`, `guardrails.py`, `stages.py` and
`render.py` do not know what tracker, host, CI system or model they are
attached to, and nothing in a port should need to change them. If a port
starts editing those, something has been wired through the wrong layer.

## What to say no to

- **"Can it deploy to production?"** No. It sets the fixVersion and raises the
  deployment ticket. Everything upstream is reversible in minutes; a production
  release is not, and a comment on a ticket is not informed consent for one on
  a team's behalf.
- **"Can we skip the approval gate for small changes?"** The gate is the
  product. Without it this is an unsupervised agent with a git token.
- **"Can it work the whole backlog?"** Not at first. `FOREMAN_MAX_TICKETS` is
  low for a reason: the cost of a bad pattern is proportional to how many
  tickets it touched before anyone read one.
- **"Can we let it edit the tests so the build goes green?"** No. That is the
  one guardrail to refuse to loosen, every time.
