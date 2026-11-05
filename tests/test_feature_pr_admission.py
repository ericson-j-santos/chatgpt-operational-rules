"""Strict positive/negative preclassification. Does not certify external evidence."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import importlib.util
import unittest

MODULE = Path(__file__).resolve().parents[1] / "scripts/feature_pr_preclassification.py"
SPEC = importlib.util.spec_from_file_location("feature_pr_preclassification", MODULE)
assert SPEC and SPEC.loader
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)


def payload(kind="feature", source_mode="assisted"):
    return {
        "contract": m.CONTRACT,
        "kind": kind,
        "issue_declared_kind": kind,
        "kind_locked_before_execution": True,
        "capability_absent_at_base": kind == "feature",
        "baseline_failure_reproduced": kind == "repair",
        "prior_repair_blocked": False,
        "source_mode": source_mode,
        "model_calls": 1 if source_mode == "model_builder" else 0,
        "model_digest": "f"*64 if source_mode == "model_builder" else None,
        "base_sha": "a"*40,
        "current_base_sha": "a"*40,
    }


class FeatureRepairClassificationTests(unittest.TestCase):
    def blocked(self, changed, reason):
        with self.assertRaisesRegex(m.ClassificationBlocked, "^" + reason + "$"):
            m.preclassify(changed)

    def test_feature_assisted_positive_no_model_claim(self):
        result=m.preclassify(payload())
        self.assertEqual(result["kind"], "feature")
        self.assertFalse(result["ready_for_pr"])
        self.assertFalse(result["independent_evidence_verified"])
        self.assertFalse(result["host_execution_authorized"])

    def test_feature_model_positive_with_digest(self):
        self.assertEqual(m.preclassify(payload(source_mode="model_builder"))["kind"], "feature")

    def test_repair_real_model_positive(self):
        self.assertEqual(m.preclassify(payload(kind="repair",source_mode="model_builder"))["kind"], "repair")

    def test_repair_without_model_blocked(self):
        self.blocked(payload(kind="repair"), "repair_requires_real_model_failure")

    def test_fake_failing_feature_blocked(self):
        p=payload();p["baseline_failure_reproduced"]=True
        self.blocked(p,"feature_baseline_invalid")

    def test_feature_without_absence_blocked(self):
        p=payload();p["capability_absent_at_base"]=False
        self.blocked(p,"feature_baseline_invalid")

    def test_reclassification_after_blocked_repair_forbidden(self):
        p=payload();p["prior_repair_blocked"]=True
        self.blocked(p,"repair_reclassification_forbidden")

    def test_kind_change_after_issue_rejected(self):
        p=payload();p["issue_declared_kind"]="repair"
        self.blocked(p,"kind_mismatch")

    def test_kind_lock_missing(self):
        p=payload();p["kind_locked_before_execution"]=False
        self.blocked(p,"kind_lock_unverified")

    def test_invalid_kind(self):
        p=payload();p["kind"]="unknown"
        self.blocked(p,"kind_mismatch")

    def test_model_call_without_digest(self):
        p=payload(source_mode="model_builder");p["model_digest"]=None
        self.blocked(p,"model_provenance_missing")

    def test_assisted_cannot_claim_model(self):
        p=payload();p["model_calls"]=1
        self.blocked(p,"model_claim_mismatch")

    def test_bool_is_not_integer_model_calls(self):
        p=payload();p["model_calls"]=True
        self.blocked(p,"calls_invalid")

    def test_moved_base_rejected(self):
        p=payload();p["current_base_sha"]="b"*40
        self.blocked(p,"base_moved")

    def test_non_sha_rejected(self):
        p=payload();p["base_sha"]="main"
        self.blocked(p,"sha_invalid")

    def test_injected_extra_field_rejected(self):
        p=payload();p["ready_for_pr"]=True
        self.blocked(p,"classification_schema_invalid")

    def test_missing_field_rejected(self):
        p=payload();del p["current_base_sha"]
        self.blocked(p,"classification_schema_invalid")

    def test_replay_is_deterministic_and_stays_not_ready(self):
        p=payload()
        result1=m.preclassify(deepcopy(p))
        result2=m.preclassify(deepcopy(p))
        self.assertEqual(result1,result2)
        self.assertIs(result2["ready_for_merge"],False)

    def test_source_or_schema_cannot_claim_authority(self):
        p=payload();p["source_mode"]="unknown"
        self.blocked(p,"source_mode_invalid")

    def test_solo_maintainer_review_keeps_independent_gates(self):
        rule=(Path(__file__).resolve().parents[1]/"rules/github-workflow.md").read_text(encoding="utf-8")
        for invariant in (
            "required_approving_review_count=0",
            "INDEPENDENT_TECHNICAL_REVIEW_PENDING",
            "OWNER_DECISION_PENDING",
            "READY_FOR_MERGE=false",
            "INDEPENDENT_HUMAN_APPROVAL",
            "SHA-bound",
            "BLOQUEAR",
            "Validator",
        ):
            with self.subTest(invariant=invariant):
                self.assertIn(invariant,rule)

    def test_rules_repair_requirement_remains_present(self):
        root=Path(__file__).resolve().parents[1]
        rule=(root/"rules/agent-durable-pr-gate.md").read_text(encoding="utf-8")
        feature=(root/"rules/feature-pr-admission.md").read_text(encoding="utf-8")
        self.assertIn("reparo real",rule)
        self.assertIn("Validator independente",rule)
        self.assertIn("READY_FOR_PR",rule)
        self.assertIn("`repair`",feature)
        self.assertIn("`feature`",feature)
        self.assertIn("não autentica",feature)


if __name__ == "__main__":
    unittest.main()
