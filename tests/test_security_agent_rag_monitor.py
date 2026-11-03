"""Offline safety, filtering, replay and publication tests for AI/RAG monitor."""
import importlib.util
import os
from pathlib import Path
from unittest import TestCase, main
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / "scripts" / "security_agent_rag_monitor.py"
SPEC = importlib.util.spec_from_file_location("ai_security_watch", PATH)
watch = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(watch)
NOW = watch.date("2026-11-04T14:00:00Z")

def entry(title="AI agent prompt injection security vulnerability", date="2026-11-04T09:00:00Z"):
    return {"source":"CISA", "url":"https://www.cisa.gov/news-events/a",
            "title":title, "description":"AI agent prompt injection vulnerability mitigations",
            "published":watch.date(date), "kind":"guidance"}

class WatchTests(TestCase):
    def test_positive_material_control(self):
        self.assertIn("injeção de instruções", watch.material(entry(), NOW))

    def test_generic_security_news_is_ignored(self):
        item = entry("Security guidance for Windows servers")
        item["description"] = "General OS security guidance."
        self.assertEqual(watch.material(item, NOW), ())

    def test_unknown_date_and_baseline_are_ignored(self):
        old = entry(date="2026-11-02T23:59:00Z")
        self.assertEqual(watch.material(old, NOW), ())
        old["published"] = None
        self.assertEqual(watch.material(old, NOW), ())

    def test_future_date_ignored(self):
        self.assertEqual(watch.material(entry(date="2026-11-06T00:00:00Z"), NOW), ())

    def test_release_not_security_ignored(self):
        item = entry("New LLM Top 10 design")
        item["description"] = "New presentation format and graphics"
        self.assertEqual(watch.material(item, NOW), ())

    def test_rss_positive(self):
        xml = b'<rss><channel><item><title>AI agent prompt injection security guidance</title><link>https://www.cisa.gov/a</link><pubDate>Wed, 04 Nov 2026 09:00:00 GMT</pubDate><description>Mitigation against prompt injection</description></item></channel></rss>'
        self.assertEqual(len(watch.rss_items(xml, "CISA")), 1)
        self.assertTrue(watch.material(watch.rss_items(xml, "CISA")[0], NOW))

    def test_invalid_rss_fails_closed(self):
        with self.assertRaises(watch.MonitorBlocked):
            watch.rss_items(b"<rss", "CISA")

    def test_unofficial_url_rejected(self):
        for url in ("http://www.cisa.gov/a", "https://evil.test/p", "https://github.com.evil.test/a"):
            with self.assertRaises(watch.MonitorBlocked):
                watch.canonical(url)

    def test_untrusted_mentions_sanitized(self):
        self.assertNotIn("@victim", watch.safe("<b>Alert</b> @victim"))

    def test_published_advisory_has_severity(self):
        data = b'[{"state":"published","html_url":"https://github.com/ollama/ollama/security/advisories/GHSA-abc", "summary":"Ollama prompt injection security vulnerability", "description":"Prompt injection exfiltration risk", "published_at":"2026-11-04T00:00:00Z", "severity":"high","ghsa_id":"GHSA-abc"}]'
        parsed = watch.advisory_items(data, "Ollama")
        self.assertEqual(parsed[0]["severity"], "high")
        self.assertTrue(watch.material(parsed[0], NOW))

    def test_draft_release_ignored(self):
        data = b'[{"draft":true,"prerelease":false,"html_url":"https://github.com/a/b/releases/tag/x"}]'
        self.assertEqual(watch.release_items(data, "OWASP GenAI"), [])

    def test_alert_identity_stable(self):
        item = entry()
        self.assertEqual(watch.alert_title(item), watch.alert_title(item))
        self.assertIn(watch.PREFIX, watch.alert_title(item))

    def test_headline_edit_does_not_create_a_new_alert_identity(self):
        original = entry()
        renamed = {**original, "title": "Revised official headline"}
        self.assertEqual(watch.alert_title(original), watch.alert_title(renamed))

    def test_external_source_request_does_not_forward_repository_token(self):
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self, size):
                return b"[]"
        with patch.object(watch, "urlopen", return_value=Response()) as mocked:
            self.assertEqual(
                watch.source_request("https://api.github.com/repos/ollama/ollama/releases", "private-token"),
                b"[]",
            )
            request = mocked.call_args.args[0]
            self.assertIsNone(request.get_header("Authorization"))

    def test_publication_rejects_non_actions_context(self):
        with patch.dict(os.environ, {"GITHUB_ACTIONS":"false","GITHUB_EVENT_NAME":"schedule",
                                      "GITHUB_RUN_ATTEMPT":"1","GITHUB_SHA":"a"*40,"GITHUB_RUN_ID":"123"}):
            with self.assertRaises(watch.MonitorBlocked):
                watch.publication_guard()

    def test_replay_does_not_publish(self):
        item = entry()
        title = watch.alert_title(item)
        env = {"GITHUB_ACTIONS":"true", "GITHUB_EVENT_NAME":"schedule",
               "GITHUB_RUN_ATTEMPT":"1", "GITHUB_SHA":"a"*40,"GITHUB_RUN_ID":"123"}
        with patch.dict(os.environ, env), patch.object(watch, "collect", return_value=([item], [{"source":"CISA","status":"ok"}])), patch.object(watch, "existing_titles", return_value={title}), patch.object(watch, "gh") as api:
            result = watch.execute(watch.REPO, "fake", NOW)
            self.assertEqual(result["issues_created"], 0)
            api.assert_not_called()

    def test_positive_issue_and_readback(self):
        item = entry()
        title, body = watch.alert_title(item), watch.report(item, watch.material(item, NOW), "a"*40)
        def api(repo, suffix, token, **kw):
            if suffix == "issues" and kw.get("method") == "POST":
                return {"number": 7}
            if suffix == "issues/7":
                return {"title": title, "body": body, "html_url":"https://github.com/example/7"}
            raise AssertionError(suffix)
        env = {"GITHUB_ACTIONS":"true","GITHUB_EVENT_NAME":"schedule",
               "GITHUB_RUN_ATTEMPT":"1","GITHUB_SHA":"a"*40,"GITHUB_RUN_ID":"123"}
        with patch.dict(os.environ, env), patch.object(watch, "collect", return_value=([item], [{"source":"CISA","status":"ok"}])), patch.object(watch, "existing_titles", return_value=set()), patch.object(watch, "gh", side_effect=api):
            self.assertEqual(watch.execute(watch.REPO, "fake", NOW)["issues_created"], 1)

    def test_same_official_item_twice_in_one_cycle_creates_one_issue(self):
        item = entry()
        title, body = watch.alert_title(item), watch.report(item, watch.material(item, NOW), "a"*40)
        def api(repo, suffix, token, **kw):
            if suffix == "issues" and kw.get("method") == "POST":
                return {"number": 9}
            if suffix == "issues/9":
                return {"title": title, "body": body, "html_url": "https://github.com/example/9"}
            raise AssertionError(suffix)
        env = {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "schedule",
               "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": "a"*40, "GITHUB_RUN_ID": "123"}
        with patch.dict(os.environ, env), patch.object(watch, "collect",
                return_value=([item, item], [{"source": "CISA", "status": "ok"}])), patch.object(
                watch, "existing_titles", return_value=set()), patch.object(watch, "gh", side_effect=api) as mocked:
            result = watch.execute(watch.REPO, "fake", NOW)
            self.assertEqual(result["issues_created"], 1)
            self.assertEqual(mocked.call_count, 2)

    def test_ambiguous_post_has_no_retry(self):
        item = entry()
        env = {"GITHUB_ACTIONS":"true","GITHUB_EVENT_NAME":"schedule",
               "GITHUB_RUN_ATTEMPT":"1","GITHUB_SHA":"a"*40,"GITHUB_RUN_ID":"123"}
        with patch.dict(os.environ, env), patch.object(watch, "collect", return_value=([item], [{"source":"CISA","status":"ok"}])), patch.object(watch, "existing_titles", return_value=set()), patch.object(watch, "gh", return_value={} ) as api:
            with self.assertRaises(watch.MonitorBlocked):
                watch.execute(watch.REPO, "fake", NOW)
            self.assertEqual(api.call_count, 1)

    def test_issue_pagination_failure_fails_closed(self):
        with patch.object(watch, "gh", return_value="invalid"):
            with self.assertRaises(watch.MonitorBlocked):
                watch.existing_titles(watch.REPO, "fake")

if __name__ == "__main__":
    main()
