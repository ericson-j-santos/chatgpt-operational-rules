from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import validate_rules as vr


class ValidateRulesPolicyProfilesTests(unittest.TestCase):
    @staticmethod
    def policy(platform: str) -> dict[str, object]:
        if platform == "windows":
            allowed = [r"C:\dev\chatgpt-workers\*"]
            denied = [r"C:\Users"]
            state_dir = r"C:\ProgramData\ReqSys\CommandGateway"
            worktree_root = r"C:\dev\chatgpt-workers"
        else:
            allowed = [
                "${REQSYS_WORKSPACE_ROOT}/chatgpt-operational-rules",
                "${REQSYS_WORKSPACE_ROOT}/.chatgpt-workers/*",
            ]
            denied = []
            state_dir = "${REQSYS_GATEWAY_STATE_DIR}"
            worktree_root = "${REQSYS_WORKSPACE_ROOT}/.chatgpt-workers"
        policy = {
            "version": 1,
            "platform": platform,
            "allowed_roots": allowed,
            "denied_roots": denied,
            "denied_segments": [],
            "denied_names": [],
            "allowed_executables": [],
            "blocked_executables": [],
            "max_timeout_seconds": 900,
            "state_dir": state_dir,
            "require_session_bootstrap": True,
            "require_preflight_snapshot": True,
            "worktree_root": worktree_root,
            "worktree_prefix": "wt-chat-",
            "rules_version": "test",
            "session_source_roots": [],
        }
        if platform == "posix":
            policy["required_path_environment"] = {
                "REQSYS_WORKSPACE_ROOT": "/workspace",
                "REQSYS_GATEWAY_STATE_DIR": "/workspace/.reqsys-command-gateway-posix",
            }
        return policy

    def validate_with(self, posix_policy: object) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config"
            config.mkdir()
            (config / "command-gateway.policy.json").write_text(
                json.dumps(self.policy("windows")),
                encoding="utf-8",
            )
            (config / "command-gateway.policy.posix.json").write_text(
                json.dumps(posix_policy),
                encoding="utf-8",
            )
            return vr.validate({"version": "test", "files": []}, root)

    def test_non_object_policy_is_reported_without_exception(self) -> None:
        errors = self.validate_with([])
        self.assertTrue(any("não é objeto" in item for item in errors))

    def test_invalid_timeout_type_is_reported_without_exception(self) -> None:
        policy = self.policy("posix")
        policy["max_timeout_seconds"] = "invalid"
        errors = self.validate_with(policy)
        self.assertTrue(any("timeout máximo" in item for item in errors))

    def test_relative_posix_root_is_rejected(self) -> None:
        policy = self.policy("posix")
        policy["allowed_roots"] = ["relative/repository"]
        errors = self.validate_with(policy)
        self.assertTrue(any("caminhos da política POSIX inválidos" in item for item in errors))

    def test_workspace_wide_posix_root_is_rejected(self) -> None:
        policy = self.policy("posix")
        policy["allowed_roots"] = [
            "${REQSYS_WORKSPACE_ROOT}/*",
            "${REQSYS_WORKSPACE_ROOT}/.chatgpt-workers/*",
        ]
        errors = self.validate_with(policy)
        self.assertIn("Command Gateway POSIX autoriza o workspace inteiro.", errors)

    def test_untrusted_posix_environment_binding_is_rejected(self) -> None:
        policy = self.policy("posix")
        policy["required_path_environment"] = {
            "REQSYS_WORKSPACE_ROOT": "/tmp/caller-controlled",
            "REQSYS_GATEWAY_STATE_DIR": "/workspace/.reqsys-command-gateway-posix",
        }
        errors = self.validate_with(policy)
        self.assertIn(
            "Command Gateway POSIX não vincula o ambiente aos caminhos confiáveis.",
            errors,
        )


class UniversalAcceptancePolicyTests(unittest.TestCase):
    """Prevent loss of the global acceptance gate across repositories."""

    @staticmethod
    def has_required_controls(content: str) -> bool:
        words = (
            "100% dos gates obrigatórios aplicáveis",
            "skipped",
            "neutral",
            "não aplicável",
            "leitura independente",
            "100% de disponibilidade",
        )
        return all(word in content for word in words)

    def test_e2e_rule_retains_all_controls(self) -> None:
        content = (ROOT / "rules/e2e-validation.md").read_text(encoding="utf-8")
        self.assertTrue(self.has_required_controls(content))

    def test_entrypoints_require_full_evidence(self) -> None:
        for path in ("README.md", "AGENTS.md", "rules/pre-delivery-validation.md"):
            content = (ROOT / path).read_text(encoding="utf-8")
            self.assertIn("100% dos gates obrigatórios aplicáveis", content)

    def test_missing_control_fails_closed(self) -> None:
        content = (ROOT / "rules/e2e-validation.md").read_text(encoding="utf-8")
        for term in ("skipped", "não aplicável", "100% de disponibilidade"):
            self.assertFalse(self.has_required_controls(content.replace(term, "")))


if __name__ == "__main__":
    unittest.main()
