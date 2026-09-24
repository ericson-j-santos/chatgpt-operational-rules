import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "scripts/configure_report_builder_main_protection_risk3.py"
RUNNER = ROOT / "scripts/run_report_builder_main_protection_local.py"
EXPECTED_MAIN_SHA = "b80c999d83a82b3454bb6c486e16129f4f04f434"


class ReportBuilderProtectionOpsTests(unittest.TestCase):
    def test_exact_risk3_contract(self) -> None:
        raw = CONFIG.read_text(encoding="utf-8")
        self.assertIn('ACTION_ID = "reqsys.report-builder-main-protection.dev"', raw)
        self.assertIn(
            'SCOPE = "repo://ericson-j-santos/report-builder-platform/branch/main"',
            raw,
        )
        self.assertIn('"scripts/run_report_builder_main_protection_local.py"', raw)
        self.assertIn("DEFAULT_TTL_MINUTES = 30", raw)
        self.assertNotIn("--token", raw)
        self.assertNotIn("--secret", raw)

    def test_executor_is_fail_closed_and_bound_to_post_merge_main(self) -> None:
        raw = RUNNER.read_text(encoding="utf-8")
        self.assertIn(
            'TARGET_REPOSITORY = "ericson-j-santos/report-builder-platform"',
            raw,
        )
        self.assertIn('TARGET_BRANCH = "main"', raw)
        self.assertIn(f'EXPECTED_MAIN_SHA = "{EXPECTED_MAIN_SHA}"', raw)
        self.assertIn('"Quality / Python 3.11"', raw)
        self.assertIn('"Quality / Python 3.14"', raw)
        self.assertIn('env.pop("GH_TOKEN", None)', raw)
        self.assertIn('env.pop("GITHUB_TOKEN", None)', raw)
        self.assertIn('"target_main_sha_changed"', raw)
        self.assertIn('"required_checks_not_green"', raw)
        self.assertIn('"required_checks_regressed_before_write"', raw)
        self.assertIn('"target_sha_changed_before_write"', raw)
        self.assertIn('"target_sha_changed_after_write"', raw)
        self.assertIn('"branch_protection_update_failed"', raw)
        self.assertIn('"PROTECTION_APPLIED"', raw)
        self.assertIn('"ALREADY_COMPLIANT"', raw)
        self.assertIn('"secret_value_exposed": False', raw)


if __name__ == "__main__":
    unittest.main()
