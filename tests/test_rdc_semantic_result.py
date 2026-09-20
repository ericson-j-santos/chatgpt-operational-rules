from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import rdc_semantic_result as rsr


class RdcSemanticResultTests(unittest.TestCase):
    def test_exit_zero_does_not_override_no_callback(self) -> None:
        result = rsr.evaluate({
            "command_exit_code": 0,
            "gateway_exit_code": 0,
            "stdout": "RESULT=NO_CALLBACK",
        })
        self.assertTrue(result["technical_ok"])
        self.assertFalse(result["semantic_ok"])
        self.assertIn("failure_marker:NO_CALLBACK", result["reasons"])

    def test_empty_matched_is_functional_failure(self) -> None:
        result = rsr.evaluate({"command_exit_code": 0, "matched": []})
        self.assertFalse(result["semantic_ok"])
        self.assertIn("semantic_empty:matched", result["reasons"])

    def test_empty_controls_is_functional_failure(self) -> None:
        result = rsr.evaluate({"gateway_exit_code": 0, "controls": []})
        self.assertFalse(result["semantic_ok"])
        self.assertIn("semantic_empty:controls", result["reasons"])

    def test_plain_result_ok_is_unproven(self) -> None:
        result = rsr.evaluate({"result": "ok", "command_exit_code": 0})
        self.assertFalse(result["semantic_ok"])
        self.assertIn("semantic_evidence_missing", result["reasons"])

    def test_transport_proof_is_semantic_success(self) -> None:
        result = rsr.evaluate({
            "command_exit_code": 0,
            "transport_proven": True,
            "ready": True,
        })
        self.assertTrue(result["semantic_ok"])
        self.assertEqual(result["classification"], "SEMANTIC_OK")

    def test_required_effect_must_be_proven(self) -> None:
        result = rsr.evaluate(
            {"command_exit_code": 0, "submitted": True, "callback_received": False},
            required=("submitted", "callback_received"),
        )
        self.assertFalse(result["semantic_ok"])
        self.assertIn("required_not_proven:callback_received", result["reasons"])

    def test_nonzero_exit_is_failure_even_with_ready_flag(self) -> None:
        result = rsr.evaluate({"command_exit_code": 9, "ready": True})
        self.assertFalse(result["semantic_ok"])
        self.assertIn("technical_exit_nonzero:command_exit_code=9", result["reasons"])


if __name__ == "__main__":
    unittest.main()
