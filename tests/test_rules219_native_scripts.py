"""Issue #219: actual Git/generator/Validator with synthetic Ollama only.

No physical model, Gateway, CAS authority, runner or deployment is invoked.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import rules219_model_propose as propose
import rules219_model_apply as apply
import rules219_validator as validate

SAFE = (
    "    def test_model_added_wrapper_without_source(self):\n"
    "        self.assertNotEqual(self.wrapper().returncode, 0)\n"
    "        self.assertIn('NO_SOURCE_STAGED', self.wrapper().stderr)\n"
)
DIGEST = "a" * 64
TASK = "cwp-" + "a" * 24


class CandidatePolicyTests(unittest.TestCase):
    def setUp(self):
        self.baseline = (
            b"class ManifestBuilderStageTests:\n"
            b"    def test_existing(self):\n"
            b"        pass\n\n"
            b"if __name__=='__main__':\n"
            b"    pass\n"
        )

    def test_accepts_safe_model_test(self):
        after = propose.candidate(self.baseline, SAFE)
        self.assertIn(b"test_model_added_wrapper_without_source", after)
        self.assertNotEqual(self.baseline, after)

    def test_rejects_code_execution(self):
        malicious = (
            "    def test_model_unsafe(self):\n"
            "        self.assertIn(__import__('os').system('echo forbidden'), 'x')\n"
            "        self.assertNotEqual(self.wrapper().returncode, 0)\n"
        )
        with self.assertRaises(SystemExit):
            propose.candidate(self.baseline, malicious)

    def test_rejects_annotated_arguments(self):
        malicious = (
            "    def test_model_annotated(self: __import__('os')):\n"
            "        self.assertNotEqual(self.wrapper().returncode, 0)\n"
            "        self.assertIn('NO_SOURCE_STAGED', self.wrapper().stderr)\n"
        )
        with self.assertRaises(SystemExit):
            propose.candidate(self.baseline, malicious)

    def test_rejects_extra_arguments_and_keywords(self):
        malicious = (
            "    def test_model_extra(self, arg=None):\n"
            "        self.assertNotEqual(self.wrapper().returncode, 0)\n"
            "        self.assertIn('NO_SOURCE_STAGED', self.wrapper().stderr)\n"
        )
        with self.assertRaises(SystemExit):
            propose.candidate(self.baseline, malicious)

    def test_rejects_unknown_calls(self):
        malicious = (
            "    def test_model_unknown(self):\n"
            "        self.assertNotEqual(self.git('commit').returncode, 0)\n"
            "        self.assertIn('NO_SOURCE_STAGED', self.wrapper().stderr)\n"
        )
        with self.assertRaises(SystemExit):
            propose.candidate(self.baseline, malicious)

    def test_rejects_duplicate_method_name(self):
        duplicate = (
            "    def test_existing(self):\n"
            "        self.assertNotEqual(self.wrapper().returncode, 0)\n"
            "        self.assertIn('NO_SOURCE_STAGED', self.wrapper().stderr)\n"
        )
        with self.assertRaises(SystemExit):
            propose.candidate(self.baseline, duplicate)


class RealGitContractTests(unittest.TestCase):
    """Hermetic local clone, synthetic HTTP, no GitHub write or host network."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "rules"
        origin = Path(__file__).resolve().parents[1]
        subprocess.run(
            ["git", "clone", "-q", "--no-hardlinks", str(origin), str(self.root)],
            check=True, timeout=30,
        )
        self.run_git("remote", "remove", "origin")
        self.base = self.run_git("rev-parse", "HEAD").stdout.strip()
        self.assertRegex(self.base, r"^[0-9a-f]{40}$")
        self.before = (self.root / propose.SOURCE).read_bytes()
        self.run_git("status", "--porcelain")
        # Unit-test runner has argv; the production entrypoints MUST keep
        # rejecting extra CLI args. Model that fixed no-args process here.
        argv = mock.patch.object(sys, "argv", ["rules219-native-entrypoint"])
        argv.start()
        self.addCleanup(argv.stop)
        for module in (propose, apply, validate):
            patcher = mock.patch.object(module, "ROOT", self.root)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {
            "RULES219_TASK_ID": TASK,
            "RULES219_APPROVED_SHA": self.base,
            "RULES219_MODEL_DIGEST": DIGEST,
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        env.start()
        self.addCleanup(env.stop)

    def run_git(self, *args):
        return subprocess.run(
            ["git", *args], cwd=self.root, check=True, timeout=20,
            capture_output=True, text=True,
        )

    @staticmethod
    def fake_api(opener, path, payload=None):
        if path == "/api/tags":
            return {"models": [{"name": propose.MODEL, "digest": DIGEST}]}
        if path == "/api/show":
            return {"details": {"format": "gguf"}}
        if path == "/api/chat":
            return {
                "done": True, "model": propose.MODEL, "eval_count": 2,
                "message": {"content": json.dumps({"method": SAFE})},
            }
        if path == "/api/ps":
            return {"models": [{"name": propose.MODEL, "digest": DIGEST,
                                "size": 1024}]}
        raise AssertionError("Unexpected network path: " + path)

    def propose_once(self):
        with mock.patch.object(propose, "ollama", side_effect=self.fake_api):
            self.assertEqual(propose.main(), 0)
        self.assertEqual(self.run_git("status", "--porcelain").stdout, "")
        return propose.state_dir() / "proposal.json"

    def test_missing_context_blocks_before_git_or_network(self):
        with mock.patch.dict(os.environ, {
            "RULES219_TASK_ID": "", "RULES219_APPROVED_SHA": "",
            "RULES219_MODEL_DIGEST": "",
        }):
            with self.assertRaisesRegex(SystemExit, "INSTALLED_CONTEXT_REQUIRED"):
                propose.main()

    def test_propose_is_one_shot_and_no_source_write(self):
        report = self.propose_once()
        receipt = json.loads(report.read_text())
        self.assertEqual(receipt["model_calls"], 1)
        self.assertEqual(receipt["digest"], DIGEST)
        self.assertEqual(receipt["before_sha256"],
                         hashlib.sha256(self.before).hexdigest())
        self.assertEqual((self.root / propose.SOURCE).read_bytes(), self.before)
        with self.assertRaisesRegex(SystemExit, "PROPOSAL_ALREADY_EXISTS"):
            self.propose_once()

    def test_apply_rejects_tampered_reply_before_write(self):
        report = self.propose_once()
        receipt = json.loads(report.read_text())
        receipt["method"] += "\n# altered"
        report.write_text(json.dumps(receipt))
        with self.assertRaisesRegex(SystemExit, "REPLY_MISMATCH"):
            apply.main()
        self.assertEqual((self.root / propose.SOURCE).read_bytes(), self.before)

    def test_apply_requires_proposal(self):
        with self.assertRaisesRegex(SystemExit, "PROPOSAL_MISSING"):
            apply.main()
        self.assertEqual((self.root / propose.SOURCE).read_bytes(), self.before)

    def test_extra_cli_arguments_fail_closed_for_every_entrypoint(self):
        with mock.patch.object(sys, "argv", ["fixed.py", "--unsafe"]):
            for entry in (propose.main, apply.main, validate.main):
                with self.assertRaisesRegex(SystemExit, "ARGUMENTS_FORBIDDEN"):
                    entry()
        self.assertEqual((self.root / propose.SOURCE).read_bytes(), self.before)

    def test_model_patch_commit_manifest_and_independent_validator(self):
        report = self.propose_once()
        self.assertEqual(apply.main(), 0)
        self.assertEqual(
            self.run_git("diff", "--name-only").stdout.strip(),
            propose.SOURCE,
        )
        with self.assertRaises(SystemExit):
            apply.main()
        self.run_git("add", "--", propose.SOURCE)
        staged = subprocess.run(
            [sys.executable, "-B", "scripts/builder_manifest_stage.py"],
            cwd=self.root, check=False, capture_output=True, text=True,
            timeout=30,
        )
        self.assertEqual(staged.returncode, 0, staged.stderr)
        self.assertEqual(set(self.run_git("diff", "--cached", "--name-only")
                             .stdout.splitlines()),
                         {"MANIFEST.json", propose.SOURCE})
        self.run_git("-c", "user.name=Fixture", "-c",
                     "user.email=fixture@example.invalid",
                     "commit", "-qm", "test(rules219): model-generated regression")
        produced = self.run_git("rev-parse", "HEAD").stdout.strip()
        self.assertNotEqual(produced, self.base)
        # Validator runs on a separate cloned checkout at the exact commit,
        # with its own Git directory, not the Builder working directory.
        independent = Path(self.temp.name) / "validator"
        subprocess.run(
            ["git", "clone", "-q", "--no-hardlinks", str(self.root), str(independent)],
            check=True, timeout=30,
        )
        subprocess.run(
            ["git", "remote", "remove", "origin"], cwd=independent,
            check=True, capture_output=True,
        )
        self.addCleanup(lambda: None)
        with mock.patch.object(validate, "ROOT", independent):
            with mock.patch.object(propose, "ROOT", independent):
                with mock.patch.dict(os.environ, {
                    "RULES219_PRODUCED_SHA": produced,
                }):
                    self.assertEqual(validate.main(), 0)
                    result = json.loads((propose.state_dir() / "validator.json").read_text())
        self.assertEqual(result["source_sha"], self.base)
        self.assertEqual(result["produced_sha"], produced)
        self.assertEqual(result["tests_failed"], 0)
        self.assertGreaterEqual(result["tests_passed"], 12)
        self.assertTrue(result["negative_control_passed"])
        self.assertEqual(self.run_git("status", "--porcelain").stdout, "")
        self.assertTrue(report.exists())  # Local sidecar; not durable CAS.

    def test_validator_rejects_missing_produced_sha(self):
        with mock.patch.dict(os.environ, {"RULES219_PRODUCED_SHA": ""}):
            with self.assertRaisesRegex(SystemExit, "VALIDATOR_CONTEXT_MISSING"):
                validate.main()


if __name__ == "__main__":
    unittest.main()
