import importlib.util
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("host_readiness", ROOT / "scripts" / "host_readiness.py")
module = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(module)


class HostReadinessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contract = json.loads((ROOT / "config" / "host-readiness.json").read_text(encoding="utf-8"))

    def test_desktop_ready_with_all_required_capabilities(self):
        evidence = {
            "profile": "desktop-pc24x7",
            "capabilities": {
                "command_gateway": True,
                "session_preflight": True,
                "github_runner": True,
                "runtime_health": True,
                "restart_recovery": True,
            },
        }
        result = module.evaluate(self.contract, "desktop-pc24x7", evidence)
        self.assertTrue(result["ready"])
        self.assertEqual(result["missing_capabilities"], [])

    def test_missing_capability_fails_closed(self):
        evidence = {"profile": "desktop-pc24x7", "capabilities": {"command_gateway": True}}
        result = module.evaluate(self.contract, "desktop-pc24x7", evidence)
        self.assertFalse(result["ready"])
        self.assertIn("github_runner", result["missing_capabilities"])

    def test_profile_mismatch_is_rejected(self):
        evidence = {"profile": "noteri", "capabilities": {}}
        with self.assertRaises(module.ReadinessError):
            module.evaluate(self.contract, "desktop-pc24x7", evidence)

    def test_no_evidence_reports_requirements_without_claiming_ready(self):
        result = module.evaluate(self.contract, "noteri", None)
        self.assertFalse(result["ready"])
        self.assertEqual(result["status"], "needs_evidence")


if __name__ == "__main__":
    unittest.main()
