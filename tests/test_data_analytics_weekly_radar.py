import importlib.util
import json
import os
from unittest.mock import patch
import pathlib
import sys
import tempfile
import unittest
from datetime import datetime, timezone

MODULE = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "data_analytics_weekly_radar.py"
SPEC = importlib.util.spec_from_file_location("data_radar", MODULE)
radar = importlib.util.module_from_spec(SPEC); assert SPEC and SPEC.loader
sys.modules[SPEC.name] = radar; SPEC.loader.exec_module(radar)

class RadarTests(unittest.TestCase):
    def item(self,title,url,product="power_bi",trust="official",summary="",published="2026-09-28T12:00:00+00:00"):
        return {"title":title,"url":url,"source":"Fixture","trust":trust,"product":product,"published":published,"summary":summary}

    def test_feed_normalizes_url_and_date(self):
        xml="""<rss><channel><item><title>Power BI semantic model monitoring automation</title><link>https://example.com/a?utm=x</link><pubDate>Mon, 28 Sep 2026 12:00:00 GMT</pubDate><description>Architecture and observability guidance.</description></item></channel></rss>"""
        src={"name":"Fixture","trust":"official","product":"power_bi"}
        got=radar.feed_items(xml,src)
        self.assertEqual(got[0]["url"],"https://example.com/a"); self.assertTrue(got[0]["published"].startswith("2026-09-28"))

    def test_meta_description_enriches_html_evidence(self):
        raw='<html><head><meta property="og:description" content="Query Store observability guidance for SQL Server."></head></html>'
        self.assertIn("Query Store",radar.page_description(raw))

    def test_history_deduplicates_normalized_url(self):
        issues=[{"title":"[Data Radar] old","body":"- Link: https://example.com/a?utm=x"}]
        self.assertIn("https://example.com/a",radar.history_urls(issues))

    def test_selects_three_to_five_covering_domains(self):
        items=[
            self.item("Power BI semantic model architecture automation","https://e/pbi"),
            self.item("SQL Server Query Store observability performance","https://e/sql","sql_server"),
            self.item("Purview governance lineage data quality","https://e/gov","governance"),
            self.item("Fabric deployment automation architecture","https://e/fabric"),
            self.item("Unrelated company news","https://e/noise"),
        ]
        got=radar.select(items,set()); self.assertGreaterEqual(len(got),3); self.assertLessEqual(len(got),5)
        self.assertNotIn("https://e/noise",{x["url"] for x in got})
        products={x["product"] for x in got}; self.assertTrue({"power_bi","sql_server","governance"}.issubset(products))

    def test_history_item_not_selected(self):
        items=[self.item("Power BI architecture monitoring","https://e/pbi"),self.item("SQL Server Query Store observability","https://e/sql","sql_server"),self.item("Purview governance lineage quality","https://e/gov","governance"),self.item("Fabric deployment automation architecture","https://e/new")]
        got=radar.select(items,{"https://e/pbi"}); self.assertNotIn("https://e/pbi",{x["url"] for x in got}); self.assertEqual(len(got),3)

    def test_fails_closed_under_three(self):
        with self.assertRaises(radar.RadarError): radar.select([self.item("Power BI architecture","https://e/1"),self.item("SQL Server observability","https://e/2","sql_server")],set())

    def test_preview_limitation(self):
        x=radar.classify(self.item("Power BI preview architecture automation","https://e/p")); self.assertIn("prévia",radar.limitation(x))

    def test_week_uses_sao_paulo(self):
        title=radar.issue_title(datetime(2026,10,2,12,0,tzinfo=timezone.utc)); self.assertIn("2026-W40",title)

    def test_replay_short_circuits_without_sources(self):
        now=datetime(2026,10,2,12,0,tzinfo=timezone.utc); title=radar.issue_title(now)
        issue={"title":title,"html_url":"https://example.test/issues/42","body":"# Radar\n\n## 1. A\n## 2. B\n## 3. C\n"}
        old_list,old_collect=radar.issue_list,radar.collect
        try:
            radar.issue_list=lambda repo,token:[issue]
            radar.collect=lambda *a,**k: (_ for _ in ()).throw(AssertionError("must not recollect"))
            with tempfile.TemporaryDirectory() as tmp: result=radar.run("o/r","token",dry_run=False,now=now,output=pathlib.Path(tmp))
            self.assertTrue(result["replay"]); self.assertEqual(result["selected_count"],3); self.assertEqual(result["issue_url"],issue["html_url"])
        finally: radar.issue_list,radar.collect=old_list,old_collect

    def publication_fixture(self, folder, replay=False):
        data={"schema_version":"1.0.0","title":"[Data Radar] 2026-W44 — Power BI, SQL Server e governança de dados",
              "repo":"o/r","issue_url":"https://github.com/o/r/issues/77",
              "head_sha":"a"*40,"run_id":"777","correlation_id":"b"*24,
              "replay":replay,"dry_run":False,"selected_count":3}
        (folder/"report.json").write_text(json.dumps(data),encoding="utf-8")
        return data

    def published_fixture(self, evidence, replay=False):
        mark=chr(96)
        body="# Radar técnico de dados — 2026-W44\n"
        if not replay:
            body+=f"- Run: {mark}777{mark}\n- SHA: {mark}{'a'*40}{mark}\n- Correlation ID: {mark}{'b'*24}{mark}\n"
        return {"html_url":evidence["issue_url"],"title":evidence["title"],"body":body}

    def test_verify_readback_is_read_only_when_issue_list_is_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=pathlib.Path(tmp); evidence=self.publication_fixture(folder)
            with patch.dict(os.environ,{"GITHUB_RUN_ID":"777","GITHUB_SHA":"a"*40},clear=True):
                with patch.object(radar,"gh",return_value=self.published_fixture(evidence)) as api:
                    with patch.object(radar,"issue_list",side_effect=AssertionError("stale issue-list")):
                        with patch.object(radar,"collect",side_effect=AssertionError("no second collection")):
                            output=radar.verify_publication("o/r","token",folder)
                    api.assert_called_once_with("o/r","issues/77","token")
            self.assertEqual(output["verification"],"passed")

    def test_verify_replay_of_preexisting_issue_without_new_post(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=pathlib.Path(tmp); evidence=self.publication_fixture(folder,replay=True)
            with patch.dict(os.environ,{"GITHUB_RUN_ID":"777","GITHUB_SHA":"a"*40},clear=True):
                with patch.object(radar,"gh",return_value=self.published_fixture(evidence,replay=True)) as api:
                    self.assertEqual(radar.verify_publication("o/r","token",folder)["verification"],"passed")
                    api.assert_called_once_with("o/r","issues/77","token")

    def test_verify_fails_closed_for_absent_or_wrong_repositoried_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=pathlib.Path(tmp)
            with patch.dict(os.environ,{"GITHUB_RUN_ID":"777","GITHUB_SHA":"a"*40},clear=True):
                with patch.object(radar,"gh",side_effect=AssertionError("must not use API")):
                    with self.assertRaisesRegex(radar.RadarError,"evidence missing"):
                        radar.verify_publication("o/r","token",folder)
                    data=self.publication_fixture(folder)
                    data["issue_url"]="https://github.com/other/repo/issues/77"
                    (folder/"report.json").write_text(json.dumps(data),encoding="utf-8")
                    with self.assertRaisesRegex(radar.RadarError,"invalid published issue"):
                        radar.verify_publication("o/r","token",folder)

    def test_verify_fails_closed_on_sha_or_correlation_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=pathlib.Path(tmp); data=self.publication_fixture(folder)
            with patch.dict(os.environ,{"GITHUB_RUN_ID":"777","GITHUB_SHA":"a"*40},clear=True):
                with patch.object(radar,"gh",return_value=self.published_fixture(data)) as api:
                    data["head_sha"]="c"*40
                    (folder/"report.json").write_text(json.dumps(data),encoding="utf-8")
                    with self.assertRaisesRegex(radar.RadarError,"current run"):
                        radar.verify_publication("o/r","token",folder)
                    api.assert_not_called()
                    data["head_sha"]="a"*40
                    (folder/"report.json").write_text(json.dumps(data),encoding="utf-8")
                    wrong=self.published_fixture(data)
                    wrong["body"]=wrong["body"].replace("b"*24,"c"*24)
                    api.return_value=wrong
                    with self.assertRaisesRegex(radar.RadarError,"correlation ID"):
                        radar.verify_publication("o/r","token",folder)

    def test_verify_error_preserves_original_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=pathlib.Path(tmp); data=self.publication_fixture(folder)
            data["issue_url"]="https://invalid.example/issues/77"
            (folder/"report.json").write_text(json.dumps(data),encoding="utf-8")
            before=(folder/"report.json").read_text(encoding="utf-8")
            with patch.dict(os.environ,{"GITHUB_RUN_ID":"777","GITHUB_SHA":"a"*40,"GH_TOKEN":"token"},clear=True):
                with patch.object(sys,"argv",["radar","--repo","o/r","--verify-publication","--output-dir",str(folder)]):
                    self.assertEqual(radar.main(),1)
            self.assertEqual((folder/"report.json").read_text(encoding="utf-8"),before)
            self.assertEqual(json.loads((folder/"verification.json").read_text())["status"],"blocked")

    def test_workflow_uses_read_only_replay_and_single_publisher_group(self):
        data=(pathlib.Path(__file__).resolve().parents[1]/".github"/"workflows"/"data-analytics-weekly-radar.yml").read_text(encoding="utf-8")
        replay=data.split("- name: Replay publication to prove idempotence",1)[1].split("- name: Upload radar evidence",1)[0]
        self.assertIn("--verify-publication",replay)
        self.assertIn("|| 'publisher'",data)
        self.assertIn("cancel-in-progress: false",data)

    def test_run_repeat_uses_receipt_and_skips_stale_listing(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=pathlib.Path(tmp); data=self.publication_fixture(folder)
            with patch.dict(os.environ,{"GITHUB_RUN_ID":"777","GITHUB_SHA":"a"*40},clear=True):
                with patch.object(radar,"gh",return_value=self.published_fixture(data)) as api:
                    with patch.object(radar,"issue_list",side_effect=AssertionError("must not list")):
                        result=radar.run("o/r","token",dry_run=False,output=folder)
                    api.assert_called_once_with("o/r","issues/77","token")
            self.assertTrue(result["replay"])
            self.assertEqual(result["issue_url"],data["issue_url"])

    def test_run_repeat_invalid_receipt_fails_closed_before_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=pathlib.Path(tmp)
            (folder/"report.json").write_text("{invalid-json",encoding="utf-8")
            with patch.dict(os.environ,{"GITHUB_RUN_ID":"777","GITHUB_SHA":"a"*40},clear=True):
                with patch.object(radar,"gh",side_effect=AssertionError("no API")):
                    with patch.object(radar,"issue_list",side_effect=AssertionError("no issue listing")):
                        with self.assertRaisesRegex(radar.RadarError,"do not republish"):
                            radar.run("o/r","token",dry_run=False,output=folder)

    def test_artifacts_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=pathlib.Path(tmp); radar.write_artifacts(p,{"selected_count":0},"# report\n")
            self.assertTrue((p/"report.json").exists()); self.assertEqual((p/"report.md").read_text(),"# report\n")

if __name__ == "__main__": unittest.main()
