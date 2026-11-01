from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "update_branch_fast_forward_governed.py"
SPEC = importlib.util.spec_from_file_location("update_branch_fast_forward_governed", SCRIPT)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)

RECONCILE_SCRIPT = ROOT / "scripts" / "reconcile_todo_gateway_pr185_main_governed.py"
RECONCILE_SPEC = importlib.util.spec_from_file_location(
    "reconcile_todo_gateway_pr185_main_governed", RECONCILE_SCRIPT,
)
assert RECONCILE_SPEC and RECONCILE_SPEC.loader
reconcile_module = importlib.util.module_from_spec(RECONCILE_SPEC)
RECONCILE_SPEC.loader.exec_module(reconcile_module)


def fake_git_factory(
    remote_values: list[str] | None = None,
    ancestor: int = 0,
    dirty: bool = False,
    *,
    repository: str = module.EXPECTED_REPOSITORY,
    branch: str = module.EXPECTED_BRANCH,
    expected_remote: str = module.EXPECTED_REMOTE_SHA,
    expected_head: str = module.EXPECTED_HEAD_SHA,
):
    remote_values = list(remote_values if remote_values is not None else [
        expected_remote, expected_remote, expected_head,
    ])
    calls: list[tuple[str, ...]] = []

    def fake_git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args == ("remote", "get-url", "origin"):
            return subprocess.CompletedProcess(args, 0, f"https://github.com/{repository}.git\n", "")
        if args == ("branch", "--show-current"):
            return subprocess.CompletedProcess(args, 0, branch + "\n", "")
        if args == ("rev-parse", "HEAD"):
            return subprocess.CompletedProcess(args, 0, expected_head + "\n", "")
        if args == ("status", "--porcelain"):
            return subprocess.CompletedProcess(args, 0, " M fixture.py\n" if dirty else "", "")
        if args[:3] == ("ls-remote", "--heads", "origin"):
            value = remote_values.pop(0)
            return subprocess.CompletedProcess(args, 0, f"{value}\trefs/heads/{branch}\n", "")
        if args[:2] == ("merge-base", "--is-ancestor"):
            return subprocess.CompletedProcess(args, ancestor, "", "")
        if args[:1] == ("push",):
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(args)

    return fake_git, calls


class GovernedUpdateTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        # Only the test fixture changes: the operational Windows allowlist remains intact.
        root_patch = patch.object(module, "EXPECTED_REPO_ROOT", str(root))
        root_patch.start()
        self.addCleanup(root_patch.stop)
        self.base_args = {
            "repo": root,
            "remote": "origin",
            "branch": module.EXPECTED_BRANCH,
            "expected_remote": module.EXPECTED_REMOTE_SHA,
            "expected_head": module.EXPECTED_HEAD_SHA,
        }

    def test_update_uses_plain_fast_forward_push_and_readback(self) -> None:
        fake_git, calls = fake_git_factory()
        with patch.object(module, "git", side_effect=fake_git):
            evidence = module.update(**self.base_args)
        self.assertEqual(evidence["fast_forward"], "true")
        self.assertEqual(evidence["force_push"], "false")
        self.assertEqual(evidence["remote_compare_atomic"], "false")
        self.assertEqual(evidence["post_remote"], module.EXPECTED_HEAD_SHA)
        self.assertEqual(1, sum(call[:1] == ("push",) for call in calls))
        self.assertIn(("push", "origin", f"HEAD:refs/heads/{module.EXPECTED_BRANCH}"), calls)
        self.assertTrue(all("force" not in token for call in calls for token in call))

    def test_worker_pool_update_is_an_exact_reviewed_operation(self) -> None:
        worker = next(
            operation for operation in module.reviewed_operations()
            if operation["repository"] == module.WORKER_REPOSITORY
        )
        self.assertEqual(worker, {
            "repository": "ericson-j-santos/engineering-worker-pool",
            "branch": "codex/windows-native-no-docker-20261022",
            "expected_remote": "8740def7e832916452b13c52a1f4bec2f5712df7",
            "expected_head": "cd150974a094dcda240987d8a2283c709a714d36",
            "repo_root": r"C:\dev\chatgpt-workers\wt-chat-engineering-worker-pool-base-20261022-a1",
        })

        worker_root = self.base_args["repo"]
        values = {
            "repo": worker_root,
            "remote": "origin",
            "branch": module.WORKER_BRANCH,
            "expected_remote": module.WORKER_REMOTE_SHA,
            "expected_head": module.WORKER_HEAD_SHA,
        }
        fake_git, calls = fake_git_factory(
            repository=module.WORKER_REPOSITORY,
            branch=module.WORKER_BRANCH,
            expected_remote=module.WORKER_REMOTE_SHA,
            expected_head=module.WORKER_HEAD_SHA,
        )
        with (
            patch.object(module, "WORKER_REPO_ROOT", str(worker_root)),
            patch.object(module, "git", side_effect=fake_git),
        ):
            evidence = module.update(**values)
        self.assertEqual(evidence["repository"], module.WORKER_REPOSITORY)
        self.assertIn(("push", "origin", f"HEAD:refs/heads/{module.WORKER_BRANCH}"), calls)

    def test_noteri_temporary_updates_are_exact_reviewed_operations(self) -> None:
        cases = (
            {
                "root_name": "NOTERI_REPO_ROOT",
                "repository": module.NOTERI_REPOSITORY,
                "branch": module.NOTERI_BRANCH,
                "expected_remote": module.NOTERI_REMOTE_SHA,
                "expected_head": module.NOTERI_HEAD_SHA,
                "repo_root": (
                    r"C:\dev\chatgpt-workers\wt-chat-noteri-eventlog-reconcile-gs-20261025-r2"
                ),
            },
            {
                "root_name": "RULES_REPO_ROOT",
                "repository": module.RULES_REPOSITORY,
                "branch": module.RULES_BRANCH,
                "expected_remote": module.RULES_REMOTE_SHA,
                "expected_head": module.RULES_HEAD_SHA,
                "repo_root": (
                    r"C:\dev\chatgpt-workers\wt-chat-noteri-eventlog-rules-reconcile-gs-20261025-r2"
                ),
            },
        )
        operations = module.reviewed_operations()
        temporary = [
            operation
            for operation in operations
            if operation["repository"]
            in {module.NOTERI_REPOSITORY, module.RULES_REPOSITORY}
        ]
        self.assertEqual(len(temporary), 2)

        for case in cases:
            with self.subTest(repository=case["repository"]):
                operation = next(
                    item
                    for item in temporary
                    if item["repository"] == case["repository"]
                )
                self.assertEqual(
                    operation,
                    {
                        key: case[key]
                        for key in (
                            "repository",
                            "branch",
                            "expected_remote",
                            "expected_head",
                            "repo_root",
                        )
                    },
                )
                root = self.base_args["repo"]
                values = {
                    "repo": root,
                    "remote": "origin",
                    "branch": case["branch"],
                    "expected_remote": case["expected_remote"],
                    "expected_head": case["expected_head"],
                }
                fake_git, calls = fake_git_factory(
                    repository=case["repository"],
                    branch=case["branch"],
                    expected_remote=case["expected_remote"],
                    expected_head=case["expected_head"],
                )
                with (
                    patch.object(module, case["root_name"], str(root)),
                    patch.object(module, "git", side_effect=fake_git),
                ):
                    evidence = module.update(**values)
                self.assertEqual(evidence["repository"], case["repository"])
                self.assertEqual(evidence["remote_compare_atomic"], "false")
                self.assertIn(
                    ("push", "origin", f"HEAD:refs/heads/{case['branch']}"),
                    calls,
                )

    def test_noteri_temporary_updates_reject_mixed_sha_pairs(self) -> None:
        invalid = (
            (module.NOTERI_BRANCH, module.NOTERI_REMOTE_SHA, module.RULES_HEAD_SHA),
            (module.RULES_BRANCH, module.NOTERI_REMOTE_SHA, module.RULES_HEAD_SHA),
            (module.NOTERI_BRANCH, module.RULES_REMOTE_SHA, module.NOTERI_HEAD_SHA),
        )
        for branch, expected_remote, expected_head in invalid:
            with self.subTest(
                branch=branch,
                expected_remote=expected_remote,
                expected_head=expected_head,
            ):
                with patch.object(module, "git") as git:
                    with self.assertRaisesRegex(RuntimeError, "reviewed branch"):
                        module.validate(
                            self.base_args["repo"],
                            "origin",
                            branch,
                            expected_remote,
                            expected_head,
                        )
                git.assert_not_called()

    def test_lenovo_pr112_policy_and_script_scope_match_exactly(self) -> None:
        operation = next(
            item for item in module.reviewed_operations()
            if item["branch"] == module.LENOVO_PR112_BRANCH
        )
        self.assertEqual(operation, {
            "repository": "ericson-j-santos/desktop-pc24x7-runtime",
            "branch": "fix/lenovo-native-task-readback-20261027",
            "expected_remote": "04d7788513618bfd3cf39c283747f9e3f0d88397",
            "expected_head": "0f2337012a7926d2327cfbdd8931147354d1db68",
            "repo_root": r"C:\dev\chatgpt-workers\wt-chat-lenovo-task-readback-20261027",
        })
        policy = json.loads((ROOT / "config" / "command-gateway.policy.json").read_text())
        matches = [item for item in policy["governed_branch_updates"] if item["branch"] == operation["branch"]]
        self.assertEqual(len(matches), 1)
        self.assertEqual({key: matches[0][key] for key in operation}, operation)
        self.assertEqual(
            matches[0]["script"],
            r"C:\dev\chatgpt-workers\wt-chat-lenovo-pr112-rules-20261027\scripts\update_branch_fast_forward_governed.py",
        )

    def test_lenovo_pr112_update_and_mixed_sha_rejection(self) -> None:
        values = {
            "repo": self.base_args["repo"],
            "remote": "origin",
            "branch": module.LENOVO_PR112_BRANCH,
            "expected_remote": module.LENOVO_PR112_REMOTE_SHA,
            "expected_head": module.LENOVO_PR112_HEAD_SHA,
        }
        fake_git, calls = fake_git_factory(
            repository=module.LENOVO_PR112_REPOSITORY,
            branch=module.LENOVO_PR112_BRANCH,
            expected_remote=module.LENOVO_PR112_REMOTE_SHA,
            expected_head=module.LENOVO_PR112_HEAD_SHA,
        )
        with (
            patch.object(module, "LENOVO_PR112_REPO_ROOT", str(values["repo"])),
            patch.object(module, "git", side_effect=fake_git),
        ):
            evidence = module.update(**values)
            with self.assertRaisesRegex(RuntimeError, "reviewed branch"):
                module.validate(**dict(values, expected_head=module.EXPECTED_HEAD_SHA))
        self.assertEqual(evidence["post_remote"], module.LENOVO_PR112_HEAD_SHA)
        self.assertEqual(sum(call[:1] == ("push",) for call in calls), 1)

    def test_update_aborts_if_remote_changes_between_checks(self) -> None:
        fake_git, calls = fake_git_factory([module.EXPECTED_REMOTE_SHA, "4" * 40])
        with patch.object(module, "git", side_effect=fake_git):
            with self.assertRaisesRegex(RuntimeError, "during pre-push"):
                module.update(**self.base_args)
        self.assertFalse(any(call[:1] == ("push",) for call in calls))

    def test_update_rejects_non_ancestor(self) -> None:
        fake_git, calls = fake_git_factory(ancestor=1)
        with patch.object(module, "git", side_effect=fake_git):
            with self.assertRaisesRegex(RuntimeError, "fast-forward"):
                module.update(**self.base_args)
        self.assertFalse(any(call[:1] == ("push",) for call in calls))

    def test_update_is_one_branch_one_sha_pair(self) -> None:
        for field in ("remote", "branch", "expected_remote", "expected_head"):
            with self.subTest(field=field):
                values = dict(self.base_args)
                values[field] = "different"
                with self.assertRaises(RuntimeError):
                    module.validate(**values)

    def test_different_root_is_rejected_before_git(self) -> None:
        values = dict(self.base_args, repo=self.base_args["repo"] / "outside")
        with patch.object(module, "git") as git:
            with self.assertRaisesRegex(RuntimeError, "repository path"):
                module.validate(**values)
        git.assert_not_called()

    def test_dirty_worktree_does_not_push(self) -> None:
        fake_git, calls = fake_git_factory(dirty=True)
        with patch.object(module, "git", side_effect=fake_git):
            with self.assertRaisesRegex(RuntimeError, "clean"):
                module.update(**self.base_args)
        self.assertFalse(any(call[:1] == ("push",) for call in calls))

    def test_replay_of_one_time_operation_does_not_push_again(self) -> None:
        fake_git, calls = fake_git_factory([module.EXPECTED_HEAD_SHA])
        with patch.object(module, "git", side_effect=fake_git):
            with self.assertRaisesRegex(RuntimeError, "before maintenance"):
                module.update(**self.base_args)
        self.assertFalse(any(call[:1] == ("push",) for call in calls))

    def test_incorrect_final_readback_is_not_success(self) -> None:
        fake_git, _ = fake_git_factory([
            module.EXPECTED_REMOTE_SHA, module.EXPECTED_REMOTE_SHA, "4" * 40,
        ])
        with patch.object(module, "git", side_effect=fake_git):
            with self.assertRaisesRegex(RuntimeError, "post-push"):
                module.update(**self.base_args)


def fixture_git(
    repo: Path,
    *args: str,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
) -> str:
    child_env = os.environ.copy()
    if env:
        child_env.update(env)
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        input=input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=child_env,
        check=True,
    )
    return result.stdout.strip()


class TodoGatewayPr185ReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.origin = self.root / "origin.git"
        self.repo = self.root / "repo"
        git_path = shutil.which("git")
        self.assertIsNotNone(git_path)
        self.git_path = Path(git_path).resolve()
        self.git_sha256 = hashlib.sha256(self.git_path.read_bytes()).hexdigest()
        subprocess.run(["git", "init", "--bare", str(self.origin)], check=True, capture_output=True)
        subprocess.run(
            ["git", "-c", "core.autocrlf=false", "clone", str(self.origin), str(self.repo)],
            check=True, capture_output=True,
        )
        fixture_git(self.repo, "config", "core.autocrlf", "false")
        fixture_git(self.repo, "config", "user.name", "Fixture")
        fixture_git(self.repo, "config", "user.email", "fixture@example.invalid")
        fixture_git(self.repo, "config", "filter.lfs.clean", "")
        fixture_git(self.repo, "config", "filter.lfs.smudge", "")
        fixture_git(self.repo, "config", "filter.lfs.process", "")
        fixture_git(self.repo, "config", "filter.lfs.required", "false")
        fixture_git(self.repo, "checkout", "-b", "main")
        generator_source = (ROOT / "scripts" / "generate_manifest.py").read_text(encoding="utf-8")
        base_generator_source = generator_source.replace(
            "sys.dont_write_bytecode = True",
            "sys.dont_write_bytecode = True\n# base fixture variant",
            1,
        )
        files = {
            "README.md": "# Fixture\n\nVersão: 9.9.9\n",
            "MANIFEST.json": "{}\n",
            "base.txt": "base\n",
            "required.txt": "required\n",
            ".github/workflows/validate-rules.yml": "base workflow\n",
            "scripts/generate_manifest.py": base_generator_source,
            "scripts/validate_rules.py": "REQUIRED_PATHS = {'README.md', 'required.txt'}\n",
            "tests/test_validate_rules_policy_profiles.py": "base tests\n",
        }
        for path, content in files.items():
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")
        fixture_git(self.repo, "add", "-A")
        fixture_git(self.repo, "commit", "-m", "base")
        self.base = fixture_git(self.repo, "rev-parse", "HEAD")

        fixture_git(self.repo, "checkout", "-b", "pr185")
        overlays = {
            ".github/workflows/validate-rules.yml": "reviewed workflow\n",
            "scripts/generate_manifest.py": generator_source,
            "tests/test_validate_rules_policy_profiles.py": "reviewed tests\n",
        }
        for path, content in overlays.items():
            (self.repo / path).write_text(content, encoding="utf-8", newline="\n")
        (self.repo / "MANIFEST.json").write_text('{"stale": true}\n', encoding="utf-8")
        fixture_git(self.repo, "add", "-A")
        fixture_git(self.repo, "commit", "-m", "reviewed PR")
        self.old = fixture_git(self.repo, "rev-parse", "HEAD")
        fixture_git(self.repo, "push", "-u", "origin", "pr185")

        fixture_git(self.repo, "checkout", "main")
        # An identical overlap is allowed; a differing overlap is not.
        (self.repo / "scripts/generate_manifest.py").write_text(
            generator_source, encoding="utf-8", newline="\n",
        )
        (self.repo / "main-only.txt").write_text("main change\n", encoding="utf-8")
        fixture_git(self.repo, "add", "-A")
        fixture_git(self.repo, "commit", "-m", "advance main")
        self.main = fixture_git(self.repo, "rev-parse", "HEAD")
        fixture_git(self.repo, "push", "-u", "origin", "main")
        fixture_git(self.repo, "checkout", "pr185")
        fixture_git(self.repo, "fetch", "origin", "main:refs/remotes/origin/main")

        self.overlay_blobs = {
            path: fixture_git(self.repo, "rev-parse", f"{self.old}:{path}")
            for path in overlays
        }
        self.real_canonical_remote = reconcile_module._canonical_remote

        def fixture_canonical_remote(value: str) -> str:
            try:
                if Path(value).resolve() == self.origin.resolve():
                    return "fixture/repository"
            except OSError:
                pass
            return self.real_canonical_remote(value)

        patches = (
            patch.object(reconcile_module, "REPOSITORY", "fixture/repository"),
            patch.object(reconcile_module, "_canonical_remote", side_effect=fixture_canonical_remote),
            patch.object(reconcile_module, "BRANCH", "pr185"),
            patch.object(reconcile_module, "OLD_HEAD", self.old),
            patch.object(reconcile_module, "BASE_SHA", self.base),
            patch.object(reconcile_module, "OVERLAY_BLOBS", self.overlay_blobs),
            patch.object(reconcile_module, "EXPECTED_HOST", "fixture-host"),
            patch.object(reconcile_module.socket, "gethostname", return_value="fixture-host"),
        )
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def _proposal(self) -> tuple[Path, str, dict[str, object]]:
        candidate = self.root / f"candidate-{len(list(self.root.glob('candidate-*')))}"
        self.candidate = candidate
        subprocess.run(
            ["git", "-c", "core.autocrlf=false", "clone", str(self.origin), str(candidate)],
            check=True, capture_output=True,
        )
        fixture_git(candidate, "config", "core.autocrlf", "false")
        fixture_git(candidate, "checkout", "--detach", self.main)
        for path, blob in self.overlay_blobs.items():
            target = candidate / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(subprocess.run(
                ["git", "-C", str(candidate), "cat-file", "blob", blob],
                check=True, capture_output=True,
            ).stdout)
        paths = fixture_git(candidate, "ls-files").splitlines()
        manifest_files = []
        for path in sorted(set(paths) - {"MANIFEST.json"}):
            payload = (candidate / path).read_bytes()
            manifest_files.append({
                "path": path,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
            })
        manifest_payload = (json.dumps({
            "version": "9.9.9",
            "generated_at": "2026-10-28T00:00:00+00:00",
            "files": manifest_files,
        }, ensure_ascii=False, indent=2) + "\n")
        (candidate / "MANIFEST.json").write_text(manifest_payload, encoding="utf-8", newline="\n")
        fixture_git(candidate, "add", "-A")
        manifest_blob = fixture_git(candidate, "hash-object", "MANIFEST.json")
        tree = fixture_git(candidate, "write-tree")
        commit_env = {
            "GIT_AUTHOR_NAME": reconcile_module.IDENTITY_NAME,
            "GIT_AUTHOR_EMAIL": reconcile_module.IDENTITY_EMAIL,
            "GIT_AUTHOR_DATE": "2026-10-28T00:00:00+00:00",
            "GIT_COMMITTER_NAME": reconcile_module.IDENTITY_NAME,
            "GIT_COMMITTER_EMAIL": reconcile_module.IDENTITY_EMAIL,
            "GIT_COMMITTER_DATE": "2026-10-28T00:00:00+00:00",
        }
        binary_env = os.environ.copy()
        binary_env.update(commit_env)
        commit = subprocess.run(
            [
                "git", "-C", str(candidate), "commit-tree", tree,
                "-p", reconcile_module.OLD_HEAD, "-p", self.main,
            ],
            input=(reconcile_module.MESSAGE + "\n").encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=binary_env,
            check=True,
        ).stdout.decode("ascii").strip()
        proposal: dict[str, object] = {
            "version": 1,
            "repository": reconcile_module.REPOSITORY,
            "branch": reconcile_module.BRANCH,
            "old_head": reconcile_module.OLD_HEAD,
            "base_sha": reconcile_module.BASE_SHA,
            "main_sha": self.main,
            "main_tree": fixture_git(self.repo, "rev-parse", f"{self.main}^{{tree}}"),
            "generated_at": "2026-10-28T00:00:00+00:00",
            "commit_date": "2026-10-28T00:00:00+00:00",
            "overlay_blobs": self.overlay_blobs,
            "expected_file_count": len(manifest_files),
            "expected_manifest_blob": manifest_blob,
            "expected_tree": tree,
            "expected_commit": commit,
            "git_path": str(self.git_path),
            "git_sha256": self.git_sha256,
        }
        proposal_path = self.root / f"proposal-{len(list(self.root.glob('proposal-*')))}.json"
        payload = (json.dumps(proposal, sort_keys=True, indent=2) + "\n").encode("utf-8")
        proposal_path.write_bytes(payload)
        return proposal_path, hashlib.sha256(payload).hexdigest(), proposal

    def test_proposal_bound_reconcile_syncs_tree_index_and_remote_then_replays(self) -> None:
        proposal_path, digest, proposal = self._proposal()
        merge_hook_marker = self.root / "merge-hook-ran"
        push_hook_marker = self.root / "push-hook-ran"
        hooks = self.repo / ".git" / "hooks"
        for name, marker in (("post-merge", merge_hook_marker), ("pre-push", push_hook_marker)):
            hook = hooks / name
            hook.write_text(f"#!/bin/sh\nprintf ran > '{marker.as_posix()}'\n", encoding="utf-8")
            hook.chmod(0o755)
        calls: list[tuple[str, ...]] = []
        real_run = reconcile_module._run_git

        def recording_run(repo: Path, *args: str, **kwargs):
            calls.append(args)
            return real_run(repo, *args, **kwargs)

        with patch.object(reconcile_module, "_run_git", side_effect=recording_run):
            first = reconcile_module.reconcile(self.repo, proposal_path, digest, "pr185-fixture")
        self.assertEqual(first["result"], "RECONCILED_AND_PUBLISHED")
        self.assertFalse(first["remote_compare_atomic"])
        self.assertFalse(first["force_push"])
        self.assertEqual(fixture_git(self.repo, "rev-parse", "HEAD"), proposal["expected_commit"])
        self.assertEqual(fixture_git(self.repo, "write-tree"), proposal["expected_tree"])
        self.assertEqual(fixture_git(self.repo, "status", "--porcelain"), "")
        self.assertEqual(
            fixture_git(self.origin, "rev-parse", "refs/heads/pr185"),
            proposal["expected_commit"],
        )
        self.assertEqual(
            fixture_git(self.repo, "show", "-s", "--format=%P", "HEAD").split(),
            [self.old, self.main],
        )
        self.assertFalse(merge_hook_marker.exists())
        self.assertFalse(push_hook_marker.exists())
        pushes = [call for call in calls if "push" in call]
        self.assertEqual(len(pushes), 1)
        push = pushes[0]
        self.assertEqual(push[push.index("push") + 1 :], ("origin", "HEAD:refs/heads/pr185"))
        self.assertTrue(any(token.startswith("core.hooksPath=") for token in push))
        self.assertTrue(all("force" not in token for call in calls for token in call))
        replay = reconcile_module.reconcile(self.repo, proposal_path, digest, "pr185-replay")
        self.assertEqual(replay["result"], "ALREADY_RECONCILED")
        self.assertEqual(replay["commit"], proposal["expected_commit"])

    def test_remote_ref_drift_blocks_before_local_change(self) -> None:
        proposal_path, digest, _ = self._proposal()
        fixture_git(self.origin, "update-ref", "refs/heads/pr185", self.base)
        with self.assertRaisesRegex(reconcile_module.ReconcileBlocked, "remote_branch_changed"):
            reconcile_module.reconcile(self.repo, proposal_path, digest, "pr185-drift")
        self.assertEqual(fixture_git(self.repo, "rev-parse", "HEAD"), self.old)

    def test_main_drift_blocks_before_local_change(self) -> None:
        proposal_path, digest, _ = self._proposal()
        fixture_git(self.origin, "update-ref", "refs/heads/main", self.base)
        with self.assertRaisesRegex(reconcile_module.ReconcileBlocked, "remote_main_changed"):
            reconcile_module.reconcile(self.repo, proposal_path, digest, "pr185-main-drift")
        self.assertEqual(fixture_git(self.repo, "rev-parse", "HEAD"), self.old)

    def test_replay_with_unpublished_remote_is_reported_pending_without_push(self) -> None:
        proposal_path, digest, proposal = self._proposal()
        reconcile_module.reconcile(self.repo, proposal_path, digest, "pr185-first")
        fixture_git(self.origin, "update-ref", "refs/heads/pr185", self.old)
        calls: list[tuple[str, ...]] = []
        real_run = reconcile_module._run_git

        def recording_run(repo: Path, *args: str, **kwargs):
            calls.append(args)
            return real_run(repo, *args, **kwargs)

        with patch.object(reconcile_module, "_run_git", side_effect=recording_run):
            with self.assertRaisesRegex(reconcile_module.ReconcileBlocked, "publication_pending"):
                reconcile_module.reconcile(self.repo, proposal_path, digest, "pr185-pending")
        self.assertEqual(fixture_git(self.repo, "rev-parse", "HEAD"), proposal["expected_commit"])
        self.assertFalse(any("push" in call for call in calls))

    def test_inherited_git_environment_blocks_before_any_git_call(self) -> None:
        proposal_path, digest, _ = self._proposal()
        trace_marker = self.root / "external-git-trace"
        for key, value in (
            ("GIT_DIR", str(self.root / "redirect")),
            ("GIT_TRACE", str(trace_marker)),
            ("GIT_TRACE2_EVENT", str(trace_marker)),
        ):
            with self.subTest(key=key):
                with patch.dict(os.environ, {key: value}, clear=False):
                    with patch.object(reconcile_module.subprocess, "run") as run:
                        with self.assertRaisesRegex(
                            reconcile_module.ReconcileBlocked, "git_environment_unsafe"
                        ):
                            reconcile_module.reconcile(
                                self.repo, proposal_path, digest, f"pr185-env-{key.lower()}"
                            )
                        run.assert_not_called()
                self.assertFalse(trace_marker.exists())

    def test_git_identity_and_proposal_schema_fail_closed(self) -> None:
        proposal_path, _digest, proposal = self._proposal()

        def write(value: object) -> str:
            payload = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode("utf-8")
            proposal_path.write_bytes(payload)
            return hashlib.sha256(payload).hexdigest()

        proposal["git_sha256"] = "0" * 64
        with self.assertRaisesRegex(reconcile_module.ReconcileBlocked, "git_identity_mismatch"):
            reconcile_module.reconcile(self.repo, proposal_path, write(proposal), "pr185-git-id")

        proposal["git_sha256"] = self.git_sha256
        proposal["version"] = True
        with self.assertRaisesRegex(reconcile_module.ReconcileBlocked, "proposal_version_invalid"):
            reconcile_module.reconcile(self.repo, proposal_path, write(proposal), "pr185-version")

        proposal["version"] = 1
        proposal["generated_at"] = "2026-02-30T00:00:00+00:00"
        with self.assertRaisesRegex(reconcile_module.ReconcileBlocked, "proposal_timestamp_invalid"):
            reconcile_module.reconcile(self.repo, proposal_path, write(proposal), "pr185-date")

        duplicate = write({**proposal, "generated_at": "2026-10-28T00:00:00+00:00"})
        raw = proposal_path.read_text(encoding="utf-8").replace(
            '  "version": 1', '  "version": 1,\n  "version": 1', 1,
        )
        proposal_path.write_text(raw, encoding="utf-8", newline="\n")
        with self.assertRaisesRegex(reconcile_module.ReconcileBlocked, "proposal_duplicate_key"):
            reconcile_module.reconcile(
                self.repo, proposal_path,
                hashlib.sha256(proposal_path.read_bytes()).hexdigest(), "pr185-duplicate",
            )
        self.assertRegex(duplicate, r"^[0-9a-f]{64}$")

        oversized = b"{" + b" " * reconcile_module.MAX_PROPOSAL_BYTES + b"}"
        proposal_path.write_bytes(oversized)
        with self.assertRaisesRegex(reconcile_module.ReconcileBlocked, "proposal_too_large"):
            reconcile_module.reconcile(
                self.repo, proposal_path, hashlib.sha256(oversized).hexdigest(), "pr185-large",
            )

    def test_unsafe_config_and_remote_redirection_block_before_publication(self) -> None:
        proposal_path, digest, _ = self._proposal()
        fsmonitor_marker = self.root / "fsmonitor-ran"
        fixture_git(
            self.repo, "config", "core.fsmonitor",
            f"!sh -c \"printf ran > '{fsmonitor_marker.as_posix()}'\"",
        )
        reconcile_module._activate_git({"git_path": str(self.git_path), "git_sha256": self.git_sha256})
        reconcile_module._assert_safe_git_config(self.repo)
        reconcile_module._text(self.repo, "status", "--porcelain")
        self.assertFalse(fsmonitor_marker.exists())
        fixture_git(self.repo, "config", "--unset-all", "core.fsmonitor")

        completed = reconcile_module.subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"", stderr=b""
        )
        with patch.object(
            reconcile_module.subprocess, "run", return_value=completed
        ) as run:
            reconcile_module._run_git(self.repo, "status", "--porcelain")
        command = run.call_args.args[0]
        self.assertIn("core.fsmonitor=false", command)
        self.assertIn("http.followRedirects=false", command)

        for key, value in (
            ("filter.attack.smudge", "external-filter"),
            ("url.https://evil.example/.pushInsteadOf", str(self.origin)),
        ):
            with self.subTest(key=key):
                fixture_git(self.repo, "config", key, value)
                with self.assertRaisesRegex(reconcile_module.ReconcileBlocked, "git_config_unsafe"):
                    reconcile_module.reconcile(self.repo, proposal_path, digest, "pr185-config")
                fixture_git(self.repo, "config", "--unset-all", key)
                self.assertEqual(fixture_git(self.repo, "rev-parse", "HEAD"), self.old)
                self.assertEqual(fixture_git(self.origin, "rev-parse", "refs/heads/pr185"), self.old)

        other = self.root / "other.git"
        subprocess.run(["git", "init", "--bare", str(other)], check=True, capture_output=True)
        fixture_git(self.repo, "remote", "set-url", "--add", "--push", "origin", str(self.origin))
        fixture_git(self.repo, "remote", "set-url", "--add", "--push", "origin", str(other))
        with self.assertRaisesRegex(reconcile_module.ReconcileBlocked, "remote_url_count_invalid"):
            reconcile_module.reconcile(self.repo, proposal_path, digest, "pr185-pushurls")
        with self.assertRaisesRegex(reconcile_module.ReconcileBlocked, "remote_url_not_approved"):
            self.real_canonical_remote(str(self.origin))

    def test_canonical_generator_accepts_resolved_temporary_directory_alias(self) -> None:
        proposal_path, digest, proposal = self._proposal()
        with tempfile.TemporaryDirectory(dir=self.root) as canonical:
            # Controlled lexical alias exercises spelling differences without
            # claiming to reproduce Windows 8.3 physically on POSIX.
            alias = Path(canonical) / ".." / Path(canonical).name
            self.assertNotEqual(str(alias), str(alias.resolve()))
            with patch.object(reconcile_module.tempfile, "TemporaryDirectory") as temporary:
                temporary.return_value.__enter__.return_value = str(alias)
                result = reconcile_module.reconcile(
                    self.repo, proposal_path, digest, "pr185-canonical-alias"
                )
            temporary.assert_called_once_with(prefix="pr185-canonical-manifest-")
        self.assertEqual(result["result"], "RECONCILED_AND_PUBLISHED")
        self.assertEqual(fixture_git(self.repo, "rev-parse", "HEAD"), proposal["expected_commit"])
        self.assertEqual(fixture_git(self.repo, "write-tree"), proposal["expected_tree"])
        self.assertEqual(
            fixture_git(self.origin, "rev-parse", "refs/heads/pr185"),
            proposal["expected_commit"],
        )

    def test_canonical_generator_still_rejects_changed_git_command(self) -> None:
        self._assert_canonical_command_rejected('"ls-files"', '"status"')

    def test_canonical_generator_still_rejects_distinct_root(self) -> None:
        self._assert_canonical_command_rejected('str(ROOT), "ls-files"', 'str(ROOT.parent), "ls-files"')

    def test_canonical_generator_still_rejects_extra_argument(self) -> None:
        self._assert_canonical_command_rejected('"ls-files", "-z"', '"ls-files", "-z", "--cached"')

    def _assert_canonical_command_rejected(self, original: str, replacement: str) -> None:
        proposal_path, digest, _ = self._proposal()
        real_run = reconcile_module._run_git

        def replace_fixture_command(repo: Path, *args: str, **kwargs):
            completed = real_run(repo, *args, **kwargs)
            if args and args[0] == "checkout-index":
                prefix = next(arg.removeprefix("--prefix=") for arg in args if arg.startswith("--prefix="))
                generator = Path(prefix) / "scripts" / "generate_manifest.py"
                source = generator.read_text(encoding="utf-8")
                self.assertIn(original, source)
                self.assertNotEqual(original, replacement)
                generator.write_text(source.replace(original, replacement, 1), encoding="utf-8")
            return completed

        with patch.object(reconcile_module, "_run_git", side_effect=replace_fixture_command):
            with self.assertRaisesRegex(reconcile_module.ReconcileBlocked, "canonical_generator_external_command"):
                reconcile_module.reconcile(self.repo, proposal_path, digest, "pr185-command-guard")
        self.assertEqual(fixture_git(self.repo, "rev-parse", "HEAD"), self.old)
        self.assertEqual(fixture_git(self.origin, "rev-parse", "refs/heads/pr185"), self.old)

    def test_canonical_generator_mismatch_blocks_before_local_change(self) -> None:
        proposal_path, digest, _ = self._proposal()
        with patch.object(reconcile_module, "_canonical_manifest_payload", return_value=b"different\n"):
            with self.assertRaisesRegex(reconcile_module.ReconcileBlocked, "canonical_manifest_mismatch"):
                reconcile_module.reconcile(self.repo, proposal_path, digest, "pr185-generator")
        self.assertEqual(fixture_git(self.repo, "rev-parse", "HEAD"), self.old)
        self.assertEqual(fixture_git(self.origin, "rev-parse", "refs/heads/pr185"), self.old)

    def test_push_timeout_is_read_back_without_second_commit(self) -> None:
        proposal_path, digest, proposal = self._proposal()
        real_run = reconcile_module._run_git
        calls: list[tuple[str, ...]] = []

        def timeout_before_push(repo: Path, *args: str, **kwargs):
            calls.append(args)
            if "push" in args:
                raise subprocess.TimeoutExpired("git push", 60)
            return real_run(repo, *args, **kwargs)

        with patch.object(reconcile_module, "_run_git", side_effect=timeout_before_push):
            with self.assertRaisesRegex(reconcile_module.ReconcileBlocked, "publication_incomplete"):
                reconcile_module.reconcile(self.repo, proposal_path, digest, "pr185-timeout-before")
        self.assertEqual(fixture_git(self.repo, "rev-parse", "HEAD"), proposal["expected_commit"])
        self.assertEqual(fixture_git(self.origin, "rev-parse", "refs/heads/pr185"), self.old)
        self.assertEqual(sum("commit-tree" in call for call in calls), 1)

    def test_push_timeout_after_remote_acceptance_uses_readback(self) -> None:
        proposal_path, digest, proposal = self._proposal()
        real_run = reconcile_module._run_git
        calls: list[tuple[str, ...]] = []

        def timeout_after_push(repo: Path, *args: str, **kwargs):
            calls.append(args)
            result = real_run(repo, *args, **kwargs)
            if "push" in args:
                raise subprocess.TimeoutExpired("git push", 60)
            return result

        with patch.object(reconcile_module, "_run_git", side_effect=timeout_after_push):
            result = reconcile_module.reconcile(self.repo, proposal_path, digest, "pr185-timeout-after")
        self.assertEqual(result["result"], "RECONCILED_AND_PUBLISHED_AFTER_READBACK")
        self.assertTrue(result["push_had_local_error"])
        self.assertEqual(fixture_git(self.origin, "rev-parse", "refs/heads/pr185"), proposal["expected_commit"])
        self.assertEqual(sum("commit-tree" in call for call in calls), 1)
        self.assertEqual(sum("push" in call for call in calls), 1)

    def test_main_change_during_push_is_reported_after_remote_readback(self) -> None:
        proposal_path, digest, proposal = self._proposal()
        real_run = reconcile_module._run_git

        def drift_after_push(repo: Path, *args: str, **kwargs):
            result = real_run(repo, *args, **kwargs)
            if "push" in args:
                fixture_git(self.origin, "update-ref", "refs/heads/main", self.base)
            return result

        with patch.object(reconcile_module, "_run_git", side_effect=drift_after_push):
            with self.assertRaisesRegex(reconcile_module.ReconcileBlocked, "remote_main_changed_during_push"):
                reconcile_module.reconcile(self.repo, proposal_path, digest, "pr185-main-race")
        self.assertEqual(fixture_git(self.origin, "rev-parse", "refs/heads/pr185"), proposal["expected_commit"])

    def test_hooks_directory_substitution_blocks_before_push(self) -> None:
        proposal_path, digest, proposal = self._proposal()
        real_plain = reconcile_module._plain_path
        hook_checks = 0
        calls: list[tuple[str, ...]] = []
        real_run = reconcile_module._run_git

        def guarded_plain(path: Path, *, directory: bool) -> None:
            nonlocal hook_checks
            if directory and path.name.startswith("reqsys-pr185-empty-hooks-"):
                hook_checks += 1
                if hook_checks == 2:
                    raise reconcile_module.ReconcileBlocked("path_identity_redirected")
            real_plain(path, directory=directory)

        def recording_run(repo: Path, *args: str, **kwargs):
            calls.append(args)
            return real_run(repo, *args, **kwargs)

        with (
            patch.object(reconcile_module, "_plain_path", side_effect=guarded_plain),
            patch.object(reconcile_module, "_run_git", side_effect=recording_run),
        ):
            with self.assertRaisesRegex(reconcile_module.ReconcileBlocked, "path_identity_redirected"):
                reconcile_module.reconcile(self.repo, proposal_path, digest, "pr185-hook-swap")
        self.assertEqual(hook_checks, 2)
        self.assertFalse(any("push" in call for call in calls))
        self.assertEqual(fixture_git(self.repo, "rev-parse", "HEAD"), proposal["expected_commit"])
        self.assertEqual(fixture_git(self.origin, "rev-parse", "refs/heads/pr185"), self.old)

    def test_differing_main_overlap_is_rejected(self) -> None:
        fixture_git(self.repo, "checkout", "main")
        (self.repo / "scripts" / "generate_manifest.py").write_text(
            "different main generator\n", encoding="utf-8", newline="\n",
        )
        fixture_git(self.repo, "add", "scripts/generate_manifest.py")
        fixture_git(self.repo, "commit", "-m", "conflicting main overlap")
        self.main = fixture_git(self.repo, "rev-parse", "HEAD")
        fixture_git(self.repo, "push", "origin", "main")
        fixture_git(self.repo, "checkout", "pr185")
        fixture_git(self.repo, "fetch", "origin", "main:refs/remotes/origin/main")
        proposal_path, digest, _ = self._proposal()
        with self.assertRaisesRegex(reconcile_module.ReconcileBlocked, "main_overlay_conflict"):
            reconcile_module.reconcile(self.repo, proposal_path, digest, "pr185-main-conflict")
        self.assertEqual(fixture_git(self.repo, "rev-parse", "HEAD"), self.old)

    def test_unexpected_pr_path_is_rejected(self) -> None:
        (self.repo / "unexpected.txt").write_text("not reviewed\n", encoding="utf-8")
        fixture_git(self.repo, "add", "unexpected.txt")
        fixture_git(self.repo, "commit", "-m", "unexpected path")
        changed_old = fixture_git(self.repo, "rev-parse", "HEAD")
        fixture_git(self.repo, "push", "origin", "pr185")
        with patch.object(reconcile_module, "OLD_HEAD", changed_old):
            proposal_path, digest, _ = self._proposal()
            with self.assertRaisesRegex(reconcile_module.ReconcileBlocked, "pr_path_set_changed"):
                reconcile_module.reconcile(self.repo, proposal_path, digest, "pr185-extra-path")
        self.assertEqual(fixture_git(self.origin, "rev-parse", "refs/heads/pr185"), changed_old)

    def test_digest_and_expected_tree_are_fail_closed(self) -> None:
        proposal_path, digest, proposal = self._proposal()
        with self.assertRaisesRegex(reconcile_module.ReconcileBlocked, "proposal_digest_mismatch"):
            reconcile_module.reconcile(self.repo, proposal_path, "0" * 64, "pr185-bad-digest")
        proposal["expected_tree"] = "0" * 40
        payload = (json.dumps(proposal, sort_keys=True, indent=2) + "\n").encode("utf-8")
        proposal_path.write_bytes(payload)
        with self.assertRaisesRegex(reconcile_module.ReconcileBlocked, "target_tree_mismatch"):
            reconcile_module.reconcile(
                self.repo, proposal_path, hashlib.sha256(payload).hexdigest(), "pr185-bad-tree",
            )
        self.assertEqual(fixture_git(self.repo, "rev-parse", "HEAD"), self.old)
        self.assertEqual(fixture_git(self.origin, "rev-parse", "refs/heads/pr185"), self.old)

    def test_boolean_file_count_and_symlink_index_entry_are_rejected(self) -> None:
        proposal_path, _digest, proposal = self._proposal()
        proposal["expected_file_count"] = True
        payload = (json.dumps(proposal, sort_keys=True, indent=2) + "\n").encode("utf-8")
        proposal_path.write_bytes(payload)
        with self.assertRaisesRegex(reconcile_module.ReconcileBlocked, "proposal_file_count_invalid"):
            reconcile_module.reconcile(
                self.repo, proposal_path, hashlib.sha256(payload).hexdigest(), "pr185-bool-count",
            )

        symlink_record = (
            f"120000 {'1' * 40} 0\tlinked.txt\0".encode("ascii")
        )
        completed = subprocess.CompletedProcess(["git"], 0, symlink_record, b"")
        with patch.object(reconcile_module, "_run_git", return_value=completed):
            with self.assertRaisesRegex(reconcile_module.ReconcileBlocked, "index_entry_invalid"):
                reconcile_module._index_entries(self.repo, {})


if __name__ == "__main__":
    unittest.main()
