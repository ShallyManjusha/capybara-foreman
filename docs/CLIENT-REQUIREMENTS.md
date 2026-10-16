# AI-SDLC Foreman — what we need from you

This is the checklist for getting the end-to-end AI-SDLC flow running on your
systems. Foreman does the repeatable work; **a person confirms at four gates**.
Everything below is something only you can supply: an account, a value, a
decision, or a rule. Each item says *why* it is needed and *what happens if it
is missing*.

Fill in the **"Your answer"** column and send the document back. Where you do not
know, say so — several items have a safe default.

---

## 1. The flow, and who does what

| # | Step | Who | Needs from you |
|---|---|---|---|
| 1 | Business requirement arrives (Jira BR board, ServiceNow incident, or email) | Business | §4 |
| 2 | **Feature** created (one per requirement) with an impact analysis | Foreman | §3 |
| 3 | **Epics** created (one per interface) and **8 Story types** under each | Foreman | §3 |
| — | **Gate 1** — a person approves the Feature / Epic / Story breakdown. *Nothing is created in Jira before this.* | Person | §3.5 |
| 4 | **Mapping story**: Foreman reads current mappings in TapiForge and drafts the change | Foreman | §6 |
| — | **Gate 2** — a person confirms the mappings. *Foreman proceeds only after this.* | Person | §3.5 |
| 5 | Mappings applied (TapiForge MCP *or* a CSV/JSON a person applies) and the Mapping Jira closed | Foreman / Person | §6 |
| 6 | **Pipeline story** (new integration only) | **CICD team — never Foreman** | §7 |
| 7 | **Dev + Unit Test story**: Foreman creates the branch, writes the code **and** tests, pushes, opens the PR. *Released only when Mapping and Pipeline are Done.* | Foreman | §5, §9 |
| — | Foreman's AI self-review of the diff is posted on the ticket | Foreman | — |
| — | **Gate 3** — a person signs off the code and review | Person | §3.5 |
| 8 | Foreman merges the PR into the DEV branch (`Release_DEV`) and closes the Code Review story | Foreman | §5 |
| 9 | Your pipeline deploys to CP4i DEV; Foreman waits until the new pods are Ready | Pipeline / Foreman | §7 |
| 10 | Foreman runs the ACE test suite (`bob-ace-testing-kit`) against DEV | Foreman | §8 |
| 11 | Dev Jira closed. When every story is Done, the requirement closes | Foreman | §3 |
| — | **Regression, Enterprise QA, Production Deployment, Production Validations** stories | **People** | — |

Foreman **never** deploys to production, never writes pipeline YAML, and never
changes CI configuration. Those stories exist on the board so the delivery is
visible end to end, and the requirement stays open until people close them.

---

## 2. Decisions we need from you

These come from the open questions in the SDLC sheet. Each has a default; tell us
if you want something different.

| # | Decision | Options | Default | Your answer |
|---|---|---|---|---|
| D1 | **Who updates mappings in TapiForge?** | **1** — AI writes via the TapiForge MCP after Gate 2. **2** — AI attaches an Excel/JSON diff and a person applies it | **2** until you give us write-tool names (§6) | |
| D2 | **How does Foreman know pods have restarted?** | **1** — Foreman polls `oc get pods` after the merge and waits for new Ready pods | **1** (implemented) | |
| D3 | **Who triggers the DEV unit tests?** | **1** — Foreman runs your test command after the pods are Ready. **2** — a test stage inside your pipeline *(see note)*. **3** — both | **1** | |
| D4 | **Does Foreman merge, or a person?** | Foreman merges after Gate 3 / a person merges and Foreman waits | person merges (`FOREMAN_AUTO_MERGE=false`) | |
| D5 | **Who creates the feature branch?** | Foreman (`foreman/<KEY>`) / a human | Foreman. The GitHub account needs branch-creation rights (§5) | |
| D6 | **Does your Jira have a `Feature` issue type?** | yes / no | yes. If not, Foreman skips the Feature level | |

*Note on D3 option 2:* Foreman does not read a pipeline's test stage directly. If
you prefer it, give us a command that exits 0 when that stage passed (for example
a script that queries your pipeline's result) and set it as the DEV test command;
Foreman only looks at the exit code.

---

## 3. Jira

### 3.1 Access
| Item | Why | Your answer |
|---|---|---|
| Jira URL and whether it is **Cloud** or **Data Center** | Different auth and different ways to link a story to its epic | |
| A **service account** for Foreman + API token (Cloud) or Personal Access Token (Data Center) | Creates and edits issues, posts comments | |
| That account's **permissions**: browse, create issues, edit issues, add comments, **transition issues**, add attachments, manage issue properties on the project | Without *transition* Foreman cannot close stories; without *attachments* the mapping diff cannot be attached | |
| The **project key** Foreman creates Features / Epics / Stories in | | |

### 3.2 Issue types and links
| Item | Why | Your answer |
|---|---|---|
| Issue type names for Feature, Epic, Story | Defaults are `Feature`, `Epic`, `Story`; a different name makes creation fail | |
| **Data Center only:** the id of the *Epic Link* custom field (e.g. `customfield_10014`) | A DC story is linked to its epic through this field, not `parent` | |
| **Data Center only:** the id of the field that links an Epic to a Feature (*Parent Link*) | Same reason | |
| **Data Center only:** the id of the mandatory *Epic Name* field, if your Epic type requires one | Otherwise epic creation is refused | |
| Are there other **mandatory fields** on these issue types (component, team, fix version…)? | Creation fails if a required field is blank. Tell us the field and the value | |

### 3.3 Workflow status names
Foreman mirrors its progress onto your board by moving tickets to named statuses,
and **silently skips** any status that does not exist. Tell us your names for:
*in progress*, *in review* (gates), and *done*. Defaults: `In Progress`, `In
Review`, `Done`. **"Done" matters most** — the requirement only closes when it
can see every story is in a Done *status category*.

### 3.4 Labels (nothing to do — for information)
`foreman` (Foreman will work this), `foreman-requirement` (a requirement),
`foreman-blocked` (a story waiting on another), `foreman-type-<type>`,
`foreman-raised`. **Do not remove `foreman-blocked` by hand** to "unstick" a
story; Foreman removes it when the blockers are Done.

### 3.5 Approvers
| Item | Why | Your answer |
|---|---|---|
| The people allowed to approve — Jira account ids or emails | An **empty list authorises nobody**, by design. Only these accounts' `/foreman approve` counts | |
| Who should *not* be able to approve? | Foreman uses **one approver list for all four gates** (`FOREMAN_APPROVERS`); it cannot yet give Gate 3 (code) a smaller group than Gate 1 (breakdown). If you need separation of duties, tell us — it is a small change, but it is not there today | |

Approvers reply on the ticket with `/foreman approve`, `/foreman changes <what>`,
or `/foreman reject <why>`. A comment from anyone else is logged and ignored, and
an approval older than the thing it approves does not count.

### 3.6 The query that selects work
Foreman works whatever this query returns. It must include requirements and the
stories Foreman owns, and must exclude stories waiting on another and production
tickets:

```
project = <KEY> AND labels in (foreman, foreman-requirement)
  AND labels not in (prod-deployment, foreman-blocked) AND statusCategory != Done
```

Confirm or adjust: ______________________________

---

## 4. Where requirements come from

Tell us which you use; configure only those.

| Source | We need | Your answer |
|---|---|---|
| **Jira BR board** | The project/board key and a query selecting ready requirements (e.g. `project = BR AND issuetype = Story AND labels = ai-sdlc AND statusCategory != Done`). A service account with *edit* on that board (to mark items as imported) | |
| **ServiceNow** | Instance host, a service user with read on incidents, and an **encoded query** selecting the incidents (category, assignment group). *Optional:* an incident state to set on import. **Do not use Resolved/Closed** — importing a requirement is not fixing it | |
| **Email (IMAP)** | Host, port, mailbox, an app password, and the **folder** to watch. Which senders are trusted? | |

Foreman will not import the same item twice (each import is tagged), and treats
all text from these sources as **untrusted data, never as instructions**.

---

## 5. GitHub

| Item | Why | Your answer |
|---|---|---|
| Repository (`org/repo`) and, if Enterprise Server, host + API URL | | |
| The **DEV branch** name (e.g. `Release_DEV`) | PRs target it; **merging into it must start your pipeline** | |
| A **read token** and a separate **write token** (branches, PRs; merge only if D4 = Foreman merges) | Two tokens so the analysis stages cannot write | |
| Branch protection on the DEV branch: required reviews, required checks, signed commits? | A protected branch can refuse Foreman's merge; tell us so the rules are accounted for | |
| Is the **Foreman account allowed to create branches** (`foreman/<KEY>`)? | The sheet notes the AI user needs branch-creation access | |
| Merge method (`squash`, `merge`, `rebase`) | Default `squash` | |

---

## 6. TapiForge (mappings)

Foreman reads and writes mappings **only through the TapiForge MCP server** and
only in the two mapping stages. If you do not want Foreman to write (D1 = 2), you
still get the read side, and the proposal arrives as a CSV and JSON attached to
the story for a person to apply.

| Item | Why | Your answer |
|---|---|---|
| How is the MCP server reached? **Remote URL** (and whether it speaks *streamable HTTP* or *SSE*) **or** a **local command** + arguments | | |
| The **credential** (token / header name, or env vars for a local server) | Held in `.env` only; never written to a commit or a comment | |
| The exact **names of the read tools** (e.g. get/list mappings), as the server advertises them | Only these are auto-approved while drafting. We do not guess names | |
| The exact **names of the write tools** (only if D1 = 1) | Auto-approved only after Gate 2 | |
| Is there a **sandbox/test TapiForge** for the pilot? | Strongly recommended before any write is enabled | |
| Which **mapping artefacts** exist per interface, and how is one identified (id, name, path)? | So a "target" in the draft means something a reviewer can find | |

*Without any of this:* mappings run in file mode (D1 = 2). The flow still works
end to end; a person applies the confirmed diff.

---

## 7. Pipeline and CP4i / OKD

| Item | Why | Your answer |
|---|---|---|
| Confirm the **CICD team builds and owns pipelines**. For a new integration a *Pipeline* story is created and Dev is blocked until it is Done | Foreman never writes pipeline YAML | |
| Confirm **merging to the DEV branch triggers the deployment** (Tekton / Jenkins / Actions …) | Foreman does not start your pipeline. If something else must trigger it, give us a webhook URL | |
| The **OpenShift namespace** and a **label selector** that matches *the pods of this integration server* (e.g. `app=ace-orders`) | Foreman polls these pods | |
| A **service account** for `oc` with `get pods` in that namespace (and a login method that does not expire mid-run) | Read-only is enough; Foreman never changes the cluster | |
| The machine that runs Foreman must have `oc` on its PATH and reach the cluster API | | |
| Typical deploy-to-ready time | Sets the timeout (default 15 minutes) | |

**How "pods restarted" is decided:** after the merge, Foreman waits until every
matching pod is Ready, none is terminating, and **every pod was created after the
merge** — so pods from the old version cannot be mistaken for the new one. It
needs two consecutive good readings. If your deployment does *not* replace pods
(e.g. a hot-reload of flows), tell us — this rule would never be satisfied.

---

## 8. Testing

| Item | Why | Your answer |
|---|---|---|
| The **command** that runs your `bob-ace-testing-kit` suite against DEV, e.g. `bob-ace-test run --config ace-test.yaml --format junit` | Foreman runs it after the rollout; **exit code 0 = pass**, anything else = fail | |
| Where the kit and its config live (in the repo, or installed on the Foreman host) and what **credentials / endpoints** it needs | | |
| Does the kit need DEV to be up? | If yes, it is only used *after* deploy. A separate quicker command can gate the branch before it is pushed | |
| A **pre-push** test command that needs no deployed environment (optional) | Runs before a branch is published | |

Foreman does not decide whether tests passed from what the AI says — only from
the exit code of the command the harness itself runs.

---

## 9. What the AI may change (ACE files)

This is the item most likely to block a first run, so please read it.

By default the build stage may only change ordinary source and test files
(`src/ app/ lib/ tests/` with `.py .js .ts .go .java …`) — **not** ACE artefacts.
Two files must be widened *together* for ACE work, deliberately:

1. `policy/guardrails.yaml` → `allow_paths`: add the patterns your repo uses, e.g.
   `*.msgflow`, `*.subflow`, `*.esql`, `*.xsl`, `*.xsd`, `*.mapmsgflow`.
2. `extension/custom_modes.yaml` → the `foreman-build` mode `fileRegex`: add the
   same extensions and your project folder layout.

Even when widened, the following stay **forbidden and cannot be widened by
configuration**: CI configuration (`.github/workflows`), `.bob/`, credentials and
key material.

| Item | Your answer |
|---|---|
| Repository layout: where do flows, ESQL, mappings and tests live? | |
| File types the AI may edit | |
| Paths the AI must **never** touch (shared libraries, `.bar` build artefacts, environment overrides…) | |
| Are **binary** files (e.g. `.bar`, `.jar`) checked in? | Binary changes are always rejected |
| Largest change you would accept without a closer look (files / lines) | Default: 15 files, 600 lines |
| Your **coding standards / ESQL conventions** document | Goes into `rules/01-repo-policy.md` so output reads like your code. `install.sh` warns while it is still a placeholder |

Existing tests can never be edited or deleted by the AI. New tests are expected.

---

## 10. Bob Shell

| Item | Your answer |
|---|---|
| Bob Shell installed on the Foreman host (`bob --version`) | |
| `BOB_API_KEY`, and `BOB_TEAM_ID` for Bob Shell 2.x with a general key | |
| A pinned model | |
| Per-stage cost budget you are comfortable with | |

---

## 11. Hosting and security

| Item | Your answer |
|---|---|
| Where does Foreman run (a VM / pod), and who operates it? | |
| Is **outbound access** available from that host to: Jira, GitHub, Bob, TapiForge, the OpenShift API, ServiceNow, IMAP? | |
| How are secrets injected (secret manager → environment)? `.env` must be `chmod 600` and never committed | |
| Schedule: how often should Foreman poll (e.g. every 10 min in working hours)? | |
| Who is told when a ticket reaches `foreman:failed`? | |

Writes require **both** the `--live` flag **and** `FOREMAN_WRITE=true`. Until both
are set, everything is a dry run that reads and reports and changes nothing.

---

## 12. Go-live steps (in order)

1. Complete this document.
2. Install Foreman, copy `.env.example` to `.env`, fill in the values from §3–§11.
3. Install the Bob modes into the repository: `bash extension/install.sh <repo>`.
4. Widen the ACE allow-paths (§9) if the AI will edit ACE files.
5. Run `python3 -m foreman doctor`. Fix every item it reports — it checks Jira,
   GitHub, the intake query, OpenShift access, the TapiForge tool lists and the
   approvers.
6. **Dry run** one requirement: `python3 -m foreman run` and read the log.
7. **Pilot with D1 = 2** (no TapiForge writes) and `FOREMAN_AUTO_MERGE=false` on
   one low-risk requirement. Confirm the Feature / Epic / Story tree at Gate 1.
8. Walk that requirement through Gate 2 and Gate 3. Check the DEV rollout and test
   result.
9. Only then consider enabling TapiForge writes (D1 = 1) and auto-merge (D4).

## 13. What is **not** covered

- Production deployment and validation (people, by design).
- Creating or editing pipelines (CICD team, by design).
- Business-requirement authoring.
- Anything that needs a human decision the sheet leaves open and this document has
  not asked about — tell us what we have missed.
