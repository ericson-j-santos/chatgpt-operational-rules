from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "scripts" / "pr_ci_self_heal.py"
spec = importlib.util.spec_from_file_location("pr_ci_self_heal", MODULE)
assert spec and spec.loader
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class PrCiSelfHealTests(unittest.TestCase):
    def make_repo(self) -> Path:
        root = Path(tempfile.mkdtemp())
        subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.name", "CI Test"], check=True)
        (root / "README.md").write_text("# Teste\n\nVersão: 1.6.1\n", encoding="utf-8")
        (root / "payload.txt").write_text("v1\n", encoding="utf-8")
        (root / "MANIFEST.json").write_text(
            json.dumps({"version": "1.6.1", "generated_at": "old", "files": []}) + "\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "-C", str(root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-m", "baseline"], check=True, capture_output=True)
        return root

    def test_regenerate_is_idempotent_after_manifest_commit(self):
        root = self.make_repo()
        changed, first = m.regenerate_if_needed(root, {"README.md", "payload.txt"})
        self.assertTrue(changed)
        self.assertEqual([item["path"] for item in first["files"]], ["README.md", "payload.txt"])
        subprocess.run(["git", "-C", str(root), "add", "MANIFEST.json"], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-m", "manifest"], check=True, capture_output=True)
        changed_again, second = m.regenerate_if_needed(root, {"README.md", "payload.txt"})
        self.assertFalse(changed_again)
        self.assertEqual(first["files"], second["files"])

    def test_payload_change_updates_only_manifest_when_repo_is_recleaned(self):
        root = self.make_repo()
        changed, _ = m.regenerate_if_needed(root, {"README.md", "payload.txt"})
        self.assertTrue(changed)
        subprocess.run(["git", "-C", str(root), "add", "MANIFEST.json"], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-m", "manifest"], check=True, capture_output=True)
        (root / "payload.txt").write_text("v2\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", "payload.txt"], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-m", "payload"], check=True, capture_output=True)
        m.verify_clean(root)
        changed, _ = m.regenerate_if_needed(root, {"README.md", "payload.txt"})
        self.assertTrue(changed)
        self.assertEqual(m.changed_paths(root), ["MANIFEST.json"])

    def test_verify_clean_blocks_dirty_target(self):
        root = self.make_repo()
        (root / "payload.txt").write_text("dirty\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "target_not_clean"):
            m.verify_clean(root)

    def test_verify_expected_head_detects_concurrency(self):
        root = self.make_repo()
        with self.assertRaisesRegex(RuntimeError, "head_changed"):
            m.verify_expected_head(root, "0" * 40)


class WorkflowApprovalTests(unittest.TestCase):
    """Execute only the bounded, credential-free approval-report step, not a GitHub approval."""

    def setUp(self):
        self.workflow = (ROOT / ".github/workflows/pr-ci-self-heal.yml").read_text(encoding="utf-8")

    def approval_step(self, workflow=None):
        raw = workflow if workflow is not None else self.workflow
        marker = "      - name: Preserve workflow approval requirement\n"
        self.assertEqual(raw.count(marker), 1)
        step = raw.split(marker, 1)[1].split("      - name:", 1)[0]
        self.assertIn("steps.context.outputs.conclusion == 'action_required'", step)
        self.assertIn("steps.context.outputs.enabled == 'true'", step)
        self.assertNotIn("GH_TOKEN", step)
        script_lines = step.split("        run: |\n", 1)[1].rstrip().splitlines()
        script = "\n".join(line[10:] for line in script_lines) + "\n"
        self.assertNotIn("gh ", script)
        self.assertNotIn("curl", script)
        self.assertNotIn("wget", script)
        self.assertNotIn("workflow_dispatch", script)
        return script

    def run_step(self, script, directory, output, sha):
        # No inherited credentials/profile, no external executable search, no network tools.
        # The versioned step uses only Bash builtins and writes the temporary output file.
        import shutil
        bash = shutil.which("bash")
        self.assertIsNotNone(bash, "bash is required for the real workflow-step contract test")
        path = directory / "approval-step.sh"
        path.write_text(script, encoding="utf-8")
        return subprocess.run(
            [bash, "--noprofile", "--norc", str(path)],
            cwd=directory, env={"PATH": "", "HEAD_SHA": sha, "GITHUB_OUTPUT": str(output)},
            text=True, capture_output=True, timeout=5, check=False,
        )

    def test_action_required_preserves_identity_without_dispatch(self):
        script = self.approval_step()
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            output = directory / "output.txt"
            result = self.run_step(script, directory, output, "a" * 40)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(output.read_text().splitlines(), [
                "action=blocked_workflow_approval_required", "result_sha=" + "a" * 40,
            ])
            self.assertIn("no workflow was dispatched", result.stdout)
            self.assertEqual({p.name for p in directory.iterdir()}, {"approval-step.sh", "output.txt"})

    def test_action_required_replay_keeps_same_block_and_sha(self):
        script = self.approval_step()
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            for attempt in range(2):
                output = directory / f"output-{attempt}.txt"
                result = self.run_step(script, directory, output, "b" * 40)
                self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((directory / "output-0.txt").read_bytes(),
                             (directory / "output-1.txt").read_bytes())

    def test_invalid_sha_never_emits_approval_or_mutates_output(self):
        script = self.approval_step()
        for value in ("", "a" * 39, "A" * 40, "a" * 40 + "\nresult=success", "$(echo injected)"):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as name:
                directory = Path(name)
                output = directory / "output.txt"
                result = self.run_step(script, directory, output, value)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(output.exists())
                self.assertNotIn("injected", result.stdout)

    def test_regression_control_rejects_redispatch_in_approval_step(self):
        mutated = self.workflow.replace(
            'echo "Original run requires approval; no workflow was dispatched."',
            "gh workflow run validate-rules.yml",
            1,
        )
        self.assertNotEqual(mutated, self.workflow)
        with self.assertRaises(AssertionError):
            self.approval_step(mutated)

    def test_approval_is_terminal_block_not_successful_remediation(self):
        terminal = self.workflow.split(
            "      - name: Fail closed when no safe remediation exists\n", 1
        )[1]
        self.assertIn("steps.context.outputs.conclusion == 'action_required' ||", terminal)
        script = "\n".join(line[10:] for line in
                          terminal.split("        run: |\n", 1)[1].rstrip().splitlines()) + "\n"
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            result = self.run_step(script, directory, directory / "output.txt", "a" * 40)
            self.assertEqual(result.returncode, 1)

    def test_approval_diagnostic_is_wired_and_existing_gates_are_preserved(self):
        self.assertIn("steps.approval.outputs.action", self.workflow)
        self.assertIn("steps.approval.outputs.result_sha", self.workflow)
        self.assertNotIn("steps.revalidate.outputs.", self.workflow)
        self.assertNotIn("validation_dispatched_without_code_change", self.workflow)
        self.assertIn("reason=stale_workflow_run", self.workflow)
        self.assertIn("reason=cross_repository_pr_not_mutated", self.workflow)
        self.assertIn("blocked_concurrent_pr_update", self.workflow)
        self.assertIn("git -C target diff --name-only", self.workflow)
        self.assertIn("persist-credentials: false", self.workflow)
        self.assertIn("github.event.repository.default_branch", self.workflow)
        self.assertEqual(self.workflow.count("gh workflow run validate-rules.yml"), 1)
        self.assertIn("Um run separado não resolve essa aprovação", self.workflow)


if __name__ == "__main__":
    unittest.main()
