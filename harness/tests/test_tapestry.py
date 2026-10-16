"""The AI-SDLC additions: requirement -> Feature/Epic/Story, mapping, merge, deploy, close.

Everything here runs offline. Jira, GitHub, `oc` and Bob are replaced by fakes,
so what is being tested is the harness's own decisions -- which is the point of
a harness.
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from foreman import (adapter, contract, deploy, intake, machine, mcpcfg, stages,
                     stories, yamlish)


# ───────────────────────────────────────────────────────────────── fakes ──
class FakeJira:
    """Just enough of JiraClient for the stages under test."""
    def __init__(self, done=()):
        self.created = []
        self.done = set(done)
        self.labels = {}
        self.comments = []
        self.state = {}
        self.transitions = []
        self.fail_on = None            # a summary fragment that makes create_issue raise

    def create_issue(self, project, summary, description, issue_type="Task",
                     labels=None, priority="", parent="", extra_fields=None):
        if self.fail_on and self.fail_on in summary:
            raise RuntimeError("jira refused: " + summary)
        key = "TAP-%d" % (len(self.created) + 100)
        self.created.append({"key": key, "summary": summary, "type": issue_type,
                             "labels": list(labels or []), "parent": parent,
                             "extra": extra_fields or {}})
        return {"key": key}

    def is_done(self, key):
        return key in self.done

    def set_labels(self, key, add=None, remove=None):
        cur = self.labels.setdefault(key, set())
        cur.update(add or [])
        cur.difference_update(remove or [])
        return True

    def add_comment(self, key, body):
        self.comments.append((key, body))

    def transition_to(self, key, status):
        self.transitions.append((key, status))
        self.done.add(key)
        return True

    def put_state(self, key, value):
        self.state[key] = dict(value)
        return True


def make_ctx(jira=None, live=True):
    return stages.Context(
        cfg={"jira": {"status_map": {"done": "Done"}}}, jira=jira or FakeJira(),
        store=None, run_id="run-test", live=live,
        paths={"workdir": tempfile.mkdtemp()}, gate_cfg=None,
        base_branch="Release_DEV", test_command="true")


def ticket(**state):
    return {"key": "TAP-1", "summary": "Add a field", "description": "", "labels": [],
            "revision": 3, "fsm": state.get("state", machine.INTAKE), "state": state,
            "status": "", "comments": [], "comment_digest": ""}


def story(t, ac=("works",)):
    return {"story_type": t, "summary": "do " + t, "acceptance_criteria": list(ac)}


def decomposition(*epics, **kw):
    d = {"decomposition": {"understood": True, "epics": list(epics)}}
    d["decomposition"].update(kw)
    return d


def epic(eid="e1", stories_=None, **kw):
    e = {"id": eid, "summary": "Interface " + eid, "interface": "SAP->OMS",
         "stories": stories_ if stories_ is not None else [story("dev")]}
    e.update(kw)
    return e


# ──────────────────────────────────────────────────────────── machine ──
class AI-SDLCRoutes(unittest.TestCase):
    def test_requirement_route_passes_the_epic_gate(self):
        route = [machine.INTAKE, "decomposing", "awaiting-epic-approval",
                 "materialising", "tracking", "done"]
        for a, b in zip(route, route[1:]):
            self.assertTrue(machine.can(a, b), "%s -> %s" % (a, b))
        self.assertFalse(machine.can("decomposing", "materialising"),
                         "nothing may be created in Jira before Gate 1")

    def test_mapping_route_passes_the_mapping_gate(self):
        route = [machine.INTAKE, "mapping", "awaiting-mapping-review",
                 "applying-mapping", "closing", "done"]
        for a, b in zip(route, route[1:]):
            self.assertTrue(machine.can(a, b), "%s -> %s" % (a, b))
        self.assertFalse(machine.can("mapping", "applying-mapping"),
                         "mappings may not be written before Gate 2")

    def test_gate_outcomes(self):
        g = machine.GATES
        self.assertEqual(g["awaiting-epic-approval"]["approve"], "materialising")
        self.assertEqual(g["awaiting-epic-approval"]["changes"], "decomposing")
        self.assertEqual(g["awaiting-mapping-review"]["approve"], "applying-mapping")
        self.assertEqual(g["awaiting-signoff"]["approve"], "merging")

    def test_failed_can_resume_exactly_the_safe_stages(self):
        for s in machine.RESUMABLE:
            self.assertTrue(machine.can("failed", s), s)
        # Re-analysing premises a human has changed is a restart, not a resume.
        self.assertFalse(machine.can("failed", "awaiting-approval"))
        self.assertFalse(machine.can("failed", "awaiting-signoff"))

    def test_stage_for_new_states(self):
        for state, stage in (("decomposing", "decompose"), ("materialising", "materialise"),
                             ("tracking", "track"), ("mapping", "mapping"),
                             ("applying-mapping", "mapping-apply"),
                             ("committed", "codereview"), ("merging", "merge"),
                             ("deploying", "deploy"), ("deployed", "devtest")):
            self.assertEqual(machine.stage_for(state), stage, state)


# ──────────────────────────────────────────────────────────── contract ──
class Decomposition(unittest.TestCase):
    def test_stories_come_out_in_the_fixed_order(self):
        scrambled = [story(t) for t in
                     ("qa", "prodvalidate", "dev", "mapping", "regression",
                      "codereview", "proddeploy")]
        v = contract.validate_decomposition(decomposition(epic(stories_=scrambled)))
        self.assertEqual([s["story_type"] for s in v.data["epics"][0]["stories"]],
                         ["mapping", "dev", "codereview", "regression", "qa",
                          "proddeploy", "prodvalidate"])

    def test_missing_delivery_stories_are_added(self):
        v = contract.validate_decomposition(decomposition(epic()))
        types = [s["story_type"] for s in v.data["epics"][0]["stories"]]
        for t in ("codereview", "regression", "qa", "proddeploy", "prodvalidate"):
            self.assertIn(t, types)
        self.assertTrue(v.corrections)

    def test_pipeline_only_for_a_new_integration(self):
        v = contract.validate_decomposition(
            decomposition(epic(stories_=[story("dev"), story("pipeline")])))
        self.assertNotIn("pipeline", [s["story_type"] for s in v.data["epics"][0]["stories"]])
        v = contract.validate_decomposition(
            decomposition(epic(new_integration=True)))
        self.assertIn("pipeline", [s["story_type"] for s in v.data["epics"][0]["stories"]])

    def test_a_dev_story_is_required(self):
        with self.assertRaises(contract.Rejection) as cm:
            contract.validate_decomposition(
                decomposition(epic(stories_=[story("qa")])))
        self.assertEqual(cm.exception.code, "no_dev_story")

    def test_unknown_story_type_is_dropped_not_guessed(self):
        v = contract.validate_decomposition(decomposition(
            epic(stories_=[story("dev"), story("celebrate")])))
        self.assertTrue(any(r["code"] == "unknown_story_type" for r in v.rejections))

    def test_not_understood_needs_questions(self):
        with self.assertRaises(contract.Rejection):
            contract.validate_decomposition(
                {"decomposition": {"understood": False}})
        v = contract.validate_decomposition(
            {"decomposition": {"understood": False,
                               "clarifications_needed": ["which interface?"]}})
        self.assertFalse(v.data["understood"])

    def test_envelope_and_epics_required(self):
        with self.assertRaises(contract.Rejection):
            contract.validate_decomposition({})
        with self.assertRaises(contract.Rejection):
            contract.validate_decomposition(decomposition())

    def test_impact_analysis_lands_on_the_feature(self):
        v = contract.validate_decomposition(
            decomposition(epic(), impact_analysis="touches the OMS order feed"))
        self.assertIn("touches the OMS order feed", v.data["feature"]["description"])

    def test_epics_are_capped(self):
        v = contract.validate_decomposition(
            decomposition(*[epic("e%d" % n) for n in range(10)]))
        self.assertEqual(len(v.data["epics"]), stories.MAX_EPICS)


class MappingContract(unittest.TestCase):
    def test_changes_are_normalised(self):
        v = contract.validate_mapping({"mapping": {"changes": [
            {"target": "Order/Status", "source": "SAP/Stat", "rule": "map"}]}})
        self.assertEqual(v.data["changes"][0]["action"], "modify")

    def test_nothing_to_review_is_refused(self):
        with self.assertRaises(contract.Rejection):
            contract.validate_mapping({"mapping": {"changes": []}})

    def test_apply_unaccounted_target_counts_as_failed(self):
        v = contract.validate_mapping_applied(
            {"mapping_applied": {"applied": [{"target": "A"}]}}, ["A", "B"])
        self.assertEqual([f["target"] for f in v.data["failed"]], ["B"])


# ───────────────────────────────────────────────────────────── stories ──
class StoryTypes(unittest.TestCase):
    def test_aliases(self):
        for raw, want in (("Mapping Task", "mapping"), ("Dev + Unit Test", "dev"),
                          ("CICD", "pipeline"), ("Code Review", "codereview"),
                          ("Enterprise QA Testing", "qa"),
                          ("Production Deployment", "proddeploy"),
                          ("Production Validations", "prodvalidate"),
                          ("bananas", ""), (None, "")):
            self.assertEqual(stories.normalise_type(raw), want, raw)

    def test_kind_of(self):
        self.assertEqual(stories.kind_of(["foreman"]), "dev")
        self.assertEqual(stories.kind_of(["foreman", "foreman-requirement"]), "requirement")
        self.assertEqual(stories.kind_of(["foreman", "foreman-type-mapping"]), "mapping")
        self.assertEqual(stories.kind_of([]), "dev")

    def test_every_type_has_an_owner(self):
        for t in stories.ORDER:
            self.assertIn(stories.TYPES[t]["owner"], ("foreman", "human", "foreman-closed"))
        self.assertEqual(stories.TYPES["proddeploy"]["owner"], "human")
        self.assertEqual(stories.TYPES["pipeline"]["owner"], "human")


class Materialise(unittest.TestCase):
    def setUp(self):
        self.decomp = contract.validate_decomposition(decomposition(
            epic(stories_=[story("mapping"), story("dev")], new_integration=True))
        ).data
        self.saved = []

    def run_it(self, jira, created=None):
        created = {} if created is None else created
        return stories.materialise(jira, "TAP", "TAP-1", self.decomp, created,
                                   lambda c: self.saved.append(
                                       __import__("copy").deepcopy(c)))

    def test_creates_feature_epic_and_all_stories(self):
        j = FakeJira()
        c = self.run_it(j)
        types = [i["type"] for i in j.created]
        self.assertEqual(types[:2], ["Feature", "Epic"])
        self.assertEqual(types.count("Story"), len(self.decomp["epics"][0]["stories"]))
        self.assertTrue(c["feature"] and c["epics"]["e1"])

    def test_children_use_parent_by_default(self):
        j = FakeJira()
        self.run_it(j)
        epic_issue = next(i for i in j.created if i["type"] == "Epic")
        story_issue = next(i for i in j.created if i["type"] == "Story")
        self.assertTrue(epic_issue["parent"])
        self.assertTrue(story_issue["parent"])

    def test_link_field_replaces_parent_on_data_center(self):
        with mock.patch.dict(os.environ, {"FOREMAN_EPIC_LINK_FIELD": "customfield_10014",
                                          "FOREMAN_FEATURE_LINK_FIELD": "customfield_10018"}):
            j = FakeJira()
            self.run_it(j)
        story_issue = next(i for i in j.created if i["type"] == "Story")
        self.assertEqual(story_issue["parent"], "")
        self.assertIn("customfield_10014", story_issue["extra"])

    def test_dev_is_blocked_until_mapping_and_pipeline_are_done(self):
        j = FakeJira()
        self.run_it(j)
        by = {i["summary"].split("]")[0][1:]: i for i in j.created if i["type"] == "Story"}
        self.assertIn(stories.BLOCKED_LABEL, by["Development & Unit Test"]["labels"])
        self.assertNotIn(stories.INTAKE_LABEL, by["Development & Unit Test"]["labels"])
        self.assertIn(stories.INTAKE_LABEL, by["Mapping"]["labels"])
        # People-owned stories are never picked up by Foreman's intake.
        self.assertNotIn(stories.INTAKE_LABEL, by["Pipeline"]["labels"])
        self.assertNotIn(stories.INTAKE_LABEL, by["Production Deployment"]["labels"])

    def test_retry_after_a_failure_does_not_duplicate(self):
        j = FakeJira()
        j.fail_on = "Production"
        created = {}
        with self.assertRaises(RuntimeError):
            self.run_it(j, created)
        made = len(j.created)
        self.assertGreater(made, 0)
        j.fail_on = None
        self.run_it(j, created)
        # A single-epic Feature legitimately shares the Epic's summary, so
        # compare (type, summary) rather than the summary alone.
        made_ids = [(i["type"], i["summary"]) for i in j.created]
        self.assertEqual(len(made_ids), len(set(made_ids)), "duplicate issues created")
        self.assertGreater(len(j.created), made)
        # ...and a third run changes nothing.
        before = len(j.created)
        self.run_it(j, created)
        self.assertEqual(len(j.created), before)

    def test_progress_is_saved_after_every_call(self):
        j = FakeJira()
        self.run_it(j)
        self.assertEqual(len(self.saved), len(j.created))

    def test_tracking_and_activation(self):
        j = FakeJira()
        created = self.run_it(j)
        tr = stories.build_tracking(self.decomp, created)
        dev = next(k for k, v in tr.items() if v["type"] == "dev")
        mapping = next(k for k, v in tr.items() if v["type"] == "mapping")
        pipeline = next(k for k, v in tr.items() if v["type"] == "pipeline")
        self.assertFalse(tr[dev]["activated"])
        self.assertEqual(sorted(tr[dev]["blocked_by"]), sorted([mapping, pipeline]))
        self.assertEqual(stories.ready_to_activate(tr, lambda k: k == mapping), [])
        self.assertEqual(stories.ready_to_activate(tr, lambda k: k in (mapping, pipeline)),
                         [dev])
        tr[dev]["activated"] = True
        self.assertEqual(stories.ready_to_activate(tr, lambda k: True), [])

    def test_link_children_records_siblings(self):
        j = FakeJira()
        created = self.run_it(j)
        stories.link_children(j, "TAP-1", self.decomp, created)
        dev = created["stories"]["e1/dev"]
        self.assertEqual(j.state[dev]["requirement"], "TAP-1")
        self.assertEqual(j.state[dev]["siblings"]["codereview"],
                         created["stories"]["e1/codereview"])

    def test_tree_markdown_names_who_works_each_story(self):
        md = stories.tree_markdown(self.decomp)
        self.assertIn("Foreman", md)
        self.assertIn("a person", md)


# ──────────────────────────────────────────────────────────── stages ──
class Tracking(unittest.TestCase):
    def tracking(self):
        return {"TAP-2": {"type": "mapping", "owner": "foreman", "blocked_by": [],
                          "activated": True},
                "TAP-3": {"type": "dev", "owner": "foreman", "blocked_by": ["TAP-2"],
                          "activated": False},
                "TAP-4": {"type": "qa", "owner": "human", "blocked_by": [],
                          "activated": True}}

    def test_releases_a_blocked_story_when_its_blocker_is_done(self):
        j = FakeJira(done={"TAP-2"})
        out = stages._track(make_ctx(j), ticket(tracking=self.tracking()))
        self.assertEqual(out.next_state, "tracking")
        self.assertIn(stories.INTAKE_LABEL, j.labels["TAP-3"])

    def test_does_not_release_early(self):
        j = FakeJira()
        stages._track(make_ctx(j), ticket(tracking=self.tracking()))
        self.assertNotIn("TAP-3", j.labels)

    def test_finishes_only_when_everything_is_done(self):
        j = FakeJira(done={"TAP-2", "TAP-3"})
        out = stages._track(make_ctx(j), ticket(tracking=self.tracking()))
        self.assertEqual(out.next_state, "tracking", "TAP-4 is still open")
        j.done.add("TAP-4")
        out = stages._track(make_ctx(j), ticket(tracking=self.tracking()))
        self.assertEqual(out.next_state, "done")

    def test_no_tracking_is_a_failure_not_a_silent_done(self):
        out = stages._track(make_ctx(), ticket())
        self.assertEqual(out.next_state, "failed")


class MergeAndClose(unittest.TestCase):
    def test_local_merge_closes_the_code_review_story_and_moves_to_deploying(self):
        j = FakeJira()
        t = ticket(siblings={"codereview": "TAP-9"}, pr_url="", decision={"author": "ann"})
        with mock.patch.dict(os.environ, {"FOREMAN_GIT_MODE": "local"}):
            out = stages._merge(make_ctx(j), t)
        self.assertEqual(out.next_state, "deploying")
        self.assertIn(("TAP-9", "Done"), j.transitions)
        self.assertTrue(t["state"]["merged_at"])

    def test_dry_run_changes_nothing(self):
        j = FakeJira()
        t = ticket(siblings={"codereview": "TAP-9"})
        out = stages._merge(make_ctx(j, live=False), t)
        self.assertEqual(out.next_state, "deploying")
        self.assertEqual(j.transitions, [])
        self.assertEqual(j.state, {})

    def test_github_merge_without_a_pr_fails_loudly(self):
        t = ticket(pr_url="")
        with mock.patch.dict(os.environ, {"FOREMAN_GIT_MODE": "github"}):
            out = stages._merge(make_ctx(), t)
        self.assertEqual(out.next_state, "failed")

    def test_auto_merge_off_waits_for_a_person(self):
        gh = mock.Mock()
        gh.get_pr.return_value = {"merged": False}
        ctx = make_ctx()
        ctx.gh, ctx.github_repo = gh, "o/r"
        t = ticket(pr_url="https://github.com/o/r/pull/7")
        with mock.patch.dict(os.environ, {"FOREMAN_GIT_MODE": "github",
                                          "FOREMAN_AUTO_MERGE": "false"}):
            out = stages._merge(ctx, t)
        self.assertEqual(out.next_state, "merging")
        gh.merge_pr.assert_not_called()

    def test_already_merged_pr_proceeds_without_merging_again(self):
        gh = mock.Mock()
        gh.get_pr.return_value = {"merged": True, "merge_commit_sha": "abc123def456"}
        ctx = make_ctx()
        ctx.gh, ctx.github_repo = gh, "o/r"
        with mock.patch.dict(os.environ, {"FOREMAN_GIT_MODE": "github"}):
            out = stages._merge(ctx, ticket(pr_url="https://github.com/o/r/pull/7"))
        self.assertEqual(out.next_state, "deploying")
        gh.merge_pr.assert_not_called()

    def test_refused_merge_is_a_failure(self):
        gh = mock.Mock()
        gh.get_pr.return_value = {"merged": False}
        gh.merge_pr.return_value = (False, "branch protection")
        ctx = make_ctx()
        ctx.gh, ctx.github_repo = gh, "o/r"
        with mock.patch.dict(os.environ, {"FOREMAN_GIT_MODE": "github",
                                          "FOREMAN_AUTO_MERGE": "true"}):
            out = stages._merge(ctx, ticket(pr_url="https://github.com/o/r/pull/7"))
        self.assertEqual(out.next_state, "failed")

    def test_close_of_a_requirement_story_raises_no_prod_ticket(self):
        j = FakeJira()
        out = stages._close(make_ctx(j), ticket(requirement="TAP-1", state="closing"))
        self.assertEqual(out.next_state, "done")
        self.assertEqual(j.created, [])
        self.assertIn(("TAP-1", "Done"), j.transitions)

    def test_a_failure_is_persisted_with_where_it_happened(self):
        j = FakeJira()
        ctx = make_ctx(j)
        with mock.patch.dict(os.environ, {"FOREMAN_GIT_MODE": "github"}):
            t = ticket(state="merging")
            out = stages.advance(ctx, t)
        self.assertEqual(out.next_state, "failed")
        self.assertEqual(j.state["TAP-1"]["state"], "failed")
        self.assertEqual(j.state["TAP-1"]["failed_from"], "merging")


class DevTest(unittest.TestCase):
    def test_no_command_is_a_failure(self):
        ctx = make_ctx()
        ctx.test_command = ""
        self.assertEqual(stages._devtest(ctx, ticket()).next_state, "failed")

    def test_dry_run_does_not_execute(self):
        out = stages._devtest(make_ctx(live=False), ticket())
        self.assertEqual(out.next_state, "closing")
        self.assertTrue(out.artifacts.get("dry_run"))


# ───────────────────────────────────────────────────────────── deploy ──
def pod(name, created, ready=True, terminating=False):
    return {"name": name, "uid": name, "created": created, "terminating": terminating,
            "phase": "Running", "ready": ready}


class Rollout(unittest.TestCase):
    SINCE = "2026-01-01T10:00:00Z"

    def test_stale_ready_pods_are_not_a_finished_deploy(self):
        done, msg = deploy.rollout_state([pod("old", "2026-01-01T09:00:00Z")], self.SINCE)
        self.assertFalse(done)
        self.assertIn("before the merge", msg)

    def test_new_ready_pods_are_done(self):
        done, _ = deploy.rollout_state([pod("new", "2026-01-01T10:05:00Z")], self.SINCE)
        self.assertTrue(done)

    def test_terminating_pods_mean_not_done(self):
        done, _ = deploy.rollout_state(
            [pod("new", "2026-01-01T10:05:00Z"),
             pod("old", "2026-01-01T09:00:00Z", terminating=True)], self.SINCE)
        self.assertFalse(done)

    def test_unready_and_empty(self):
        self.assertFalse(deploy.rollout_state(
            [pod("new", "2026-01-01T10:05:00Z", ready=False)], self.SINCE)[0])
        self.assertFalse(deploy.rollout_state([], self.SINCE)[0])

    def test_without_a_merge_time_only_readiness_is_checked(self):
        self.assertTrue(deploy.rollout_state([pod("p", "2020-01-01T00:00:00Z")])[0])

    def test_pod_facts_reads_oc_json(self):
        item = {"metadata": {"name": "a", "uid": "u", "creationTimestamp": "t"},
                "status": {"phase": "Running", "containerStatuses": [{"ready": True},
                                                                      {"ready": False}]}}
        self.assertFalse(deploy._pod_facts(item)["ready"])
        item["status"]["containerStatuses"][1]["ready"] = True
        self.assertTrue(deploy._pod_facts(item)["ready"])
        item["metadata"]["deletionTimestamp"] = "x"
        self.assertTrue(deploy._pod_facts(item)["terminating"])

    def test_wait_needs_two_consecutive_good_reads(self):
        good = [pod("new", "2026-01-01T10:05:00Z")]
        reads = [(good, ""), ([pod("new", "2026-01-01T10:05:00Z", ready=False)], ""),
                 (good, ""), (good, "")]
        with mock.patch.object(deploy, "list_pods", side_effect=reads) as lp, \
                mock.patch.object(deploy.time, "sleep"):
            ok, _ = deploy._wait_for_rollout("ns", "app=x", self.SINCE, 600, poll_sec=0)
        self.assertTrue(ok)
        self.assertEqual(lp.call_count, 4, "a blip must reset the streak")

    def test_wait_times_out(self):
        with mock.patch.object(deploy, "list_pods", return_value=([], "")), \
                mock.patch.object(deploy.time, "sleep"):
            ok, msg = deploy._wait_for_rollout("ns", "app=x", self.SINCE, 0, poll_sec=0)
        self.assertFalse(ok)
        self.assertIn("did not finish", msg)


# ───────────────────────────────────────────────────────────── mcpcfg ──
class McpConfig(unittest.TestCase):
    ENV = {"TAPIFORGE_MCP_URL": "https://tapi.example/mcp",
           "TAPIFORGE_MCP_TOKEN": "s3cr3t-token-value",
           "TAPIFORGE_MCP_READ_TOOLS": "get_mapping,list_mappings",
           "TAPIFORGE_MCP_WRITE_TOOLS": "update_mapping"}

    def clean(self):
        return {k: "" for k in os.environ if k.startswith("TAPIFORGE_")
                or k == "FOREMAN_MAPPING_MODE"}

    def test_unconfigured_means_file_mode(self):
        with mock.patch.dict(os.environ, self.clean()):
            self.assertFalse(mcpcfg.configured())
            self.assertEqual(mcpcfg.mode(), "file")
            self.assertIsNone(mcpcfg.install(tempfile.mkdtemp(), allow_write=False))

    def test_configured_means_mcp_mode_unless_overridden(self):
        with mock.patch.dict(os.environ, dict(self.clean(), **self.ENV)):
            self.assertEqual(mcpcfg.mode(), "mcp")
        with mock.patch.dict(os.environ, dict(self.clean(), **self.ENV,
                                              FOREMAN_MAPPING_MODE="file")):
            self.assertEqual(mcpcfg.mode(), "file")

    def test_write_tools_only_when_allowed(self):
        with mock.patch.dict(os.environ, dict(self.clean(), **self.ENV)):
            ro = mcpcfg.server_entry(allow_write=False)
            rw = mcpcfg.server_entry(allow_write=True)
        self.assertEqual(ro["alwaysAllow"], ["get_mapping", "list_mappings"])
        self.assertIn("update_mapping", rw["alwaysAllow"])

    def test_remote_transport_and_auth_header(self):
        with mock.patch.dict(os.environ, dict(self.clean(), **self.ENV)):
            e = mcpcfg.server_entry(False)
        self.assertEqual(e["httpURL"], "https://tapi.example/mcp")
        self.assertEqual(e["headers"]["Authorization"], "Bearer s3cr3t-token-value")
        with mock.patch.dict(os.environ, dict(self.clean(), **self.ENV,
                                              TAPIFORGE_MCP_TRANSPORT="sse")):
            self.assertIn("url", mcpcfg.server_entry(False))

    def test_local_server(self):
        env = dict(self.clean(), TAPIFORGE_MCP_COMMAND="npx",
                   TAPIFORGE_MCP_ARGS="-y tapiforge-mcp",
                   TAPIFORGE_MCP_ENV="API_KEY=abcdef123456")
        with mock.patch.dict(os.environ, env):
            e = mcpcfg.server_entry(False)
        self.assertEqual(e["command"], "npx")
        self.assertEqual(e["args"], ["-y", "tapiforge-mcp"])
        self.assertEqual(e["env"], {"API_KEY": "abcdef123456"})

    def test_install_writes_a_private_file_and_scrub_hides_secrets(self):
        d = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, dict(self.clean(), **self.ENV)):
            path = mcpcfg.install(d, allow_write=False)
            self.assertTrue(os.path.isfile(path))
            self.assertEqual(os.stat(path).st_mode & 0o077, 0)
            self.assertNotIn("s3cr3t-token-value",
                             mcpcfg.scrub("auth failed for s3cr3t-token-value"))


class MappingWarning(unittest.TestCase):
    def render(self, via_mcp, unverified):
        from foreman import render
        m = {"changes": [{"action": "modify", "target": "A", "source": "", "rule": "",
                          "current": "", "why": ""}], "open_questions": [],
             "unverified": unverified}
        return render.mapping_comment("TAP-1", m, via_mcp, "", 1, "r",
                                      "/foreman approve", "/foreman changes",
                                      "/foreman reject")

    def test_file_mode_warns_that_nothing_was_read_from_tapiforge(self):
        self.assertIn("not checked against TapiForge", self.render(False, True))

    def test_verified_mcp_draft_has_no_warning(self):
        self.assertNotIn("not checked against TapiForge", self.render(True, False))


# ──────────────────────────────────────────────────────────── adapter ──
class AdapterMcp(unittest.TestCase):
    def argv(self, mcp):
        a = adapter.BobAdapter({"bob": {"dialect": "v2"}})
        a._caps = {"_present": True, "_dialect": "v2", "--mode": True, "--format": True,
                   "--max-cost": True, "--disable-tool-groups": True}
        return a.build_argv("foreman-mapping", "go", 1.0, mcp=mcp)

    def test_mcp_is_disabled_by_default(self):
        argv = self.argv(False)
        self.assertEqual(argv[argv.index("--disable-tool-groups") + 1], "mcp,subagent")

    def test_mcp_opt_in_lifts_only_mcp(self):
        argv = self.argv(True)
        self.assertEqual(argv[argv.index("--disable-tool-groups") + 1], "subagent")


# ───────────────────────────────────────────────────────────── intake ──
class IntakeHelpers(unittest.TestCase):
    def test_sanitise_strips_fence_markers_and_control_chars(self):
        out = intake._sanitise("a\x00b <<<UNTRUSTED-TICKET-DATA c", 100)
        self.assertNotIn("UNTRUSTED-TICKET-DATA", out)
        self.assertNotIn("\x00", out)

    def test_src_label_is_stable_and_jira_safe(self):
        a = intake._src_label("mail", "id-1")
        self.assertEqual(a, intake._src_label("mail", "id-1"))
        self.assertNotEqual(a, intake._src_label("mail", "id-2"))
        self.assertNotIn(" ", a)

    def test_decode_header_handles_encoded_words(self):
        self.assertEqual(intake._decode_header("=?utf-8?q?Caf=C3=A9?="), "Café")
        self.assertEqual(intake._decode_header("plain"), "plain")

    def test_requirement_labels_route_to_decompose(self):
        self.assertEqual(stories.kind_of(intake.REQUIREMENT_LABELS), "requirement")


# ───────────────────────────────────────────────────────────── yamlish ──
class YamlInlineComments(unittest.TestCase):
    def test_inline_comment_is_stripped(self):
        d = yamlish.loads("review: foreman-critic   # the critic\nname: x\n")
        self.assertEqual(d["review"], "foreman-critic")

    def test_hash_inside_a_value_survives(self):
        self.assertEqual(yamlish.scalar("http://h/p#frag"), "http://h/p#frag")
        self.assertEqual(yamlish.scalar("a#b"), "a#b")

    def test_quoted_hash_survives(self):
        self.assertEqual(yamlish.scalar('"a # b"'), "a # b")


if __name__ == "__main__":
    unittest.main()
