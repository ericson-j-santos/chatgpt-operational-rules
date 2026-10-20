from __future__ import annotations

import json
import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
POSIX_POLICY = ROOT / "config" / "command-gateway.policy.posix.json"
WINDOWS_POLICY = ROOT / "config" / "command-gateway.policy.json"
sys.path.insert(0, str(SCRIPTS))

import command_gateway as cg


class PosixCommandGatewayPolicyTests(unittest.TestCase):
    def test_both_profiles_are_structurally_valid(self) -> None:
        windows = cg.load_policy(
            WINDOWS_POLICY,
            enforce_platform=False,
            validate_paths=False,
        )
        posix = cg.load_policy(
            POSIX_POLICY,
            enforce_platform=False,
            validate_paths=False,
        )
        self.assertEqual(windows["platform"], "windows")
        self.assertEqual(posix["platform"], "posix")
        self.assertEqual(windows["rules_version"], posix["rules_version"])

    @unittest.skipUnless(os.name == "nt", "perfil Windows é exercitado em executor Windows")
    def test_windows_profile_loads_on_windows(self) -> None:
        policy = cg.load_policy(WINDOWS_POLICY)
        self.assertEqual(policy["platform"], "windows")
        self.assertEqual(cg.current_platform(), "windows")

    @unittest.skipIf(os.name == "nt", "perfil POSIX é exercitado em executor POSIX")
    def test_posix_profile_loads_with_explicit_environment(self) -> None:
        workspace = Path("/workspace")
        state = Path("/workspace/.reqsys-command-gateway-posix")
        with mock.patch.dict(
            os.environ,
            {
                "REQSYS_WORKSPACE_ROOT": str(workspace),
                "REQSYS_GATEWAY_STATE_DIR": str(state),
            },
            clear=False,
        ):
            policy = cg.load_policy(POSIX_POLICY)
            cg.assert_allowed_path(workspace / "chatgpt-operational-rules", policy)
            cg.assert_allowed_path(workspace / ".chatgpt-workers" / "wt-chat-test", policy)
            with self.assertRaisesRegex(cg.GatewayError, "fora da allowlist"):
                cg.assert_allowed_path(workspace / "unlisted-repository", policy)

    @unittest.skipIf(os.name == "nt", "perfil POSIX é exercitado em executor POSIX")
    def test_posix_profile_rejects_environment_that_changes_authority(self) -> None:
        trusted = {
            "REQSYS_WORKSPACE_ROOT": "/workspace",
            "REQSYS_GATEWAY_STATE_DIR": "/workspace/.reqsys-command-gateway-posix",
        }
        for name in trusted:
            environment = dict(trusted)
            environment[name] = "/tmp/caller-controlled"
            with self.subTest(name=name), mock.patch.dict(
                os.environ,
                environment,
                clear=False,
            ), self.assertRaisesRegex(cg.GatewayError, "diverge do valor confiável"):
                cg.load_policy(POSIX_POLICY)

    @unittest.skipIf(os.name == "nt", "semântica de symlink validada em POSIX")
    def test_posix_profile_rejects_symlink_alias_for_trusted_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            alias = Path(tmp) / "workspace-alias"
            alias.symlink_to("/workspace", target_is_directory=True)
            with mock.patch.dict(
                os.environ,
                {
                    "REQSYS_WORKSPACE_ROOT": str(alias),
                    "REQSYS_GATEWAY_STATE_DIR": "/workspace/.reqsys-command-gateway-posix",
                },
                clear=False,
            ), self.assertRaisesRegex(cg.GatewayError, "diverge do valor confiável"):
                cg.load_policy(POSIX_POLICY)

    def test_loaded_policy_freezes_expanded_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trusted = root / "trusted"
            replacement = root / "replacement"
            policy_path = root / "policy.json"
            policy_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "required_path_environment": {"REQSYS_TEST_ROOT": str(trusted)},
                        "allowed_roots": ["${REQSYS_TEST_ROOT}/repository"],
                        "denied_roots": [],
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ,
                {"REQSYS_TEST_ROOT": str(trusted)},
                clear=False,
            ):
                policy = cg.load_policy(policy_path)
                os.environ["REQSYS_TEST_ROOT"] = str(replacement)
                cg.assert_allowed_path(trusted / "repository", policy)
                with self.assertRaisesRegex(cg.GatewayError, "fora da allowlist"):
                    cg.assert_allowed_path(replacement / "repository", policy)

    def test_nested_policy_paths_are_bound_and_frozen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trusted = root / "trusted"
            replacement = root / "replacement"
            policy_path = root / "policy.json"
            policy_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "required_path_environment": {"REQSYS_TEST_ROOT": str(trusted)},
                        "allowed_roots": ["${REQSYS_TEST_ROOT}/repository"],
                        "denied_roots": [],
                        "governed_branch_updates": [
                            {
                                "script": "${REQSYS_TEST_ROOT}/tools/update.py",
                                "repo_root": "${REQSYS_TEST_ROOT}/repository",
                            }
                        ],
                        "allowed_executable_specs": [
                            {
                                "path": "${REQSYS_TEST_ROOT}/tools/helper",
                                "sha256": "0" * 64,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ,
                {"REQSYS_TEST_ROOT": str(trusted)},
                clear=False,
            ):
                policy = cg.load_policy(policy_path)
                os.environ["REQSYS_TEST_ROOT"] = str(replacement)
                self.assertEqual(
                    policy["governed_branch_updates"][0]["repo_root"],
                    str(trusted / "repository"),
                )
                self.assertEqual(
                    policy["allowed_executable_specs"][0]["path"],
                    str(trusted / "tools" / "helper"),
                )

    def test_required_environment_covers_every_referenced_variable(self) -> None:
        policy = {
            "version": 1,
            "required_path_environment": {"REQSYS_TEST_ROOT": "/trusted"},
            "allowed_roots": ["${REQSYS_OTHER_ROOT}/repository"],
            "denied_roots": [],
        }
        with self.assertRaisesRegex(cg.GatewayError, "sem vínculo"):
            cg.validate_required_path_environment(policy)

    def test_unresolved_path_variable_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "allowed_roots": ["${REQSYS_TEST_UNSET_ROOT}/repo"],
                        "denied_roots": [],
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(cg.GatewayError, "variável"):
                    cg.load_policy(path)

    def test_all_variable_syntaxes_and_empty_values_fail_closed(self) -> None:
        samples = (
            "$REQSYS_TEST_UNSET_ROOT/repo",
            "${REQSYS_TEST_UNSET_ROOT}/repo",
            "%REQSYS_TEST_UNSET_ROOT%/repo",
            "/${REQSYS_TEST_MALFORMED/repo",
        )
        with mock.patch.dict(os.environ, {}, clear=True):
            for sample in samples:
                with self.subTest(sample=sample), self.assertRaises(cg.GatewayError):
                    cg.expand_path(sample)
        with mock.patch.dict(os.environ, {"REQSYS_TEST_EMPTY_ROOT": ""}, clear=True):
            with self.assertRaisesRegex(cg.GatewayError, "ausente ou vazia"):
                cg.expand_path("${REQSYS_TEST_EMPTY_ROOT}/repo")

    def test_platform_mismatch_fails_closed(self) -> None:
        other = "posix" if cg.current_platform() == "windows" else "windows"
        with self.assertRaisesRegex(cg.GatewayError, "executor atual"):
            cg.validate_policy({"version": 1, "platform": other})

    def test_relative_policy_path_is_rejected(self) -> None:
        with self.assertRaisesRegex(cg.GatewayError, "caminho absoluto"):
            cg.validate_runtime_policy_paths(
                {"version": 1, "allowed_roots": ["relative/repository"]}
            )

    def test_disabling_platform_check_does_not_disable_path_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "platform": "windows" if os.name != "nt" else "posix",
                        "allowed_roots": ["relative/repository"],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(cg.GatewayError, "caminho absoluto"):
                cg.load_policy(path, enforce_platform=False)

    @unittest.skipIf(os.name == "nt", "semântica de symlink validada em POSIX")
    def test_symlink_cannot_escape_allowed_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            allowed = root / "allowed"
            outside = root / "outside"
            allowed.mkdir()
            outside.mkdir()
            link = allowed / "escape"
            link.symlink_to(outside, target_is_directory=True)
            policy = {
                "allowed_roots": [str(allowed)],
                "denied_roots": [],
                "denied_segments": [],
            }
            with self.assertRaisesRegex(cg.GatewayError, "fora da allowlist"):
                cg.assert_allowed_path(link, policy)

    @unittest.skipIf(os.name == "nt", "semântica de symlink validada em POSIX")
    def test_symlink_with_target_inside_allowed_root_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            allowed = Path(tmp) / "allowed"
            target = allowed / "target"
            target.mkdir(parents=True)
            link = allowed / "inside"
            link.symlink_to(target, target_is_directory=True)
            cg.assert_allowed_path(
                link,
                {
                    "allowed_roots": [str(allowed)],
                    "denied_roots": [],
                    "denied_segments": [],
                },
            )

    @unittest.skipIf(os.name == "nt", "semântica de symlink validada em POSIX")
    def test_policy_root_that_is_already_symlinked_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            target.mkdir()
            linked_root = root / "linked-root"
            linked_root.symlink_to(target, target_is_directory=True)
            policy_path = root / "policy.json"
            policy_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "allowed_roots": [str(linked_root / "*")],
                        "denied_roots": [],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(cg.GatewayError, "symlink ou junction"):
                cg.load_policy(policy_path)

    @unittest.skipIf(os.name == "nt", "semântica de symlink validada em POSIX")
    def test_loaded_allowed_pattern_does_not_follow_retargeted_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workers = root / "workers"
            workers.mkdir()
            policy_path = root / "policy.json"
            policy_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "allowed_roots": [str(workers / "*")],
                        "denied_roots": [],
                    }
                ),
                encoding="utf-8",
            )
            policy = cg.load_policy(policy_path)
            workers.rename(root / "workers-original")
            outside = root / "outside"
            candidate = outside / "repository"
            candidate.mkdir(parents=True)
            workers.symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(cg.GatewayError, "fora da allowlist"):
                cg.assert_allowed_path(workers / "repository", policy)

    @unittest.skipIf(os.name == "nt", "semântica de symlink validada em POSIX")
    def test_state_dir_retarget_after_load_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            state.mkdir()
            policy_path = root / "policy.json"
            policy_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "allowed_roots": [str(root)],
                        "denied_roots": [],
                        "state_dir": str(state),
                    }
                ),
                encoding="utf-8",
            )
            policy = cg.load_policy(policy_path)
            state.rename(root / "state-original")
            outside = root / "outside-state"
            outside.mkdir()
            state.symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(cg.GatewayError, "symlink ou junction"):
                cg.state_dir(policy)

    @unittest.skipIf(os.name == "nt", "semântica de symlink validada em POSIX")
    def test_state_descendant_symlinks_are_rejected_for_files_and_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            outside = root / "outside"
            state.mkdir()
            outside.mkdir()
            policy_path = root / "policy.json"
            policy_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "allowed_roots": [str(root)],
                        "denied_roots": [],
                        "state_dir": str(state),
                    }
                ),
                encoding="utf-8",
            )
            policy = cg.load_policy(policy_path)
            (state / "sessions").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(cg.GatewayError, "symlink ou junction"):
                cg.secure_state_path(
                    policy,
                    "sessions",
                    "session.json",
                    create_parent=True,
                )
            (state / "sessions").unlink()
            outside_event = outside / "event-target.jsonl"
            outside_event.write_text("unchanged\n", encoding="utf-8")
            (state / "events.jsonl").symlink_to(outside_event)
            with self.assertRaisesRegex(cg.GatewayError, "symlink ou junction"):
                cg.append_event(policy, {"result": "must-not-be-written"})
            self.assertEqual(outside_event.read_text(encoding="utf-8"), "unchanged\n")

    @unittest.skipIf(os.name == "nt", "dir_fd ancorado é validado em POSIX")
    def test_session_write_remains_anchored_when_parent_is_swapped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            sessions = state / "sessions"
            outside = root / "outside"
            sessions.mkdir(parents=True)
            outside.mkdir()
            policy = {"state_dir": str(state)}
            path = cg.secure_state_path(policy, "sessions", "session.json")
            original_open = cg.os.open
            swapped = False

            def swapping_open(name, flags, mode=0o777, *, dir_fd=None):
                nonlocal swapped
                if name == "session.json" and dir_fd is not None and not swapped:
                    swapped = True
                    sessions.rename(state / "sessions-original")
                    sessions.symlink_to(outside, target_is_directory=True)
                return original_open(name, flags, mode, dir_fd=dir_fd)

            with mock.patch.object(cg.os, "open", side_effect=swapping_open), self.assertRaisesRegex(
                cg.GatewayError,
                "mudou durante a operação",
            ):
                cg.write_state_text(policy, path, "anchored", exclusive=True)

            self.assertTrue(swapped)
            self.assertFalse((outside / "session.json").exists())
            self.assertEqual(
                (state / "sessions-original" / "session.json").read_text(encoding="utf-8"),
                "",
            )

    @unittest.skipIf(os.name == "nt", "dir_fd ancorado é validado em POSIX")
    def test_snapshot_replace_remains_anchored_when_parent_is_swapped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            snapshots = state / "snapshots"
            outside = root / "outside"
            snapshots.mkdir(parents=True)
            outside.mkdir()
            policy = {"state_dir": str(state)}
            target = cg.secure_state_path(policy, "snapshots", "session.json")
            cg.write_state_text(policy, target, "old", exclusive=True)
            outside_target = outside / "session.json"
            outside_target.write_text("outside", encoding="utf-8")
            original_replace = cg.os.replace
            swapped = False

            def swapping_replace(source, destination, *, src_dir_fd=None, dst_dir_fd=None):
                nonlocal swapped
                if src_dir_fd is not None and not swapped:
                    swapped = True
                    snapshots.rename(state / "snapshots-original")
                    snapshots.symlink_to(outside, target_is_directory=True)
                return original_replace(
                    source,
                    destination,
                    src_dir_fd=src_dir_fd,
                    dst_dir_fd=dst_dir_fd,
                )

            with mock.patch.object(
                cg.os,
                "replace",
                side_effect=swapping_replace,
            ), self.assertRaisesRegex(cg.GatewayError, "mudou durante a operação"):
                cg.atomic_write_state_text(policy, target, "new")

            self.assertTrue(swapped)
            self.assertEqual(outside_target.read_text(encoding="utf-8"), "outside")
            original = state / "snapshots-original"
            self.assertEqual((original / "session.json").read_text(encoding="utf-8"), "new")
            self.assertFalse(any(item.name.endswith(".tmp") for item in original.iterdir()))

    @unittest.skipIf(os.name == "nt", "dir_fd ancorado é validado em POSIX")
    def test_lock_unlink_remains_anchored_when_parent_is_swapped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            locks = state / "locks"
            outside = root / "outside"
            locks.mkdir(parents=True)
            outside.mkdir()
            policy = {"state_dir": str(state)}
            lock_path = cg.secure_state_path(policy, "locks", "repository.lock")
            identity = cg.write_state_text(policy, lock_path, "lock", exclusive=True)
            outside_lock = outside / "repository.lock"
            outside_lock.write_text("outside", encoding="utf-8")
            original_unlink = cg.os.unlink
            swapped = False

            def swapping_unlink(name, *, dir_fd=None):
                nonlocal swapped
                if name == "repository.lock" and dir_fd is not None and not swapped:
                    swapped = True
                    locks.rename(state / "locks-original")
                    locks.symlink_to(outside, target_is_directory=True)
                return original_unlink(name, dir_fd=dir_fd)

            with mock.patch.object(
                cg.os,
                "unlink",
                side_effect=swapping_unlink,
            ), self.assertRaisesRegex(cg.GatewayError, "mudou durante a operação"):
                cg.unlink_state_file(policy, lock_path, expected_identity=identity)

            self.assertTrue(swapped)
            self.assertEqual(outside_lock.read_text(encoding="utf-8"), "outside")
            self.assertFalse((state / "locks-original" / "repository.lock").exists())

    @unittest.skipIf(os.name == "nt", "hardlink do estado é validado em POSIX")
    def test_state_writes_reject_hardlinked_sentinels_before_modification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            snapshots = state / "snapshots"
            snapshots.mkdir(parents=True)
            policy = {"state_dir": str(state)}

            event_sentinel = root / "event-sentinel"
            event_sentinel.write_text("event-original", encoding="utf-8")
            os.link(event_sentinel, state / "events.jsonl")
            with self.assertRaisesRegex(cg.GatewayError, "hardlinks"):
                cg.append_event(policy, {"result": "must-not-be-written"})
            self.assertEqual(event_sentinel.read_text(encoding="utf-8"), "event-original")

            snapshot_sentinel = root / "snapshot-sentinel"
            snapshot_sentinel.write_text("snapshot-original", encoding="utf-8")
            target = snapshots / "session.json"
            os.link(snapshot_sentinel, target)
            with self.assertRaisesRegex(cg.GatewayError, "hardlinks"):
                cg.atomic_write_state_text(policy, target, "replacement")
            self.assertEqual(
                snapshot_sentinel.read_text(encoding="utf-8"),
                "snapshot-original",
            )

    @unittest.skipIf(os.name != "posix", "permissões POSIX não se aplicam ao Windows")
    def test_state_directories_reject_group_or_world_writable_components(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            state.mkdir()
            policy = {"state_dir": str(state)}

            state.chmod(0o777)
            with self.assertRaisesRegex(cg.GatewayError, "escrita de grupo/outros"):
                cg.state_dir(policy)

            state.chmod(0o700)
            sessions = state / "sessions"
            sessions.mkdir()
            sessions.chmod(0o770)
            with self.assertRaisesRegex(cg.GatewayError, "escrita de grupo/outros"):
                cg.secure_state_path(
                    policy,
                    "sessions",
                    "session.json",
                    create_parent=True,
                )

    @unittest.skipIf(os.name == "nt", "semântica de symlink validada em POSIX")
    def test_cwd_is_canonicalized_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "repository"
            target.mkdir()
            subprocess.run(["git", "init"], cwd=target, check=True, capture_output=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"],
                cwd=target,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Test"],
                cwd=target,
                check=True,
            )
            (target / "tracked.txt").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=target, check=True)
            subprocess.run(
                ["git", "commit", "-m", "base"],
                cwd=target,
                check=True,
                capture_output=True,
            )
            alias = root / "alias"
            alias.symlink_to(target, target_is_directory=True)
            self.assertEqual(cg.canonical_existing_directory(alias), target.resolve())
            policy_path = root / "policy.json"
            policy_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "allowed_roots": [str(target)],
                        "denied_roots": [],
                        "denied_segments": [],
                        "denied_names": [],
                        "allowed_executables": ["git"],
                        "blocked_executables": [],
                        "require_git_repo": True,
                        "state_dir": str(root / "state"),
                    }
                ),
                encoding="utf-8",
            )
            policy = cg.load_policy(policy_path)
            original_run = cg.subprocess.run
            executed_cwds: list[str] = []
            inherited_fds: list[tuple[int, ...]] = []

            def tracking_run(*args, **kwargs):
                if args and args[0] == ["git", "status", "--short"]:
                    executed_cwds.append(str(Path(kwargs["cwd"]).resolve(strict=True)))
                    inherited_fds.append(tuple(kwargs.get("pass_fds", ())))
                return original_run(*args, **kwargs)

            with mock.patch.object(cg.subprocess, "run", side_effect=tracking_run), redirect_stdout(
                io.StringIO()
            ):
                result = cg.execute(
                    alias,
                    policy,
                    ["git", "status", "--short"],
                    risk=1,
                    timeout=30,
                    expected_head=None,
                    allow_dirty=False,
                    allow_head_change=False,
                    correlation_id="canonical-cwd-test",
                )
            self.assertEqual(result, 0)
            self.assertEqual(executed_cwds, [str(target.resolve())])
            self.assertEqual(len(inherited_fds), 1)
            self.assertEqual(len(inherited_fds[0]), 1)

    @unittest.skipIf(os.name != "posix", "cwd por descritor é específico de POSIX")
    def test_anchored_cwd_survives_namespace_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = root / "repository"
            original.mkdir()
            (original / "marker.txt").write_text("original", encoding="utf-8")
            moved = root / "moved"

            with cg.anchored_subprocess_cwd(original) as (anchored, options):
                original.rename(moved)
                original.mkdir()
                (original / "marker.txt").write_text("replacement", encoding="utf-8")
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        "from pathlib import Path; print(Path('marker.txt').read_text())",
                    ],
                    cwd=anchored,
                    text=True,
                    capture_output=True,
                    check=True,
                    **options,
                )
            self.assertEqual(completed.stdout.strip(), "original")

    def test_worktree_root_must_be_covered_by_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(cg.GatewayError, "worktree_root"):
                cg.validate_runtime_policy_paths(
                    {
                        "version": 1,
                        "allowed_roots": [str(root / "repository")],
                        "worktree_root": str(root / "worktrees"),
                    }
                )

    def test_state_dir_fallback_remains_available_when_not_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(cg.Path, "home", return_value=root):
                self.assertEqual(cg.state_dir({}), root / ".reqsys-command-gateway")

    def test_state_dir_rejects_relative_and_unresolved_values(self) -> None:
        with self.assertRaisesRegex(cg.GatewayError, "caminho absoluto"):
            cg.state_dir({"state_dir": "relative/state"})
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(cg.GatewayError):
                cg.state_dir({"state_dir": "${REQSYS_TEST_UNSET_STATE}"})

    def test_posix_profile_does_not_allow_workspace_root(self) -> None:
        policy = cg.load_policy(POSIX_POLICY, enforce_platform=False, validate_paths=False)
        self.assertNotIn("${REQSYS_WORKSPACE_ROOT}", policy["allowed_roots"])
        self.assertNotIn("${REQSYS_WORKSPACE_ROOT}/*", policy["allowed_roots"])


if __name__ == "__main__":
    unittest.main()
