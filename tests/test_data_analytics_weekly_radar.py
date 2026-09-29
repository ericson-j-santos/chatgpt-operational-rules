import importlib.util
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

    def test_artifacts_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=pathlib.Path(tmp); radar.write_artifacts(p,{"selected_count":0},"# report\n")
            self.assertTrue((p/"report.json").exists()); self.assertEqual((p/"report.md").read_text(),"# report\n")

if __name__ == "__main__": unittest.main()
