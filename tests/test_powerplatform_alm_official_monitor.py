import importlib.util
import pathlib
import unittest


MODULE_PATH = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "powerplatform_alm_official_monitor.py"
SPEC = importlib.util.spec_from_file_location("alm_monitor", MODULE_PATH)
monitor = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(monitor)


class PowerPlatformAlmMonitorTests(unittest.TestCase):
    def test_front_matter_only_change_is_ignored(self):
        before = "---\nms.date: 2026-09-01\n---\n# Pipelines\nSame body.\n"
        after = "---\nms.date: 2026-09-29\n---\n# Pipelines\nSame body.\n"
        self.assertEqual(monitor.changed_content_lines(before, after), [])

    def test_editorial_change_is_not_material(self):
        lines = [
            "Power Platform pipelines help makers deploy solutions.",
            "Power Platform pipelines help makers deploy solution artifacts.",
        ]
        self.assertFalse(monitor.is_material_doc_change(lines))

    def test_managed_environment_requirement_is_material(self):
        lines = ["All target environments must be enabled as managed environments."]
        self.assertTrue(monitor.is_material_doc_change(lines))

    def test_connection_reference_breaking_rule_is_material(self):
        lines = [
            "Connection references without a value in the solution can no longer be updated during deployment."
        ]
        self.assertTrue(monitor.is_material_doc_change(lines))

    def test_roadmap_scope_requires_product_and_alm_topic(self):
        scoped = {
            "title": "Power Platform pipelines deployment update",
            "description": "Connection references in target environments are validated.",
            "tagsContainer": {
                "products": [{"tagName": "Microsoft Power Platform governance and administration"}]
            },
        }
        unrelated = {
            "title": "New document formatting",
            "description": "Formatting update.",
            "tagsContainer": {"products": [{"tagName": "Microsoft Word"}]},
        }
        self.assertTrue(monitor.roadmap_is_in_scope(scoped))
        self.assertFalse(monitor.roadmap_is_in_scope(unrelated))

    def test_roadmap_unchanged_has_no_event(self):
        state = {
            "123": {
                "title": "Power Platform pipelines",
                "description_hash": "a",
                "description_excerpt": "pipeline",
                "status": "In development",
                "publicRoadmapStatus": "",
                "publicDisclosureAvailabilityDate": "",
                "publicPreviewDate": "",
                "releasePhase": [],
            }
        }
        self.assertEqual(monitor.roadmap_events(state, state), [])

    def test_roadmap_change_creates_one_event(self):
        before = {
            "123": {
                "title": "Power Platform pipelines",
                "description_hash": "a",
                "description_excerpt": "pipeline",
                "status": "In development",
                "publicRoadmapStatus": "",
                "publicDisclosureAvailabilityDate": "",
                "publicPreviewDate": "",
                "releasePhase": [],
            }
        }
        after = {
            "123": {
                **before["123"],
                "status": "Rolling out",
            }
        }
        events = monitor.roadmap_events(before, after)
        self.assertEqual(len(events), 1)
        self.assertIn("status", events[0]["evidence"])

    def test_state_round_trip(self):
        state = {"schema": 1, "docs": {"pipelines": {"commit": "a" * 40}}, "roadmap": {}}
        self.assertEqual(monitor.parse_state(monitor.state_body(state)), state)

    def test_alert_title_is_stable_across_retries(self):
        events = [
            {
                "kind": "documentation",
                "source": "pipelines",
                "url": "https://example.invalid",
                "evidence": "a -> b",
                "subject": "pipelines",
                "risk": "alto",
                "effect": "x",
                "adaptation": "y",
                "details": ["z"],
            }
        ]
        first = monitor.alert_title(events)
        second = monitor.alert_title(events)
        self.assertEqual(first, second)
        self.assertRegex(first, r"^\[ALM Monitor\].*-[0-9a-f]{12}$")


if __name__ == "__main__":
    unittest.main()
