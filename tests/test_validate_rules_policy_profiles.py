from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
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

    def test_manifest_generator_bootstraps_sibling_import_under_isolation(self) -> None:
        script_dir = ROOT / "scripts"
        generator_path = script_dir / "generate_manifest.py"
        original_path = list(sys.path)
        original_dont_write_bytecode = sys.dont_write_bytecode
        original_validator = sys.modules.pop("validate_rules", None)
        module_name = "_generate_manifest_isolated_import_test"
        sys.modules.pop(module_name, None)
        try:
            sys.path[:] = [
                entry
                for entry in original_path
                if Path(entry or ".").resolve() != script_dir.resolve()
            ]
            spec = importlib.util.spec_from_file_location(module_name, generator_path)
            self.assertIsNotNone(spec)
            self.assertIsNotNone(spec.loader)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            self.assertEqual(module.SCRIPT_DIR, script_dir)
            self.assertEqual(sys.path[0], str(script_dir))
            self.assertIn(".github/workflows/validate-rules.yml", module.REQUIRED_PATHS)
        finally:
            sys.modules.pop(module_name, None)
            sys.path[:] = original_path
            sys.dont_write_bytecode = original_dont_write_bytecode
            if original_validator is None:
                sys.modules.pop("validate_rules", None)
            else:
                sys.modules["validate_rules"] = original_validator

    def test_manifest_generator_entrypoint_runs_under_isolated_python(self) -> None:
        generator_path = ROOT / "scripts" / "generate_manifest.py"
        manifest_path = ROOT / "MANIFEST.json"
        generator_source = generator_path.read_text(encoding="utf-8")
        manifest_source = manifest_path.read_bytes()
        bootstrap = (
            "SCRIPT_DIR = Path(__file__).resolve().parent\n"
            "if str(SCRIPT_DIR) not in sys.path:\n"
            "    sys.path.insert(0, str(SCRIPT_DIR))\n\n"
        )
        self.assertIn(bootstrap, generator_source)

        with tempfile.TemporaryDirectory() as tmp:
            fixture_root = Path(tmp) / "repository"
            listed = subprocess.run(
                ["git", "-C", str(ROOT), "ls-files", "-z"],
                capture_output=True,
                check=True,
            )
            for raw_path in listed.stdout.split(b"\0"):
                if not raw_path:
                    continue
                relative = Path(raw_path.decode("utf-8"))
                destination = fixture_root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / relative, destination)

            subprocess.run(["git", "init", "--quiet", str(fixture_root)], check=True)
            subprocess.run(
                ["git", "-C", str(fixture_root), "add", "--all"],
                check=True,
            )
            fixture_generator = fixture_root / "scripts" / "generate_manifest.py"
            fixture_generator.write_text(
                generator_source.replace(bootstrap, "", 1),
                encoding="utf-8",
                newline="\n",
            )
            negative = subprocess.run(
                [sys.executable, "-I", str(fixture_generator)],
                cwd=fixture_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(negative.returncode, 0)
            self.assertIn("No module named 'validate_rules'", negative.stderr)

            fixture_generator.write_text(
                generator_source,
                encoding="utf-8",
                newline="\n",
            )
            positive = subprocess.run(
                [sys.executable, "-I", str(fixture_generator)],
                cwd=fixture_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(positive.returncode, 0, positive.stderr)
            self.assertIn("MANIFEST_GENERATED", positive.stdout)

            generated = json.loads((fixture_root / "MANIFEST.json").read_text(encoding="utf-8"))
            entries = {entry["path"]: entry for entry in generated["files"]}
            payload = fixture_generator.read_bytes()
            self.assertEqual(
                entries["scripts/generate_manifest.py"]["sha256"],
                hashlib.sha256(payload).hexdigest(),
            )
            self.assertEqual(entries["scripts/generate_manifest.py"]["size"], len(payload))

        self.assertEqual(generator_path.read_text(encoding="utf-8"), generator_source)
        self.assertEqual(manifest_path.read_bytes(), manifest_source)


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
