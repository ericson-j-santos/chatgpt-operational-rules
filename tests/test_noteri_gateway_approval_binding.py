"""Testes da passagem tipada de aprovacao; nenhum subprocesso ou host real."""
from __future__ import annotations

import dataclasses
import unittest
from unittest import mock

from scripts import noteri_gateway_approval_binding as m

SHA = "a" * 40
RID = 123456


def facts(**overrides):
    data = {
        "session_id": "noteri-session-01",
        "correlation_id": "noteri-approval-01",
        "source_repo": m.REPOSITORY,
        "expected_head": m.MAINTENANCE_SHA,
        "task_name": m.TASK,
        "risk": 2,
        "mode": "apply",
        "command_confirm": m.CONFIRM,
        "state_validated": True,
        "session_launch_ok": True,
        "clean_worktree": True,
        "exclusive_lease": True,
        "fencing_token": 9,
        "session_snapshot_sha256": "b" * 64,
        "policy_sha256": "c" * 64,
        "zero_incremental_cost_verified": True,
    }
    data.update(overrides)
    return m.TrustedGatewayFacts(**data)


def policy(**overrides):
    data = {
        m.POLICY_FLAG: True,
        "noteri_approval_workflow_sha": SHA,
        "noteri_maintenance_exact_sha": m.MAINTENANCE_SHA,
    }
    data.update(overrides)
    return data


def valid_review():
    return {
        "result": "REVIEW_PROVENANCE_VERIFIED",
        "review_provenance_verified": True,
        "execution_authorized": False,
        "run_id": RID,
        "workflow_sha": SHA,
        "maintenance_sha": m.MAINTENANCE_SHA,
    }


class ApprovalBindingTests(unittest.TestCase):
    def setUp(self):
        self.patches = [
            mock.patch.object(m, "_host_matches", return_value=True),
            mock.patch.object(m.review, "consultar_evidencia_github", return_value=({}, [], {})),
            mock.patch.object(m.review, "validar_evidencia", return_value=valid_review()),
            mock.patch.object(m.gateway, "secure_state_directory", return_value=None),
            mock.patch.object(m.gateway, "secure_state_path", return_value="approved.claim"),
            mock.patch.object(m.gateway, "write_state_text", return_value=(1, 2)),
        ]
        self.mocks = [p.start() for p in self.patches]
        self.addCleanup(lambda: [p.stop() for p in reversed(self.patches)])

    def apply(self, *, f=None, p=None, token="synthetic-credential", run=RID, sha=SHA):
        return m.bind_once(
            facts=f if f is not None else facts(),
            policy=p if p is not None else policy(),
            workflow_run_id=run, workflow_sha=sha, github_token=token,
        )

    def test_exact_env_only_after_live_review_and_durable_single_use_claim(self):
        self.assertEqual(self.apply(), {m.ENVIRONMENT_VARIABLE: m.MAINTENANCE_SHA})
        self.mocks[1].assert_called_once_with(RID, token="synthetic-credential")
        self.mocks[2].assert_called_once()
        self.mocks[5].assert_called_once()
        self.assertTrue(self.mocks[5].call_args.kwargs["exclusive"])
        self.assertIn('"fencing_token": 9', self.mocks[5].call_args.args[2])

    def test_default_policy_is_disabled(self):
        with self.assertRaisesRegex(m.BindingBlocked, "capability_not_enabled"):
            self.apply(p={})
        self.mocks[1].assert_not_called()
        self.mocks[5].assert_not_called()

    def test_wrong_host_blocks_without_any_external_access(self):
        self.mocks[0].return_value = False
        with self.assertRaisesRegex(m.BindingBlocked, "host_not_allowed"):
            self.apply()
        self.mocks[1].assert_not_called()

    def test_scope_and_runtime_fail_closed_before_github(self):
        variants = [
            {"source_repo": "elsewhere/repo"}, {"expected_head": "d" * 40},
            {"task_name": r"\Automation\OtherTask"}, {"risk": 1},
            {"risk": True}, {"mode": "plan"}, {"command_confirm": "fake"},
            {"state_validated": False}, {"session_launch_ok": False},
            {"clean_worktree": False}, {"exclusive_lease": False},
            {"fencing_token": 0}, {"fencing_token": True},
            {"zero_incremental_cost_verified": False},
            {"session_snapshot_sha256": "short"},
            {"policy_sha256": "bad"}, {"correlation_id": "has spaces"},
        ]
        for variant in variants:
            with self.subTest(variant=variant):
                with self.assertRaises(m.BindingBlocked):
                    self.apply(f=facts(**variant))
        self.mocks[1].assert_not_called()
        self.mocks[5].assert_not_called()

    def test_wrong_pinned_sha_and_policy_fail_closed(self):
        for p, sha in [(policy(noteri_approval_workflow_sha="b" * 40), SHA),
                       (policy(noteri_maintenance_exact_sha="b" * 40), SHA),
                       (policy(), "z" * 40)]:
            with self.subTest(sha=sha, p=p):
                with self.assertRaises(m.BindingBlocked):
                    self.apply(p=p, sha=sha)
        self.mocks[1].assert_not_called()

    def test_unverified_review_result_does_not_consume(self):
        for altered in [
            {"result": "BLOCKED"}, {"review_provenance_verified": False},
            {"execution_authorized": True}, {"maintenance_sha": "c" * 40},
            {"run_id": RID + 1}, {"workflow_sha": "b" * 40},
        ]:
            with self.subTest(altered=altered):
                self.mocks[2].return_value = {**valid_review(), **altered}
                with self.assertRaisesRegex(m.BindingBlocked, "github_review_not_proven"):
                    self.apply()
        self.mocks[5].assert_not_called()

    def test_malformed_review_result_never_claims(self):
        self.mocks[2].return_value = None
        with self.assertRaisesRegex(m.BindingBlocked, "github_review_not_proven"):
            self.apply()
        self.mocks[5].assert_not_called()

    def test_replay_blocks_even_when_review_remains_valid(self):
        self.mocks[5].side_effect = FileExistsError("already claimed")
        with self.assertRaisesRegex(m.BindingBlocked, "approval_replay_blocked"):
            self.apply()

    def test_claim_write_denied_no_env(self):
        self.mocks[5].side_effect = OSError("synthetic io denied")
        with self.assertRaisesRegex(m.BindingBlocked, "durable_consumption_unavailable"):
            self.apply()

    def test_github_source_unavailable_no_claim(self):
        self.mocks[1].side_effect = m.review.RecusaDeAutorizacao("synthetic error")
        with self.assertRaisesRegex(m.BindingBlocked, "github_review_readback_failed"):
            self.apply()
        self.mocks[5].assert_not_called()

    def test_missing_token_and_bad_run_id_do_not_call_api(self):
        for token, run in [("", RID), ("token", 0), ("token", True)]:
            with self.subTest(token=token, run=run):
                with self.assertRaises(m.BindingBlocked):
                    self.apply(token=token, run=run)
        self.mocks[1].assert_not_called()
        self.mocks[5].assert_not_called()

    def test_no_cli_or_subprocess(self):
        import inspect
        text = inspect.getsource(m)
        self.assertNotIn("subprocess.run(", text)
        self.assertNotIn('if __name__ == "__main__"', text)
        self.assertFalse(hasattr(m, "main"))


if __name__ == "__main__":
    unittest.main()
