"""
Stage output contracts.

Bob Shell proposes; the harness decides. Every stage emits one JSON envelope,
and this module is where an envelope stops being a claim and becomes a fact the
pipeline is willing to act on.

Three kinds of check, in increasing order of how much they matter:

1. **Shape.** Required fields, enumerated values, sane types. Cheap, and it
   catches a mode whose instructions have drifted.
2. **Evidence.** Any citation of the repository is re-read from disk and the
   quoted text must actually be there. Line numbers are corrected rather than
   trusted — models quote real code reliably and count lines unreliably, and
   rejecting a true finding over an off-by-two throws away the work. But the
   code must *exist*. A fabricated citation is not a near-miss; it is deleted.
3. **Self-assessment is never taken at face value.** The build stage's account
   of what it changed is commentary; the git diff is the record. The verify
   stage's account of whether the tests passed is commentary; the exit code of
   the test run is the record. Any pipeline that lets a model grade its own
   homework will eventually be told the homework is excellent.
"""
import os
import re
from typing import Any, Dict, List, Optional, Tuple

RISKS = ("low", "medium", "high")
MAX_EPIC_DESC = 16000

ACTIONS = ("create", "modify", "delete")
VERDICTS = ("accept", "revise", "reject")


class Rejection(Exception):
    def __init__(self, code, detail=""):
        super().__init__("%s: %s" % (code, detail))
        self.code = code
        self.detail = detail


class Validated:
    """A stage's accepted output, plus everything that was thrown away."""
    def __init__(self, stage: str, data: Dict[str, Any]):
        self.stage = stage
        self.data = data
        self.rejections: List[Dict[str, str]] = []
        self.corrections: List[Dict[str, Any]] = []

    def reject(self, code, detail=""):
        self.rejections.append({"code": code, "detail": str(detail)[:300]})

    def as_dict(self):
        return {"stage": self.stage, "data": self.data,
                "rejections": self.rejections, "corrections": self.corrections}


def _norm(s: str) -> str:
    """Whitespace-insensitive comparison form.

    Indentation and line-ending differences must not fail an honest citation,
    but the tokens themselves must match exactly.
    """
    return re.sub(r"\s+", " ", (s or "")).strip()


def _str(d: Dict[str, Any], key: str, required: bool = True,
         maxlen: int = 4000) -> str:
    v = d.get(key)
    if v in (None, ""):
        if required:
            raise Rejection("missing_field", key)
        return ""
    return str(v).strip()[:maxlen]


def _list(d: Dict[str, Any], key: str, required: bool = False) -> List[Any]:
    v = d.get(key)
    if v is None:
        if required:
            raise Rejection("missing_field", key)
        return []
    if not isinstance(v, list):
        raise Rejection("not_a_list", key)
    return v


def _enum(value: str, allowed: Tuple[str, ...], field: str) -> str:
    v = str(value or "").strip().lower()
    if v not in allowed:
        raise Rejection("bad_%s" % field, v or "(empty)")
    return v


def _rel_path(raw: Any, field: str = "path") -> str:
    p = str(raw or "").strip().replace("\\", "/")
    if not p:
        raise Rejection("missing_field", field)
    if os.path.isabs(p) or p.startswith("../") or "/../" in p or p == "..":
        raise Rejection("bad_path", p)
    return p.lstrip("./")


# ───────────────────────────────────────────────────────────────── evidence ──
def read_lines(repo_root: str, rel_path: str, start: int,
               end: int) -> Optional[List[str]]:
    path = os.path.normpath(os.path.join(repo_root, rel_path))
    if not path.startswith(os.path.normpath(repo_root) + os.sep):
        return None                        # refuse traversal outside the tree
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return None
    if start < 1 or end > len(lines) or start > end:
        return None
    return lines[start - 1:end]


def locate_snippet(repo_root: str, rel_path: str, snippet: str):
    """Find where `snippet` actually lives. Returns (start, end, text) or None.

    The anti-fabrication guarantee is "this code exists in this file", not "the
    model counted lines correctly". So the quoted text is searched for, and the
    range corrected — but if the text is not in the file, the citation dies.
    """
    path = os.path.normpath(os.path.join(repo_root, rel_path))
    if not path.startswith(os.path.normpath(repo_root) + os.sep):
        return None
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return None

    want = _norm(snippet)
    if not want:
        return None
    for start in range(len(lines)):
        acc = ""
        for end in range(start, min(start + 400, len(lines))):
            acc = _norm(acc + " " + lines[end]) if acc else _norm(lines[end])
            if len(acc) < len(want):
                if want.startswith(acc[:max(1, len(acc))]) or acc in want:
                    continue
                break
            if want in acc:
                return start + 1, end + 1, "".join(lines[start:end + 1])
            break
    return None


def verify_evidence(item: Dict[str, Any], repo_root: str,
                    out: Validated) -> Optional[Dict[str, Any]]:
    """Validate one cited piece of code. Returns the corrected item, or None."""
    try:
        rel = _rel_path(item.get("file"), "file")
        ls, le = int(item.get("line_start", 0)), int(item.get("line_end", 0))
    except (Rejection, TypeError, ValueError) as e:
        out.reject("bad_citation", getattr(e, "detail", str(e)))
        return None
    if ls < 1 or le < ls:
        out.reject("bad_lines", "%s:%s-%s" % (item.get("file"), ls, le))
        return None

    claimed = _norm(item.get("snippet", ""))
    if not claimed:
        out.reject("missing_snippet", rel)
        return None

    actual = read_lines(repo_root, rel, ls, le)
    if actual is not None:
        real = _norm("".join(actual))
        if claimed in real or real in claimed:
            item["file"] = rel
            item["snippet"] = "".join(actual).rstrip("\n")
            item["evidence_verified"] = True
            return item

    found = locate_snippet(repo_root, rel, item.get("snippet", ""))
    if found is None:
        out.reject("snippet_not_in_file" if actual is not None
                   else "citation_unreadable", "%s (cited %d-%d)" % (rel, ls, le))
        return None

    new_start, new_end, text = found
    out.corrections.append({"file": rel, "from": [ls, le],
                            "to": [new_start, new_end]})
    item.update({"file": rel, "line_start": new_start, "line_end": new_end,
                 "snippet": text.rstrip("\n"), "evidence_verified": True,
                 "line_numbers_corrected": True})
    return item




def validate_decomposition(payload: Dict[str, Any]) -> Validated:
    """Validate a requirement's Feature -> Epic -> Story breakdown.

    The model proposes; the harness decides the *shape*. Whatever order the
    model lists stories in, they come out in `stories.ORDER`. A story type the
    harness does not know is dropped, not guessed at. And the stories every
    delivery needs (code review, regression, QA, production deployment,
    production validations) are added with plain default criteria when the
    model left them out, because a delivery with no production-validation
    story is not a shorter plan, it is a hole in it.
    """
    from . import stories as st
    out = Validated("decompose", {})
    d = (payload or {}).get("decomposition")
    if not isinstance(d, dict):
        raise Rejection("missing_envelope", "decomposition")

    if d.get("understood") is False:
        q = [str(x)[:400] for x in _list(d, "clarifications_needed")][:10]
        if not q:
            raise Rejection("not_understood_without_questions",
                            "understood=false requires clarifications_needed")
        out.data = {"understood": False, "summary": _str(d, "summary", required=False),
                    "clarifications_needed": q}
        return out

    feat_raw = d.get("feature") if isinstance(d.get("feature"), dict) else {}
    feature = {"summary": _str(feat_raw, "summary", required=False, maxlen=255),
               "description": _str(feat_raw, "description", required=False,
                                   maxlen=MAX_EPIC_DESC)}

    epics_raw = _list(d, "epics", required=True)
    if not epics_raw:
        raise Rejection("no_epics", "decomposition must contain at least one epic")

    epics, seen_ids = [], set()
    for n, e in enumerate(epics_raw[:st.MAX_EPICS], 1):
        if not isinstance(e, dict):
            out.reject("bad_epic_entry", str(e)[:120])
            continue
        try:
            summary = _str(e, "summary", maxlen=255)
        except Rejection as ex:
            out.reject(ex.code, ex.detail)
            continue
        eid = re.sub(r"[^a-z0-9]+", "-", str(e.get("id") or "e%d" % n).lower()).strip("-") or "e%d" % n
        if eid in seen_ids:
            eid = "%s-%d" % (eid, n)
        seen_ids.add(eid)
        new_integration = bool(e.get("new_integration", False))

        by_type: Dict[str, Dict[str, Any]] = {}
        for s in _list(e, "stories")[:16]:
            if not isinstance(s, dict):
                out.reject("bad_story_entry", str(s)[:120])
                continue
            t = st.normalise_type(s.get("story_type"))
            if not t:
                out.reject("unknown_story_type", str(s.get("story_type"))[:60])
                continue
            if t in by_type:
                out.reject("duplicate_story_type", "%s in epic %s" % (t, eid))
                continue
            try:
                s_summary = _str(s, "summary", maxlen=255)
            except Rejection as ex:
                out.reject(ex.code, ex.detail)
                continue
            criteria = [str(c)[:300] for c in _list(s, "acceptance_criteria")][:6]
            if not criteria:
                out.reject("no_acceptance_criteria",
                           "story %r has no acceptance criteria" % s_summary[:60])
                continue
            by_type[t] = {"story_type": t, "summary": s_summary,
                          "description": _str(s, "description", required=False,
                                              maxlen=1500),
                          "acceptance_criteria": criteria}

        # A pipeline story only exists for a new integration: the CICD team has
        # nothing to build for a change to an existing one.
        if "pipeline" in by_type and not new_integration:
            del by_type["pipeline"]
            out.corrections.append({"epic": eid, "dropped": "pipeline",
                                    "why": "not a new integration"})
        if "pipeline" not in by_type and new_integration:
            by_type["pipeline"] = _default_story(
                "pipeline", summary,
                ["The pipeline for this interface exists and deploys to DEV"])
            out.corrections.append({"epic": eid, "added": "pipeline",
                                    "why": "new integration"})
        if "dev" not in by_type:
            raise Rejection("no_dev_story",
                            "epic %r has no development story" % summary[:60])
        for t, crit in (("codereview", ["The change is reviewed against the standards"]),
                        ("regression", ["The regression suite passes with the change"]),
                        ("qa", ["The agreed QA test cases pass on the QA environment"]),
                        ("proddeploy", ["The change is deployed to production"]),
                        ("prodvalidate", ["Post-deployment validation passes in production"])):
            if t not in by_type:
                by_type[t] = _default_story(t, summary, crit)
                out.corrections.append({"epic": eid, "added": t,
                                        "why": "every delivery needs one"})
        epics.append({
            "id": eid, "summary": summary,
            "description": _str(e, "description", required=False, maxlen=3000),
            "interface": _str(e, "interface", required=False, maxlen=200),
            "new_integration": new_integration,
            "stories": [by_type[t] for t in st.ORDER if t in by_type]})

    if not epics:
        raise Rejection("no_valid_epics", "every epic entry failed validation")
    if not feature["summary"]:
        feature["summary"] = epics[0]["summary"] if len(epics) == 1 else \
            "Requirement: %s" % epics[0]["summary"]
    impact = _str(d, "impact_analysis", required=False, maxlen=MAX_EPIC_DESC)
    if impact:
        feature["description"] = ("%s\n\n## Impact analysis\n%s"
                                  % (feature["description"], impact)).strip()
    out.data = {"understood": True, "feature": feature, "epics": epics,
                "impact_analysis": impact}
    return out


def _default_story(story_type: str, epic_summary: str, criteria: List[str]) -> Dict[str, Any]:
    from . import stories as st
    return {"story_type": story_type,
            "summary": epic_summary[:200],
            "description": "%s for: %s" % (st.TYPES[story_type]["title"], epic_summary),
            "acceptance_criteria": criteria}


def validate_mapping(payload: Dict[str, Any]) -> Validated:
    """A proposed set of TapiForge mapping changes. A person confirms them at Gate 2."""
    out = Validated("mapping", {})
    m = (payload or {}).get("mapping")
    if not isinstance(m, dict):
        raise Rejection("missing_envelope", "mapping")
    changes = []
    for c in _list(m, "changes", required=True)[:200]:
        if not isinstance(c, dict):
            out.reject("bad_change_entry", str(c)[:120])
            continue
        try:
            changes.append({
                "action": _enum(c.get("action", "modify"),
                                ("add", "modify", "remove"), "action"),
                "target": _str(c, "target", maxlen=300),
                "source": _str(c, "source", required=False, maxlen=300),
                "rule": _str(c, "rule", required=False, maxlen=600),
                "current": _str(c, "current", required=False, maxlen=600),
                "why": _str(c, "why", required=False, maxlen=400)})
        except Rejection as e:
            out.reject(e.code, e.detail)
    questions = [str(x)[:400] for x in _list(m, "open_questions")][:10]
    if not changes and not questions:
        raise Rejection("empty_mapping",
                        "no changes and no open questions — nothing to review")
    out.data = {"summary": _str(m, "summary", required=False, maxlen=1500),
                "interface": _str(m, "interface", required=False, maxlen=200),
                "changes": changes, "open_questions": questions,
                "tapiforge_read": bool(m.get("tapiforge_read", False))}
    return out


def validate_mapping_applied(payload: Dict[str, Any],
                             expected_targets: List[str]) -> Validated:
    """What the apply stage says it wrote to TapiForge.

    A claim, not a fact -- the harness cross-checks it against the tool-call
    count Bob reported, and a target the model did not account for is treated
    as failed rather than assumed fine.
    """
    out = Validated("mapping_apply", {})
    m = (payload or {}).get("mapping_applied")
    if not isinstance(m, dict):
        raise Rejection("missing_envelope", "mapping_applied")
    applied = [str((a or {}).get("target", ""))[:300] for a in _list(m, "applied")
               if isinstance(a, dict)]
    failed = [{"target": str((f or {}).get("target", ""))[:300],
               "error": str((f or {}).get("error", ""))[:400]}
              for f in _list(m, "failed") if isinstance(f, dict)]
    accounted = set(applied) | {f["target"] for f in failed}
    for t in expected_targets:
        if t not in accounted:
            failed.append({"target": t, "error": "not reported by the apply stage"})
    out.data = {"summary": _str(m, "summary", required=False, maxlen=1500),
                "applied": applied, "failed": failed}
    return out


def validate_code_review(payload: Dict[str, Any]) -> Validated:
    """The AI's review of its own diff. Advisory: a human still signs off."""
    out = Validated("codereview", {})
    r = (payload or {}).get("code_review")
    if not isinstance(r, dict):
        raise Rejection("missing_envelope", "code_review")
    findings = []
    for f in _list(r, "findings")[:50]:
        if not isinstance(f, dict):
            continue
        try:
            findings.append({
                "severity": _enum(f.get("severity", "minor"),
                                  ("blocker", "major", "minor", "nit"), "severity"),
                "file": _str(f, "file", required=False, maxlen=300),
                "detail": _str(f, "detail", maxlen=600)})
        except Rejection as e:
            out.reject(e.code, e.detail)
    out.data = {"verdict": _enum(r.get("verdict", "concerns"),
                                 ("pass", "concerns"), "verdict"),
                "summary": _str(r, "summary", maxlen=2000),
                "findings": findings,
                "checked": [str(x)[:200] for x in _list(r, "checked")][:20]}
    # `pass` with a blocker in the list is a contradiction; trust the list.
    if out.data["verdict"] == "pass" and any(f["severity"] == "blocker" for f in findings):
        out.data["verdict"] = "concerns"
        out.corrections.append({"verdict": "pass -> concerns",
                                "why": "a blocker finding was listed"})
    return out


# ─────────────────────────────────────────────────────────────── envelopes ──
def validate_triage(payload: Dict[str, Any], repo_root: str) -> Validated:
    out = Validated("triage", {})
    a = (payload or {}).get("analysis")
    if not isinstance(a, dict):
        raise Rejection("missing_envelope", "analysis")

    understood = bool(a.get("understood", True))
    data: Dict[str, Any] = {
        "understood": understood,
        "summary": _str(a, "summary"),
        "problem_statement": _str(a, "problem_statement"),
        "risk": _enum(a.get("risk", "medium"), RISKS, "risk"),
        "affected_areas": [str(x)[:200] for x in _list(a, "affected_areas")][:20],
        "out_of_scope": [str(x)[:300] for x in _list(a, "out_of_scope")][:20],
        "clarifications_needed": [str(x)[:400] for x in
                                  _list(a, "clarifications_needed")][:10],
    }
    # A ticket Foreman cannot act on is a valid and useful answer — it is how
    # the pipeline says "this needs a human" instead of inventing a change.
    if understood:
        data["root_cause"] = _str(a, "root_cause")
    else:
        data["root_cause"] = _str(a, "root_cause", required=False)
        if not data["clarifications_needed"]:
            raise Rejection("not_understood_without_questions",
                            "understood=false requires clarifications_needed")

    evidence = []
    for item in _list(a, "evidence")[:20]:
        if isinstance(item, dict):
            ok = verify_evidence(dict(item), repo_root, out)
            if ok:
                evidence.append(ok)
    data["evidence"] = evidence
    # An analysis with no verifiable citation is an opinion. It may still be a
    # correct opinion, but it is not something to build a code change on, so it
    # goes to a human rather than to the plan stage.
    if understood and not evidence:
        raise Rejection("no_verified_evidence",
                        "every citation failed verification against the checkout")

    est = a.get("estimated_change") or {}
    data["estimated_change"] = {
        "files": _int(est.get("files"), 0), "lines": _int(est.get("lines"), 0)}
    out.data = data
    return out


def validate_plan(payload: Dict[str, Any]) -> Validated:
    out = Validated("plan", {})
    p = (payload or {}).get("plan")
    if not isinstance(p, dict):
        raise Rejection("missing_envelope", "plan")

    files = []
    for f in _list(p, "files_to_change", required=True):
        if not isinstance(f, dict):
            out.reject("bad_file_entry", str(f)[:120])
            continue
        try:
            files.append({"path": _rel_path(f.get("path")),
                          "action": _enum(f.get("action", "modify"),
                                          ACTIONS, "action"),
                          "why": _str(f, "why", required=False)})
        except Rejection as e:
            out.reject(e.code, e.detail)
    if not files:
        raise Rejection("empty_plan", "no valid entries in files_to_change")

    steps = []
    for n, s in enumerate(_list(p, "steps", required=True), 1):
        if isinstance(s, dict):
            steps.append({"n": _int(s.get("n"), n),
                          "intent": _str(s, "intent"),
                          "detail": _str(s, "detail", required=False),
                          "files": [str(x)[:200] for x in
                                    (s.get("files") or [])][:20]})
    if not steps:
        raise Rejection("empty_plan", "no valid steps")

    tests = []
    for t in _list(p, "tests"):
        if isinstance(t, dict):
            tests.append({"path": str(t.get("path", ""))[:300],
                          "what": str(t.get("what", ""))[:500]})

    criteria = [str(x)[:400] for x in _list(p, "acceptance_criteria")][:20]
    if not criteria:
        # The sign-off gate asks a human "did this do what was agreed?". With
        # no acceptance criteria there is nothing to hold the answer against,
        # and the gate degrades into a vibe check.
        raise Rejection("no_acceptance_criteria",
                        "a plan must state how it will be judged")

    est = p.get("estimated_diff") or {}
    out.data = {
        "approach": _str(p, "approach"),
        "steps": steps,
        "files_to_change": files,
        "tests": tests,
        "acceptance_criteria": criteria,
        "risks": [{"risk": str((r or {}).get("risk", ""))[:300],
                   "mitigation": str((r or {}).get("mitigation", ""))[:300]}
                  for r in _list(p, "risks") if isinstance(r, dict)][:10],
        "rollback": _str(p, "rollback", required=False),
        "estimated_diff": {"files": _int(est.get("files"), len(files)),
                           "lines": _int(est.get("lines"), 0)},
    }
    return out


def validate_implementation(payload: Dict[str, Any]) -> Validated:
    """The build stage's *commentary*. The diff is the record.

    Nothing here is trusted to describe what changed — `guardrails.inspect`
    reads the worktree for that. This exists so the commit message and the
    ticket comment can be written in the model's own words, and so a deviation
    from the plan is stated rather than discovered later in review.
    """
    out = Validated("build", {})
    i = (payload or {}).get("implementation")
    if not isinstance(i, dict):
        raise Rejection("missing_envelope", "implementation")

    msg = _str(i, "commit_message", maxlen=400)
    if "\n" in msg:
        subject, body = msg.split("\n", 1)
    else:
        subject, body = msg, ""
    if len(subject) > 72:
        out.corrections.append({"commit_subject_truncated": len(subject)})
        subject = subject[:69].rstrip() + "..."

    out.data = {
        "summary": _str(i, "summary"),
        "commit_subject": subject,
        "commit_body": body.strip()[:2000],
        "notes": _str(i, "notes", required=False),
        "claimed_files": [str((f or {}).get("path", ""))[:300]
                          for f in _list(i, "files_changed")
                          if isinstance(f, dict)][:100],
        "deviations": [{"from_plan": str((d or {}).get("from_plan", ""))[:300],
                        "why": str((d or {}).get("why", ""))[:400]}
                       for d in _list(i, "deviations")
                       if isinstance(d, dict)][:10],
    }
    return out


def validate_verification(payload: Dict[str, Any]) -> Validated:
    """The verify stage's account of the tests it wrote.

    Deliberately does NOT carry a pass/fail field. Whether the suite passes is
    established by running it, in `stages.verify`, and a model-reported result
    would be indistinguishable from a real one in every report downstream.
    """
    out = Validated("verify", {})
    v = (payload or {}).get("verification")
    if not isinstance(v, dict):
        raise Rejection("missing_envelope", "verification")

    tests = []
    for t in _list(v, "tests_added", required=True):
        if not isinstance(t, dict):
            continue
        try:
            tests.append({"path": _rel_path(t.get("path")),
                          "name": _str(t, "name", required=False),
                          "criterion": _str(t, "criterion", required=False),
                          "asserts": _str(t, "asserts", required=False)})
        except Rejection as e:
            out.reject(e.code, e.detail)
    if not tests:
        raise Rejection("no_tests", "the verify stage added no test files")

    coverage = []
    for c in _list(v, "coverage_of_criteria"):
        if isinstance(c, dict):
            coverage.append({"criterion": str(c.get("criterion", ""))[:400],
                             "covered_by": str(c.get("covered_by", ""))[:300],
                             "covered": bool(c.get("covered", False))})

    out.data = {
        "summary": _str(v, "summary"),
        "tests_added": tests,
        "command": _str(v, "command", required=False, maxlen=300),
        "coverage_of_criteria": coverage,
        "notes": _str(v, "notes", required=False),
    }
    return out


def validate_review(payload: Dict[str, Any]) -> Validated:
    """The adversarial reviewer. Its job is to kill work, not to bless it."""
    out = Validated("review", {})
    r = (payload or {}).get("review")
    if not isinstance(r, dict):
        raise Rejection("missing_envelope", "review")
    out.data = {
        "verdict": _enum(r.get("verdict", "revise"), VERDICTS, "verdict"),
        "reasons": [str(x)[:400] for x in _list(r, "reasons")][:20],
        "blocking": [str(x)[:400] for x in _list(r, "blocking")][:20],
        "notes": _str(r, "notes", required=False),
    }
    # A reviewer that blocks without saying why is not reviewable itself.
    if out.data["verdict"] != "accept" and not (out.data["reasons"] or
                                                out.data["blocking"]):
        raise Rejection("unreasoned_rejection",
                        "verdict %s with no reasons" % out.data["verdict"])
    return out


def _int(v: Any, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default
