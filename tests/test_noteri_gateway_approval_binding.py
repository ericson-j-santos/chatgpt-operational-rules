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


class GatewayIntegratedHandoffTests(unittest.TestCase):
    """Contrato do Gateway real, com API e host simulados; nenhum spawn."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from scripts import command_gateway
        self.gateway = command_gateway
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cwd = Path(self.temp.name)
        (self.cwd / "scripts").mkdir()
        (self.cwd / "scripts" / "noteri_worker_pool_inplace_maintenance.py").write_text(
            "# synthetic-only\n", encoding="utf-8"
        )
        self.args = [
            "python", "scripts/noteri_worker_pool_inplace_maintenance.py",
            "--mode", "apply", "--expected-head", m.MAINTENANCE_SHA,
            "--confirm", m.CONFIRM,
        ]
        self.before = SimpleNamespace(head=m.MAINTENANCE_SHA, status_count=0)
        self.policy = {
            m.POLICY_FLAG: True,
            "require_preflight_snapshot": True,
            "noteri_approval_workflow_sha": SHA,
            "noteri_approval_run_id": RID,
            "noteri_maintenance_exact_sha": m.MAINTENANCE_SHA,
        }

    def call(self, *, args=None, risk=2, policy=None, expected_head=None,
             session_id="noteri-session-01", allow_dirty=False,
             allow_head_change=False):
        return self.gateway.noteri_inplace_maintenance_spawn_environment(
            args=args if args is not None else self.args,
            cwd=self.cwd, policy=policy if policy is not None else self.policy,
            risk=risk, session_id=session_id,
            correlation_id="noteri-approval-01", before=self.before,
            expected_head=expected_head or m.MAINTENANCE_SHA,
            allow_dirty=allow_dirty, allow_head_change=allow_head_change,
        )

    def test_unrelated_command_never_intercepts(self):
        self.assertIsNone(self.call(args=["python", "tests/other.py"]))

    def test_plan_sanitizes_untrusted_environment(self):
        import os
        p = self.args.copy()
        p[3] = "plan"
        with mock.patch.dict(os.environ, {
            m.ENVIRONMENT_VARIABLE: "forged",
            "NOTERI_APPROVAL_READ_TOKEN": "synthetic-token",
            "REPOSITORY_GOVERNANCE_TOKEN": "synthetic-other-token",
        }):
            env = self.call(args=p, risk=1)
        for key in (m.ENVIRONMENT_VARIABLE, "NOTERI_APPROVAL_READ_TOKEN",
                    "REPOSITORY_GOVERNANCE_TOKEN"):
            self.assertNotIn(key, env)

    def test_readonly_plan_works_without_confirmation_or_outer_head(self):
        import os
        with mock.patch.dict(os.environ, {
            m.ENVIRONMENT_VARIABLE: "forged",
            "NOTERI_APPROVAL_READ_TOKEN": "untrusted",
        }):
            env = self.gateway.noteri_inplace_maintenance_spawn_environment(
                args=self.args[:6],
                cwd=self.cwd, policy=self.policy, risk=1,
                session_id=None, correlation_id="noteri-plan-only",
                before=self.before, expected_head=None,
                allow_dirty=False, allow_head_change=False,
            )
        self.assertNotIn(m.ENVIRONMENT_VARIABLE, env)
        self.assertNotIn("NOTERI_APPROVAL_READ_TOKEN", env)

    def test_apply_without_confirm_never_dispatches(self):
        with mock.patch.object(m.review, "consultar_evidencia_github") as lookup:
            with self.assertRaisesRegex(self.gateway.GatewayError,
                                        "EXPLICIT_CONFIRMATION_REQUIRED"):
                self.call(args=self.args[:6])
            lookup.assert_not_called()

    def test_readonly_plan_rejects_forced_confirmation(self):
        args = self.args.copy()
        args[3] = "plan"
        args[7] = "untrusted"
        with self.assertRaisesRegex(self.gateway.GatewayError,
                                     "PLAN_ARGUMENTS_INVALID"):
            self.call(args=args, risk=1)

    def test_plan_does_not_require_installed_approval_module(self):
        import builtins
        importer = builtins.__import__
        def deny_binding_import(name, globals=None, locals=None, fromlist=(), level=0):
            if (name == "scripts" and "noteri_gateway_approval_binding" in (fromlist or ())):
                raise AssertionError("Risk1 plan attempted to load Risk2 binding")
            return importer(name, globals, locals, fromlist, level)
        with mock.patch("builtins.__import__", side_effect=deny_binding_import):
            env = self.call(args=self.args[:6], risk=1)
        self.assertNotIn(m.ENVIRONMENT_VARIABLE, env)

    def test_other_gateway_child_cannot_inherit_maintenance_approval_flag(self):
        import contextlib
        import os
        import subprocess
        from pathlib import Path
        g = self.gateway
        state = g.GitState(
            repo_root=str(self.cwd), branch="DETACHED",
            head=m.MAINTENANCE_SHA, status_digest="0" * 64, status_count=0,
        )
        @contextlib.contextmanager
        def lock(*args, **kwargs):
            yield Path(self.cwd) / "synthetic-only-lock"
        @contextlib.contextmanager
        def anchored(*args, **kwargs):
            yield str(self.cwd), {}
        with mock.patch.object(g, "canonical_existing_directory", return_value=self.cwd), \
             mock.patch.object(g, "assert_allowed_path"), \
             mock.patch.object(g, "validate_command"), \
             mock.patch.object(g, "assert_safe_gui_automation"), \
             mock.patch.object(g, "git_state", return_value=state), \
             mock.patch.object(g, "repository_lock", side_effect=lock), \
             mock.patch.object(g, "anchored_subprocess_cwd", side_effect=anchored), \
             mock.patch.object(g, "append_event"), \
             mock.patch.object(g, "emit_json"), \
             mock.patch.object(g.subprocess, "run", return_value=
                 subprocess.CompletedProcess(["python"], 0, "", "")) as spawn, \
             mock.patch.dict(os.environ, {m.ENVIRONMENT_VARIABLE: m.MAINTENANCE_SHA}):
            code = g.execute(
                cwd=self.cwd, policy={"max_timeout_seconds": 10},
                args=["python", "scripts/unrelated.py"], risk=1,
                timeout=5, expected_head=None, allow_dirty=False,
                allow_head_change=False, correlation_id="synthetic-only",
            )
        self.assertEqual(code, 0)
        self.assertEqual(spawn.call_count, 1)
        self.assertNotIn(m.ENVIRONMENT_VARIABLE, spawn.call_args.kwargs["env"])

    def test_apply_disabled_prevents_any_external_approval_lookup(self):
        with mock.patch.object(m.review, "consultar_evidencia_github") as lookup:
            with self.assertRaisesRegex(self.gateway.GatewayError, "CAPABILITY_DISABLED"):
                self.call(policy={})
            lookup.assert_not_called()

    def test_malformed_commands_cannot_trigger_approval(self):
        alternatives = [
            self.args + ["--extra", "oops"],
            ["python", "-m", "scripts.noteri_worker_pool_inplace_maintenance",
             "--mode", "apply"],
            [*self.args[:6], "--confirm", "wrong"],
            ["python", "elsewhere/noteri_worker_pool_inplace_maintenance.py",
             *self.args[2:]],
        ]
        with mock.patch.object(m.review, "consultar_evidencia_github") as lookup:
            for argv in alternatives:
                with self.subTest(argv=argv):
                    with self.assertRaises(self.gateway.GatewayError):
                        self.call(args=argv)
            lookup.assert_not_called()

    def test_dirty_or_head_change_flags_never_authorize_apply(self):
        for flags in ({"allow_dirty": True}, {"allow_head_change": True},
                      {"expected_head": "b" * 40}):
            with self.subTest(flags=flags):
                with self.assertRaisesRegex(self.gateway.GatewayError, "GIT_STATE_MISMATCH"):
                    self.call(**flags)

    def test_missing_authenticated_session_fails_before_github(self):
        with mock.patch.object(self.gateway, "enforce_session", return_value={
                "status": "materialized", "fencing_token": 3}), \
             mock.patch.object(self.gateway, "validate_session_snapshot",
                 return_value={"snapshot_sha256": "a" * 64}), \
             mock.patch.object(self.gateway, "run_capture") as capture, \
             mock.patch.object(m.review, "consultar_evidencia_github") as lookup:
            import subprocess
            capture.return_value = subprocess.CompletedProcess(
                ["git"], 0,
                "https://github.com/ericson-j-santos/noteri-runtime.git\n", "")
            with self.assertRaisesRegex(self.gateway.GatewayError,
                                         "NOTERI_APPROVAL_NOT_ATTESTED"):
                self.call()
            lookup.assert_not_called()

    def test_prevalidated_handoff_passes_only_scoped_environment(self):
        import os
        import subprocess
        snapshot = {
            "snapshot_sha256": "a" * 64,
            "session_launch_ok": True,
            "state_validated": True,
            "zero_incremental_cost_verified": True,
        }
        with mock.patch.object(self.gateway, "enforce_session", return_value={
                 "status": "materialized", "fencing_token": 3}), \
             mock.patch.object(self.gateway, "validate_session_snapshot",
                               return_value=snapshot), \
             mock.patch.object(self.gateway, "run_capture", return_value=
                 subprocess.CompletedProcess(
                     ["git"], 0,
                     "https://github.com/ericson-j-santos/noteri-runtime.git\n", "")), \
             mock.patch.object(m, "bind_once",
                               return_value={m.ENVIRONMENT_VARIABLE: m.MAINTENANCE_SHA}) as binding, \
             mock.patch.dict(os.environ, {m.ENVIRONMENT_VARIABLE: "untrusted",
                                         "NOTERI_APPROVAL_READ_TOKEN": "read-only-synthetic"}):
            env = self.call()
        self.assertEqual(env[m.ENVIRONMENT_VARIABLE], m.MAINTENANCE_SHA)
        self.assertNotIn("NOTERI_APPROVAL_READ_TOKEN", env)
        self.assertEqual(binding.call_count, 1)
        bound_facts = binding.call_args.kwargs["facts"]
        self.assertTrue(bound_facts.state_validated)
        self.assertTrue(bound_facts.exclusive_lease)
        self.assertEqual(bound_facts.expected_head, m.MAINTENANCE_SHA)
        self.assertEqual(binding.call_args.kwargs["workflow_run_id"], RID)

    def test_binder_may_never_return_arbitrary_environment(self):
        import subprocess
        with mock.patch.object(self.gateway, "enforce_session", return_value={
                 "status": "materialized", "fencing_token": 3}), \
             mock.patch.object(self.gateway, "validate_session_snapshot", return_value={
                 "snapshot_sha256": "a" * 64,
                 "session_launch_ok": True, "state_validated": True,
                 "zero_incremental_cost_verified": True}), \
             mock.patch.object(self.gateway, "run_capture", return_value=
                 subprocess.CompletedProcess(
                     ["git"], 0, "https://github.com/ericson-j-santos/noteri-runtime.git\n", "")), \
             mock.patch.object(m, "bind_once", return_value={"PATH": "untrusted"}):
            with self.assertRaisesRegex(self.gateway.GatewayError,
                                         "BINDING_RESULT_INVALID"):
                self.call()


if __name__ == "__main__":
    unittest.main()
