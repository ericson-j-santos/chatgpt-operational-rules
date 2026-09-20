from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import owner_prelogin_admin_once as opa


class PreloginAdminOnceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.plan = self.root / "plan.json"
        self.result = self.root / "result.json"
        self.audit = self.root / "audit.jsonl"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def common_patches(self):
        return (
            patch.object(opa, "plan_path", return_value=self.plan),
            patch.object(opa, "result_path", return_value=self.result),
            patch.object(opa, "audit_path", return_value=self.audit),
            patch.object(opa, "owner_fingerprint", return_value="fingerprint"),
            patch.object(opa, "current_head", return_value="a" * 40),
            patch.object(opa, "require_clean_tracked"),
            patch.object(opa.socket, "gethostname", return_value="Noteri"),
        )

    def test_authorization_is_bound_to_host_head_and_expiry(self) -> None:
        p1, p2, p3, p4, p5, p6 = self.common_patches()
        with p1, p2, p3, p4, p5, p6:
            result = opa.authorize(
                host="Noteri",
                minutes=5,
                authorization_ref="chat-explicit-admin",
                confirm=opa.AUTHORIZE_CONFIRM,
            )
        payload = json.loads(self.plan.read_text(encoding="utf-8"))
        self.assertEqual(result["action_id"], opa.ACTION_ID)
        self.assertEqual(payload["host"], "Noteri")
        self.assertEqual(payload["owner_fingerprint"], "fingerprint")
        self.assertEqual(payload["rules_head"], "a" * 40)
        self.assertIsNone(payload["submitted_at"])
        self.assertIsNone(payload["consumed_at"])
        self.assertEqual(len(payload["authorization_ref_sha256"]), 64)

    def test_consumed_authorization_fails_closed(self) -> None:
        p1, p2, p3, p4, p5, p6 = self.common_patches()
        with p1, p2, p3, p4, p5, p6:
            opa.authorize(
                host="Noteri",
                minutes=5,
                authorization_ref="chat-explicit-admin",
                confirm=opa.AUTHORIZE_CONFIRM,
            )
            payload = json.loads(self.plan.read_text(encoding="utf-8"))
            payload["consumed_at"] = opa.utc_iso()
            self.plan.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(opa.PreloginAdminError, "already consumed"):
                opa.load_plan(opa.ACTION_ID)

    def test_execute_requires_elevated_token(self) -> None:
        with patch.object(opa, "is_windows_admin", return_value=False):
            with self.assertRaisesRegex(opa.PreloginAdminError, "elevated token"):
                opa.execute_elevated(
                    action_id=opa.ACTION_ID,
                    confirm=opa.EXECUTE_CONFIRM,
                )

    def test_elevated_execute_consumes_before_mutation_and_runs_exact_installers(self) -> None:
        plan = {
            "host": "Noteri",
            "rules_head": "a" * 40,
            "submitted_at": opa.utc_iso(),
            "consumed_at": None,
        }
        events: list[str] = []

        fake_arbitration = types.SimpleNamespace(
            apply=lambda: events.append("arbitration") or {"result": "ok"}
        )
        fake_resilience = types.SimpleNamespace(
            HEADLESS_CONFIRM="INSTALL-RDC-HEADLESS",
            apply=lambda seconds, headless, confirm: (
                events.append("headless") or {"result": "ok"}
            ),
        )

        def fake_atomic(path: Path, payload: dict) -> None:
            if path == self.plan and payload.get("consumed_at"):
                events.append("consumed")
            if path == self.result:
                events.append("result")

        with (
            patch.object(opa, "is_windows_admin", return_value=True),
            patch.object(opa, "load_plan", return_value=plan),
            patch.object(opa, "require_clean_tracked"),
            patch.object(opa, "plan_path", return_value=self.plan),
            patch.object(opa, "result_path", return_value=self.result),
            patch.object(opa, "atomic_json", side_effect=fake_atomic),
            patch.object(opa, "append_audit"),
            patch.object(
                opa,
                "install_orchestrator_s4u",
                side_effect=lambda: events.append("orchestrator") or {"result": "ok"},
            ),
            patch.dict(
                sys.modules,
                {
                    "rdc_owner_arbitration": fake_arbitration,
                    "rdc_task_resilience": fake_resilience,
                },
            ),
        ):
            result = opa.execute_elevated(
                action_id=opa.ACTION_ID,
                confirm=opa.EXECUTE_CONFIRM,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(
            events[:4],
            ["consumed", "arbitration", "headless", "orchestrator"],
        )
        self.assertEqual(events[-1], "result")

    def test_revoke_removes_plan(self) -> None:
        self.plan.write_text("{}", encoding="utf-8")
        with (
            patch.object(opa, "plan_path", return_value=self.plan),
            patch.object(opa, "audit_path", return_value=self.audit),
        ):
            result = opa.revoke(opa.REVOKE_CONFIRM)
        self.assertTrue(result["revoked"])
        self.assertFalse(self.plan.exists())


if __name__ == "__main__":
    unittest.main()
