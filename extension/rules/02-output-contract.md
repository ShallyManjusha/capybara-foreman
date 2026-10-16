# Output contract

Emit **exactly one JSON object** as your final output. No prose before it, none
after it, no fence around it. The harness parses it; anything else is discarded
and the stage is recorded as failed.

The harness validates every envelope and drops what does not conform. It is not
being pedantic — the fields below are what a human reads at a gate, and a field
you leave out is a question they cannot answer.

---

## `foreman-triage`

```json
{
  "analysis": {
    "understood": true,
    "summary": "two or three sentences a non-engineer could follow",
    "problem_statement": "one line: what is actually broken",
    "root_cause": "the mechanism, not the symptom",
    "evidence": [
      {
        "file": "src/pricing.py",
        "line_start": 41,
        "line_end": 48,
        "snippet": "the exact text of those lines, copied from the file",
        "why": "what this shows"
      }
    ],
    "affected_areas": ["pricing", "order lifecycle"],
    "out_of_scope": ["what you are deliberately not addressing"],
    "risk": "low | medium | high",
    "estimated_change": {"files": 2, "lines": 40},
    "clarifications_needed": []
  }
}
```

**`snippet` is re-read from disk and byte-compared.** Quote it exactly. Line
numbers that are slightly off are corrected for you; text that is not in the
file deletes the citation. An analysis whose every citation fails verification
is rejected as a whole and the ticket goes to a human.

If you cannot establish a root cause from the ticket and the code, set
`understood` to `false` and fill `clarifications_needed` with specific
questions. That path exists so you do not have to guess, and using it is a
success, not a failure.

---

## `foreman-plan`

```json
{
  "plan": {
    "approach": "the change in a paragraph",
    "steps": [{"n": 1, "intent": "what this step achieves", "files": ["src/pricing.py"], "detail": "how"}],
    "files_to_change": [{"path": "src/pricing.py", "action": "modify", "why": "..."}],
    "tests": [{"path": "tests/test_pricing.py", "what": "the behaviour to pin"}],
    "acceptance_criteria": ["checkable statements a reviewer will judge this against"],
    "risks": [{"risk": "...", "mitigation": "..."}],
    "rollback": "how to undo this if it goes wrong in production",
    "estimated_diff": {"files": 2, "lines": 40}
  }
}
```

`action` is one of `create`, `modify`, `delete`.

`files_to_change` is a contract, not a sketch. The build stage may touch these
files and no others; anything else is reverted.

`acceptance_criteria` is mandatory. Without it the sign-off gate has nothing to
judge and degrades into a formality.

---

## `foreman-critic`

```json
{
  "review": {
    "verdict": "accept | revise | reject",
    "blocking": ["objections that should stop this proceeding"],
    "reasons": ["objections worth the reviewer's attention"],
    "notes": "anything else"
  }
}
```

Any verdict other than `accept` must carry at least one reason. A verdict with
no reasons is rejected by the harness.

---

## `foreman-build`

```json
{
  "implementation": {
    "summary": "what you changed and why, for the ticket",
    "files_changed": [{"path": "src/pricing.py", "action": "modify", "summary": "..."}],
    "commit_message": "fix(pricing): reject reuse of single-use discount codes",
    "deviations": [{"from_plan": "what you did differently", "why": "..."}],
    "notes": ""
  }
}
```

`files_changed` is commentary. The harness reads `git diff` for the truth, and
the diff is what the guardrails are applied to. Where the two disagree, the
diff wins and you are not consulted.

`commit_message`: conventional-commit subject under 72 characters, optional
body after a blank line.

---

## `foreman-verify`

```json
{
  "verification": {
    "summary": "what these tests establish",
    "tests_added": [
      {"path": "tests/test_pricing.py", "name": "test_single_use_code_cannot_be_reused",
       "criterion": "the acceptance criterion this covers", "asserts": "what it asserts"}
    ],
    "command": "python3 -m pytest -q",
    "coverage_of_criteria": [
      {"criterion": "...", "covered_by": "test_...", "covered": true}
    ],
    "notes": "criteria that cannot be unit tested, and why"
  }
}
```

There is deliberately **no pass/fail field**. The harness runs the suite and
records its own result. Anything you assert about whether your tests pass is
ignored, so do not spend turns on it.

Mark a criterion `covered: false` when it is not covered. Claiming coverage you
do not have is the single most damaging thing you can do at this stage: a human
signs off on the strength of this table.

---

## `foreman-decompose`

```json
{
  "decomposition": {
    "understood": true,
    "impact_analysis": "what changes, which systems/interfaces are affected, risks",
    "feature": {"summary": "one line", "description": "scope of the requirement"},
    "epics": [
      {
        "id": "orders-sync",
        "summary": "one Epic per interface that changes",
        "description": "a few sentences",
        "interface": "name of the interface / flow",
        "new_integration": false,
        "stories": [
          {
            "story_type": "mapping | dev | pipeline | codereview | regression | qa | proddeploy | prodvalidate",
            "summary": "one line",
            "description": "what must be done and why, a few sentences",
            "acceptance_criteria": ["a checkable statement"]
          }
        ]
      }
    ]
  }
}
```

If you cannot tell what is being asked, return
`{"decomposition": {"understood": false, "clarifications_needed": ["..."]}}`.

The harness enforces: stories are re-ordered into the fixed delivery order;
`pipeline` is kept only when `new_integration` is true; each epic must have a
`dev` story; missing codereview / regression / qa / proddeploy / prodvalidate
stories are added; a story with no acceptance criteria is dropped. Nothing is
created in Jira until a human approves.

---

## `foreman-mapping`

```json
{
  "mapping": {
    "interface": "the interface this concerns",
    "summary": "what these changes achieve",
    "tapiforge_read": true,
    "changes": [
      {"action": "add | modify | remove", "target": "target field/element",
       "source": "where the value comes from", "rule": "the transformation",
       "current": "what TapiForge holds now (empty if you could not read it)",
       "why": "which acceptance criterion this serves"}
    ],
    "open_questions": ["anything you could not determine"]
  }
}
```

`tapiforge_read` is `true` only if you read the current mappings through the
TapiForge MCP tools. It is shown to the reviewer; saying `true` when you did not
read them is the one thing that makes this stage dangerous.

You may only READ TapiForge in this stage.

---

## `foreman-mapping-apply`

```json
{
  "mapping_applied": {
    "summary": "one or two sentences",
    "applied": [{"target": "exactly as in the confirmed list"}],
    "failed":  [{"target": "...", "error": "what the tool said"}]
  }
}
```

Every confirmed `target` must appear under `applied` or `failed`. The harness
treats an unaccounted-for target as failed.

---

## `foreman-codereview`

```json
{
  "code_review": {
    "verdict": "pass | concerns",
    "summary": "two or three sentences",
    "checked": ["what you looked at"],
    "findings": [
      {"severity": "blocker | major | minor | nit", "file": "path", "detail": "specific and actionable"}
    ]
  }
}
```

`pass` with a `blocker` finding is changed to `concerns` by the harness.
