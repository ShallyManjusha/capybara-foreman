"""
Requirement → Feature → Epic → Story: the shape of a AI-SDLC delivery.

A *requirement* arrives from Jira BR, ServiceNow or email and becomes one Jira
ticket carrying the `foreman` and `foreman-requirement` labels. Foreman drafts a
decomposition, a human approves it (Gate 1), and this module then creates:

    Feature   one per requirement / project, carries the impact analysis
      Epic    one per interface that has to change
        Story up to eight per Epic, always in the same order (see ORDER)

Only some story types are worked by Foreman. The rest are owned by people, and
the requirement ticket simply *tracks* them to Done:

    mapping      Foreman   (TapiForge MCP, Gate 2)
    dev          Foreman   (code + tests + PR, Gate 3, merge, DEV deploy, DEV tests)
    codereview   Foreman   (closed by Foreman once Gate 3 passes — no work of its own)
    pipeline     a person  (the CICD team; Foreman never writes pipeline YAML)
    regression   a person
    qa           a person
    proddeploy   a person  (Foreman never deploys to production)
    prodvalidate a person

Dependencies are enforced by *labels*, not by trust. A Foreman-owned story is
created without the `foreman` intake label if something has to finish first; the
tracking stage adds the label when its blockers are Done. That is how "Foreman
proceeds only once a human has confirmed the mappings" survives a restart, a
crash, or somebody dragging a card around the board.
"""
import os
from typing import Any, Callable, Dict, List, Optional, Tuple

# ── Labels ───────────────────────────────────────────────────────────────────
INTAKE_LABEL = "foreman"                 # what the intake JQL selects on
REQUIREMENT_LABEL = "foreman-requirement"
RAISED_LABEL = "foreman-raised"          # created by Foreman; never re-triaged
BLOCKED_LABEL = "foreman-blocked"        # waiting on another story
TYPE_LABEL_PREFIX = "foreman-type-"

# ── The eight story types, in the order they are created and worked ──────────
ORDER = ["mapping", "dev", "pipeline", "codereview",
         "regression", "qa", "proddeploy", "prodvalidate"]

TYPES: Dict[str, Dict[str, Any]] = {
    "mapping":      {"title": "Mapping",                    "owner": "foreman",
                     "aliases": ["mapping", "mappings", "mappingtask", "tapiforge"]},
    "dev":          {"title": "Development & Unit Test",    "owner": "foreman",
                     "aliases": ["dev", "development", "devunittest", "dev+unittest",
                                 "devunittests", "developmentunittest"]},
    "pipeline":     {"title": "Pipeline",                   "owner": "human",
                     "aliases": ["pipeline", "cicd", "pipelinedevelopment"]},
    "codereview":   {"title": "Code Review",                "owner": "foreman-closed",
                     "aliases": ["codereview", "review"]},
    "regression":   {"title": "Regression",                 "owner": "human",
                     "aliases": ["regression", "regressiontesting"]},
    "qa":           {"title": "Enterprise QA Testing",      "owner": "human",
                     "aliases": ["qa", "qatesting", "enterpriseqa",
                                 "enterpriseqatesting", "testing"]},
    "proddeploy":   {"title": "Production Deployment",      "owner": "human",
                     "aliases": ["proddeploy", "proddeployment", "productiondeployment",
                                 "production", "deployment"]},
    "prodvalidate": {"title": "Production Validations",     "owner": "human",
                     "aliases": ["prodvalidate", "prodvalidation", "prodvalidations",
                                 "productionvalidation", "productionvalidations",
                                 "validation"]},
}

# Foreman may not start these until the listed stories in the same Epic are Done.
BLOCKED_BY: Dict[str, List[str]] = {
    "dev": ["mapping", "pipeline"],
}

MAX_EPICS = 4


def normalise_type(raw: Any) -> str:
    """Map whatever the model called a story type onto a canonical key, or ''."""
    key = "".join(ch for ch in str(raw or "").lower() if ch.isalnum() or ch == "+")
    key = key.replace("+", "")
    for canon, spec in TYPES.items():
        if key == canon or key in [a.replace("+", "") for a in spec["aliases"]]:
            return canon
    return ""


def type_label(story_type: str) -> str:
    return TYPE_LABEL_PREFIX + story_type


def kind_of(labels: List[str]) -> str:
    """What sort of ticket is this? Decides which route intake sends it down.

    A ticket with no AI-SDLC label is an ordinary Foreman ticket and keeps the
    original triage → plan → build route. That is what keeps the base product
    working unchanged for teams that never use requirements.
    """
    labs = set(labels or [])
    if REQUIREMENT_LABEL in labs:
        return "requirement"
    for t in ORDER:
        if type_label(t) in labs:
            return t
    return "dev"


# ── Jira hierarchy ───────────────────────────────────────────────────────────
def _link(parent_key: str, field_env: str) -> Tuple[str, Dict[str, Any]]:
    """How to attach a child to its parent on *this* Jira.

    Cloud and company-managed projects with the new hierarchy take `parent`.
    Data Center and classic projects need the Epic Link / Parent Link custom
    field instead — its id differs per instance, so the client supplies it.
    """
    field = os.environ.get(field_env, "").strip()
    if field:
        return "", {field: parent_key}
    return parent_key, {}


def feature_enabled() -> bool:
    return os.environ.get("FOREMAN_FEATURE_ISSUETYPE", "Feature").strip().lower() \
        not in ("", "none", "off")


def issue_types() -> Dict[str, str]:
    return {
        "feature": os.environ.get("FOREMAN_FEATURE_ISSUETYPE", "Feature").strip(),
        "epic": os.environ.get("FOREMAN_EPIC_ISSUETYPE", "Epic").strip() or "Epic",
        "story": os.environ.get("FOREMAN_STORY_ISSUETYPE", "Story").strip() or "Story",
    }


def story_description(story: Dict[str, Any], requirement: str, epic_key: str,
                      type_title: str) -> str:
    parts = [story.get("description", "").strip(), ""]
    ac = story.get("acceptance_criteria") or []
    if ac:
        parts += ["## Acceptance criteria"] + ["- %s" % c for c in ac] + [""]
    parts += ["---",
              "_%s story · requirement %s · epic %s · raised by AI-SDLC Foreman_"
              % (type_title, requirement, epic_key or "—")]
    return "\n".join(parts)


# ── Materialisation ──────────────────────────────────────────────────────────
def materialise(jira, project: str, requirement_key: str, decomp: Dict[str, Any],
                created: Dict[str, Any],
                save: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
    """Create the Feature, Epics and Stories. Safe to run again after a failure.

    `created` is persisted through `save` after *every* Jira call, so a crash
    or a refused issue type half-way through leaves a map of what exists, and
    the retry skips it. Without that, a retry would double the board.
    """
    types = issue_types()
    created.setdefault("epics", {})
    created.setdefault("stories", {})

    feature_key = created.get("feature") or ""
    feat = decomp.get("feature") or {}
    if feature_enabled() and not feature_key and feat.get("summary"):
        issue = jira.create_issue(
            project, feat["summary"],
            "%s\n\n---\n_Requirement %s · raised by AI-SDLC Foreman_"
            % (feat.get("description", ""), requirement_key),
            issue_type=types["feature"],
            labels=[RAISED_LABEL, "foreman-feature"])
        feature_key = issue.get("key", "")
        created["feature"] = feature_key
        save(created)

    for epic in decomp.get("epics", []):
        eid = epic["id"]
        epic_key = created["epics"].get(eid, "")
        if not epic_key:
            parent, extra = ("", {})
            if feature_key:
                parent, extra = _link(feature_key, "FOREMAN_FEATURE_LINK_FIELD")
            name_field = os.environ.get("FOREMAN_EPIC_NAME_FIELD", "").strip()
            if name_field:
                extra = dict(extra, **{name_field: epic["summary"][:250]})
            issue = jira.create_issue(
                project, epic["summary"],
                "%s\n\n---\n_Interface: %s · requirement %s · raised by AI-SDLC Foreman_"
                % (epic.get("description", ""), epic.get("interface", "—"),
                   requirement_key),
                issue_type=types["epic"], labels=[RAISED_LABEL],
                parent=parent, extra_fields=extra or None)
            epic_key = issue.get("key", "")
            created["epics"][eid] = epic_key
            save(created)

        for story in epic["stories"]:
            skey = "%s/%s" % (eid, story["story_type"])
            if created["stories"].get(skey):
                continue
            spec = TYPES[story["story_type"]]
            blockers = [created["stories"].get("%s/%s" % (eid, b))
                        for b in BLOCKED_BY.get(story["story_type"], [])
                        if any(s["story_type"] == b for s in epic["stories"])]
            blocked = spec["owner"] == "foreman" and any(blockers)
            labels = [RAISED_LABEL, type_label(story["story_type"])]
            if spec["owner"] == "foreman":
                labels.append(BLOCKED_LABEL if blocked else INTAKE_LABEL)
            parent, extra = _link(epic_key, "FOREMAN_EPIC_LINK_FIELD")
            issue = jira.create_issue(
                project, ("[%s] %s" % (spec["title"], story["summary"]))[:250],
                story_description(story, requirement_key, epic_key, spec["title"]),
                issue_type=types["story"], labels=labels,
                parent=parent, extra_fields=extra or None)
            created["stories"][skey] = issue.get("key", "")
            save(created)
    return created


def build_tracking(decomp: Dict[str, Any], created: Dict[str, Any]
                   ) -> Dict[str, Dict[str, Any]]:
    """What the tracking stage needs: who blocks whom, and who owns what."""
    out: Dict[str, Dict[str, Any]] = {}
    for epic in decomp.get("epics", []):
        eid = epic["id"]
        present = {s["story_type"] for s in epic["stories"]}
        for story in epic["stories"]:
            t = story["story_type"]
            key = (created.get("stories") or {}).get("%s/%s" % (eid, t))
            if not key:
                continue
            blockers = [created["stories"].get("%s/%s" % (eid, b))
                        for b in BLOCKED_BY.get(t, []) if b in present]
            blockers = [b for b in blockers if b]
            owner = TYPES[t]["owner"]
            out[key] = {"type": t, "epic": eid, "owner": owner,
                        "blocked_by": blockers,
                        "activated": not (owner == "foreman" and blockers)}
    return out


def link_children(jira, requirement_key: str, decomp: Dict[str, Any],
                  created: Dict[str, Any]) -> None:
    """Tell each story who its siblings are, so it can close them later.

    Stored on the story's own state property: a story is worked in a later run
    with no memory of the run that created it, and a code-review story has to
    be closable from the dev story without a search.
    """
    for epic in decomp.get("epics", []):
        eid = epic["id"]
        siblings = {s["story_type"]: created["stories"].get("%s/%s" % (eid, s["story_type"]))
                    for s in epic["stories"]}
        siblings = {k: v for k, v in siblings.items() if v}
        for story in epic["stories"]:
            key = created["stories"].get("%s/%s" % (eid, story["story_type"]))
            if key:
                jira.put_state(key, {
                    "requirement": requirement_key,
                    "story_type": story["story_type"],
                    "epic": created["epics"].get(eid, ""),
                    "feature": created.get("feature", ""),
                    "siblings": siblings,
                    "plan_acceptance_criteria": story.get("acceptance_criteria", []),
                })


def ready_to_activate(tracking: Dict[str, Dict[str, Any]],
                      is_done: Callable[[str], Optional[bool]]) -> List[str]:
    """Foreman-owned stories whose blockers are all Done, and not yet released."""
    out = []
    for key, info in tracking.items():
        if info.get("activated") or info.get("owner") != "foreman":
            continue
        if all(is_done(b) for b in info.get("blocked_by", [])):
            out.append(key)
    return out


def tree_markdown(decomp: Dict[str, Any], created: Optional[Dict[str, Any]] = None
                  ) -> str:
    """The decomposition as a reviewer reads it — at the gate and after creation."""
    created = created or {}
    rows = []
    feat = decomp.get("feature") or {}
    if feat.get("summary"):
        rows.append("**Feature:** %s%s" % (
            feat["summary"], (" — `%s`" % created["feature"]) if created.get("feature") else ""))
        rows.append("")
    for epic in decomp.get("epics", []):
        ek = (created.get("epics") or {}).get(epic["id"])
        rows.append("### %s%s" % (epic["summary"], (" — `%s`" % ek) if ek else ""))
        if epic.get("interface"):
            rows.append("_Interface:_ %s%s" % (
                epic["interface"],
                " · **new integration**" if epic.get("new_integration") else ""))
        rows.append("")
        rows.append("| # | Story | Worked by | Acceptance criteria |")
        rows.append("|---|---|---|---|")
        for n, s in enumerate(epic["stories"], 1):
            spec = TYPES[s["story_type"]]
            who = {"foreman": "Foreman", "human": "a person",
                   "foreman-closed": "Foreman (closes)"}[spec["owner"]]
            sk = (created.get("stories") or {}).get("%s/%s" % (epic["id"], s["story_type"]))
            ac = "; ".join(s.get("acceptance_criteria") or [])[:240] or "—"
            rows.append("| %d | %s%s | %s | %s |" % (
                n, ("%s: %s" % (spec["title"], s["summary"]))[:140],
                (" — `%s`" % sk) if sk else "", who, ac.replace("|", "/")))
        rows.append("")
    return "\n".join(rows)
