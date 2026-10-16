"""
Jira Data Center support.

No DC instance is reachable from a test run, so these cover the parts that do
not need one: which endpoint is chosen, which body format is produced, which
auth header is sent, and whether a comment survives the round trip through
wiki markup. The parts that DO need one — that DC actually accepts these
payloads — are listed in docs/PORTABILITY.md as the pre-engagement smoke test.

What is deliberately tested here is the *divergence*. Every assertion below
pairs a Cloud expectation with a DC one, because the failure this guards
against is a change that quietly fixes one deployment by breaking the other.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from foreman import markup, render
from foreman.gate import GateConfig, evaluate, parse_ts
from foreman.jira import CLOUD, DATACENTER, JiraClient


def client(deployment, email="me@example.com", token="tok"):
    # An explicit deployment skips the /serverInfo probe, so no network.
    return JiraClient("jira.example.com", email, token, deployment=deployment)


class DialectSelection(unittest.TestCase):
    def test_rest_version(self):
        self.assertEqual(client(CLOUD).api_version, "3")
        self.assertEqual(client(DATACENTER).api_version, "2")

    def test_endpoint_paths_diverge(self):
        self.assertEqual(client(CLOUD)._api("/myself"), "/rest/api/3/myself")
        self.assertEqual(client(DATACENTER)._api("/myself"), "/rest/api/2/myself")

    def test_body_format_diverges(self):
        self.assertIsInstance(client(CLOUD)._body("# hi"), dict)      # ADF tree
        self.assertIsInstance(client(DATACENTER)._body("# hi"), str)  # wiki markup

    def test_cloud_body_is_a_valid_adf_doc(self):
        doc = client(CLOUD)._body("# hi")
        self.assertEqual(doc["type"], "doc")
        self.assertEqual(doc["version"], 1)

    def test_dc_body_is_wiki_markup(self):
        self.assertTrue(client(DATACENTER)._body("# hi").startswith("h1. hi"))


class Auth(unittest.TestCase):
    def test_email_plus_token_is_basic(self):
        self.assertTrue(client(CLOUD)._auth_header.startswith("Basic "))

    def test_token_alone_is_bearer(self):
        # DC personal access tokens are bearers. Sending them as Basic with an
        # invented username fails with a 401 that blames the username.
        c = client(DATACENTER, email="", token="pat-abc")
        self.assertEqual(c._auth_header, "Bearer pat-abc")


class BaseUrl(unittest.TestCase):
    def test_cloud_host_gets_https(self):
        self.assertEqual(client(CLOUD).base, "https://jira.example.com")

    def test_dc_scheme_port_and_context_path_survive(self):
        # DC is routinely on plain http, a non-standard port and a context
        # path inside a VPN. Normalising any of that away breaks every call.
        c = JiraClient("http://jira.corp:8080/jira", "", "pat",
                       deployment=DATACENTER)
        self.assertEqual(c.base, "http://jira.corp:8080/jira")
        self.assertEqual(c._api("/myself"),
                         "/rest/api/2/myself")


class WikiMarkup(unittest.TestCase):
    def test_headings_lists_and_tables(self):
        w = markup.from_markdown(
            "# T\n\n- a\n- b\n\n| A | B |\n|---|---|\n| 1 | 2 |\n")
        self.assertIn("h1. T", w)
        self.assertIn("* a", w)
        self.assertIn("||A||B||", w)
        self.assertIn("|1|2|", w)

    def test_code_spans_are_not_mangled_into_emphasis(self):
        # Underscores and asterisks are ordinary characters in code and
        # formatting characters in wiki markup. Most quoted Python has both.
        w = markup.from_markdown("Call `do_a_thing(*args)` first.")
        self.assertIn("{{do_a_thing(*args)}}", w)

    def test_code_blocks_keep_their_language(self):
        w = markup.from_markdown("```python\nif a_b: pass\n```")
        self.assertIn("{code:python}", w)
        self.assertIn("if a_b: pass", w)

    def test_panels_become_macros(self):
        self.assertIn("{warning}", markup.from_markdown(":::warning\nx\n:::"))
        self.assertIn("{tip}", markup.from_markdown(":::success\nx\n:::"))

    def test_expand_degrades_to_a_heading_not_a_dropped_section(self):
        # {expand} needs a plugin many DC instances lack. Losing the content
        # would lose the evidence a reviewer is being asked to judge.
        w = markup.from_markdown(":::expand Detail\nthe evidence\n:::")
        self.assertIn("h4. Detail", w)
        self.assertIn("the evidence", w)
        self.assertNotIn("{expand", w)

    def test_links(self):
        self.assertIn("[text|https://x.y]",
                      markup.from_markdown("[text](https://x.y)"))

    def test_oversize_bodies_are_cut_visibly(self):
        out = markup.truncate("x" * 40000, limit=1000)
        self.assertLess(len(out), 1400)
        self.assertIn("truncated", out)


class UniversalReader(unittest.TestCase):
    def test_reads_a_cloud_adf_body(self):
        adf_body = {"type": "doc", "version": 1, "content": [
            {"type": "paragraph",
             "content": [{"type": "text", "text": "/foreman approve"}]}]}
        self.assertIn("/foreman approve", markup.to_text(adf_body))

    def test_reads_a_dc_string_body_and_strips_macros(self):
        self.assertIn("/foreman approve",
                      markup.to_text("{quote}/foreman approve{quote}").strip())

    def test_a_gate_command_is_honoured_on_both_deployments(self):
        # The regression this catches: a DC body arriving with a leading macro
        # so the command is no longer the first text, and every approval on
        # every DC instance is silently ignored.
        cfg = GateConfig(["acct-1"], "/foreman approve", "/foreman reject",
                         "/foreman changes")
        opened = parse_ts("2026-09-20T10:00:00.000+0000")
        now = parse_ts("2026-09-20T12:00:00.000+0000")
        author = {"accountId": "acct-1", "displayName": "Lead"}
        for body in (
            {"type": "doc", "version": 1, "content": [
                {"type": "paragraph",
                 "content": [{"type": "text", "text": "/foreman approve ok"}]}]},
            "/foreman approve ok",
        ):
            r = evaluate([{"id": "1", "created": "2026-09-20T11:00:00.000+0000",
                           "author": author, "body": body}], cfg, opened, now)
            self.assertIsNotNone(r.decision, body)
            self.assertEqual(r.decision.verdict, "approve")


class SharedTemplates(unittest.TestCase):
    def test_one_rendered_comment_converts_to_both_formats(self):
        # render.py emits markdown; the deployment picks the converter. If a
        # template ever grows deployment-specific syntax, this fails.
        md = render.verify_comment(
            "K-1", {"summary": "s", "tests_added": [
                {"path": "tests/test_a.py", "name": "t", "criterion": "c"}]},
            {"passed": True, "command": "pytest -q", "exit_code": 0,
             "duration_sec": 1.0},
            ["it works"], "https://pr", None, 0, "RUN",
            "/foreman approve", "/foreman changes", "/foreman reject")
        wiki = markup.from_markdown(md)
        self.assertIn("h1. Verification", wiki)
        self.assertIn("{tip}", wiki)                 # the pass panel
        self.assertIn("/foreman approve", wiki)

        adf_doc = client(CLOUD)._body(md)
        self.assertEqual(adf_doc["type"], "doc")
        # The marker must survive both conversions, or Foreman loses track of
        # its own comment and starts a parallel thread on every revision.
        self.assertIn(render.marker("verify"), markup.to_text(wiki))
        self.assertIn(render.marker("verify"), markup.to_text(adf_doc))


if __name__ == "__main__":
    unittest.main()
