import json
import signal
import stat
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import scripts.install_todo_gateway_debian_systemd as canonical_installer
import scripts.todo_gateway_debian_installer_isolated_e2e as isolated_harness
from scripts.todo_gateway_debian_installer_isolated_e2e import (
    HomologationError,
    PSQL_ARGS,
    _run,
    assert_replay_unchanged,
    assert_local_database_url,
    assert_sensitive_values_absent,
    audit_psql_execs,
    installer_failure_diagnostic,
    parse_psql_exec_argv,
    validate_execution_context,
    _run_installer,
)

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "validate-rules.yml"


def _env() -> dict[str, str]:
    return {
        "GITHUB_ACTIONS": "true",
        "TODO22_RUNNER_ENVIRONMENT": "github-hosted",
        "GITHUB_REPOSITORY": "ericson-j-santos/chatgpt-operational-rules",
        "GITHUB_EVENT_NAME": "pull_request",
        "TODO22_SOURCE_REF": "fix/post-merge-homologation-gate-20261019",
        "TODO22_PR_HEAD_REPOSITORY": "ericson-j-santos/chatgpt-operational-rules",
        "TODO22_PR_AUTHOR": "ericson-j-santos",
    }


def _trace(arguments: list[str]) -> str:
    rendered = ", ".join(repr(argument) for argument in arguments)
    return (
        '812 execve("/usr/bin/psql", '
        f"[{rendered}], 0x7fffd000 /* 12 vars */) = 0\n"
    )


def test_context_accepts_only_clean_github_hosted_systemd_vm():
    validate_execution_context(
        _env(),
        euid=0,
        pid1_name="systemd",
        in_container=False,
        collisions=[],
    )


@pytest.mark.parametrize(
    ("env_update", "euid", "pid1_name", "in_container", "collisions", "error"),
    [
        ({"TODO22_RUNNER_ENVIRONMENT": "self-hosted"}, 0, "systemd", False, [], "github_hosted"),
        ({"TODO22_SOURCE_REF": "bad ref"}, 0, "systemd", False, [], "ref_mismatch"),
        ({"TODO22_PR_HEAD_REPOSITORY": "fork/repo"}, 0, "systemd", False, [], "pull_request_repository"),
        ({"TODO22_PR_AUTHOR": "other-user"}, 0, "systemd", False, [], "pull_request_owner"),
        ({"GITHUB_EVENT_NAME": "schedule"}, 0, "systemd", False, [], "github_event_not_allowed"),
        ({}, 1000, "systemd", False, [], "root_required"),
        ({}, 0, "bash", False, [], "systemd_vm"),
        ({}, 0, "systemd", True, [], "systemd_vm"),
        ({}, 0, "systemd", False, ["/opt/todo-global"], "target_collision"),
    ],
)
def test_context_fails_closed_outside_disposable_vm(
    env_update, euid, pid1_name, in_container, collisions, error
):
    env = _env()
    env.update(env_update)
    with pytest.raises(HomologationError, match=error):
        validate_execution_context(
            env,
            euid=euid,
            pid1_name=pid1_name,
            in_container=in_container,
            collisions=collisions,
        )


def test_context_accepts_main_push_and_verified_manual_pr_dispatch():
    cases = (
        ("push", "main", "false"),
        ("workflow_dispatch", "main", "false"),
        ("workflow_dispatch", "fix/verified-owner-pr", "true"),
    )
    for event_name, source_ref, manual_pr_verified in cases:
        env = _env()
        env.update(
            {
                "GITHUB_EVENT_NAME": event_name,
                "TODO22_SOURCE_REF": source_ref,
                "TODO22_PR_HEAD_REPOSITORY": "",
                "TODO22_PR_AUTHOR": "",
                "TODO22_MANUAL_PR_VERIFIED": manual_pr_verified,
            }
        )
        validate_execution_context(
            env,
            euid=0,
            pid1_name="systemd",
            in_container=False,
            collisions=[],
        )


@pytest.mark.parametrize(
    ("event_name", "source_ref"),
    [
        ("push", "feature/not-main"),
        ("workflow_dispatch", "feature/not-allowlisted"),
    ],
)
def test_context_rejects_untrusted_non_pr_refs(event_name, source_ref):
    env = _env()
    env.update(
        {
            "GITHUB_EVENT_NAME": event_name,
            "TODO22_SOURCE_REF": source_ref,
            "TODO22_PR_HEAD_REPOSITORY": "",
            "TODO22_PR_AUTHOR": "",
        }
    )
    with pytest.raises(HomologationError, match="ref_mismatch"):
        validate_execution_context(
            env,
            euid=0,
            pid1_name="systemd",
            in_container=False,
            collisions=[],
        )


def test_trace_parser_and_psql_contract_accept_stdin_only_invocation():
    argv = ["psql", *PSQL_ARGS]
    trace = _trace(argv)
    assert parse_psql_exec_argv(trace) == [argv]
    assert audit_psql_execs([trace], ["synthetic-password"]) == 1


def test_strace_uses_a_distinct_output_file_without_alignment_argument(tmp_path: Path):
    completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
    with patch(
        "scripts.todo_gateway_debian_installer_isolated_e2e.shutil.which",
        return_value="/usr/bin/strace",
    ), patch(
        "scripts.todo_gateway_debian_installer_isolated_e2e._run",
        return_value=completed,
    ) as invoked:
        assert _run_installer(
            Path("/checkout"),
            tmp_path / "install.strace",
            skip_packages=True,
            timeout=300,
        ) is completed
    args = invoked.call_args.args[0]
    assert "-a" not in args
    assert "--kill-on-exit" in args
    assert args[args.index("-o") + 1] == (tmp_path / "install.strace").as_posix()
    assert stat.S_IMODE((tmp_path / "install.strace").stat().st_mode) == 0o600


def test_trace_path_must_be_new_and_cannot_be_a_symlink(tmp_path: Path):
    existing = tmp_path / "existing.strace"
    existing.write_text("preserve", encoding="utf-8")
    existing.chmod(0o600)
    symlink = tmp_path / "linked.strace"
    symlink.symlink_to(existing)

    with patch(
        "scripts.todo_gateway_debian_installer_isolated_e2e.shutil.which",
        return_value="/usr/bin/strace",
    ):
        for trace in (existing, symlink):
            with pytest.raises(HomologationError, match="trace_path_invalid"):
                _run_installer(
                    Path("/checkout"),
                    trace,
                    skip_packages=True,
                    timeout=300,
                )
    assert existing.read_text(encoding="utf-8") == "preserve"


def test_failed_installer_diagnostic_is_allowlisted_and_never_echoes_output(
    tmp_path: Path,
):
    secret = "synthetic-password-must-not-escape"
    trace = tmp_path / "install.strace"
    trace.write_text(_trace(["psql", *PSQL_ARGS]), encoding="utf-8")
    trace.chmod(0o600)
    result = subprocess.CompletedProcess(
        ["strace"],
        1,
        stdout=f"unexpected {secret}",
        stderr=(
            "Traceback (most recent call last):\n"
            "RuntimeError: command failed rc=100: apt-get\n"
            f"hidden {secret}\n"
        ),
    )

    diagnostic = installer_failure_diagnostic(result, trace, "expected-ready")

    assert diagnostic == {
        "returncode": 1,
        "stdout_contract_match": False,
        "stderr_empty": False,
        "failure_kind": "installer_command_failed",
        "failed_command": "apt-get",
        "command_returncode": 100,
        "last_exec": "psql",
    }
    assert secret not in json.dumps(diagnostic, sort_keys=True)


def test_command_timeout_terminates_the_entire_process_group():
    process = MagicMock(pid=4321, returncode=-signal.SIGTERM)
    process.communicate.side_effect = [
        subprocess.TimeoutExpired(["synthetic-command"], 1),
        ("", ""),
    ]
    with patch(
        "scripts.todo_gateway_debian_installer_isolated_e2e.subprocess.Popen",
        return_value=process,
    ) as popen, patch(
        "scripts.todo_gateway_debian_installer_isolated_e2e.os.killpg"
    ) as killpg:
        with pytest.raises(HomologationError, match="command_timeout"):
            _run(["synthetic-command"], timeout=1)
    assert popen.call_args.kwargs["start_new_session"] is True
    killpg.assert_called_once_with(4321, signal.SIGTERM)


def test_database_url_must_match_the_generated_local_postgres_credentials():
    values = {
        "POSTGRES_PASSWORD": "synthetic-password",
        "DATABASE_URL": (
            "postgresql://todo_global_bus_dev_user:synthetic-password@"
            "127.0.0.1:5432/todo_global_bus_dev"
        ),
    }
    assert_local_database_url(values)
    for invalid_url in (
        "postgresql://todo_global_bus_dev_user:synthetic-password@neon.example:5432/todo_global_bus_dev",
        "postgresql://todo_global_bus_dev_user:wrong@127.0.0.1:5432/todo_global_bus_dev",
        "postgresql://todo_global_bus_dev_user:synthetic-password@127.0.0.1:5433/todo_global_bus_dev",
        "postgresql://todo_global_bus_dev_user:synthetic-password@127.0.0.1:5432/todo_global_bus_dev?sslmode=require",
    ):
        with pytest.raises(HomologationError, match="runtime_database_url_not_local"):
            assert_local_database_url({**values, "DATABASE_URL": invalid_url})


@pytest.mark.parametrize(
    ("argument", "error"),
    [
        ("SELECT rolname FROM pg_roles", "sql_present_in_argv"),
        ("synthetic-password", "secret_present_in_argv"),
        ("--command-file=/tmp/query.sql", "psql_argv_contract_failed"),
    ],
)
def test_psql_audit_detects_injected_sensitive_or_unapproved_argument(argument, error):
    trace = _trace(["psql", *PSQL_ARGS, argument])
    with pytest.raises(HomologationError, match=error):
        audit_psql_execs([trace], ["synthetic-password"])


def test_sensitive_output_scanner_detects_generated_credentials():
    assert_sensitive_values_absent(["installer completed"], ["synthetic-password"])
    with pytest.raises(HomologationError, match="secret_present_in_output"):
        assert_sensitive_values_absent(
            ["failure: synthetic-password"],
            ["synthetic-password"],
        )


def test_replay_comparator_detects_current_or_sentinel_mutation():
    before = {
        "release_fingerprint": "a" * 64,
        "current_inode": 100,
        "sentinel": "preserve-me",
    }
    assert_replay_unchanged(before, dict(before))
    after = dict(before)
    after["sentinel"] = "changed"
    with pytest.raises(HomologationError, match="replay_mutated"):
        assert_replay_unchanged(before, after)


def test_activation_journal_contract_and_residuals_fail_closed(tmp_path, monkeypatch):
    assert canonical_installer.ACTIVATION_JOURNAL_NAME == ".todo-activation.json"
    assert (
        isolated_harness.ACTIVATION_JOURNAL
        == canonical_installer._activation_journal_path()
    )

    journal = tmp_path / canonical_installer.ACTIVATION_JOURNAL_NAME
    monkeypatch.setattr(isolated_harness, "ACTIVATION_JOURNAL", journal)
    isolated_harness.assert_activation_journal_absent("initial")

    journal.write_text("residual", encoding="utf-8")
    with pytest.raises(HomologationError, match=r"^activation_journal_after_replay$"):
        isolated_harness.assert_activation_journal_absent("replay")
    assert journal.read_text(encoding="utf-8") == "residual"

    journal.unlink()
    journal.symlink_to(tmp_path / "missing-journal-target")
    with pytest.raises(HomologationError, match=r"^activation_journal_not_cleared$"):
        isolated_harness.assert_activation_journal_absent("rollback")
    assert journal.is_symlink()
    assert not journal.exists()


def test_isolated_homologation_is_automatic_same_repo_and_reuses_canonical_installer():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    job = workflow.split("  debian-installer-isolated:\n", 1)[1]
    assert "run-name: Operational Rules / ${{ inputs.correlation_id || github.ref_name }}" in workflow
    assert "inputs:\n      expected_sha:" in workflow
    assert "      correlation_id:" in workflow
    assert "pull-requests: read" in workflow
    assert "id: bind" in workflow
    assert "manual_pr_verified=true" in workflow
    assert "Manual non-main dispatch requires one exact owner draft PR" in workflow
    assert "github.event_name == 'workflow_dispatch'" in job
    assert "github.event_name == 'push'" in job
    assert "github.event_name == 'pull_request'" in job
    assert "github.event.pull_request.head.repo.full_name == github.repository" in job
    assert "github.event.pull_request.user.login == 'ericson-j-santos'" in job
    assert "needs: validate" in job
    assert "runs-on: ubuntu-24.04" in job
    assert "persist-credentials: false" in job
    assert "TODO22_RUNNER_ENVIRONMENT: ${{ runner.environment }}" in job
    assert 'TODO22_SOURCE_REF="$SOURCE_REF"' in job
    assert 'TODO22_PR_HEAD_REPOSITORY="$PR_HEAD_REPOSITORY"' in job
    assert 'TODO22_PR_AUTHOR="$PR_AUTHOR"' in job
    assert 'TODO22_MANUAL_PR_VERIFIED="$MANUAL_PR_VERIFIED"' in job
    assert "scripts/todo_gateway_debian_installer_isolated_e2e.py" in job
    assert "--preflight-only" in job
    harness = (ROOT / "scripts" / "todo_gateway_debian_installer_isolated_e2e.py").read_text(
        encoding="utf-8"
    )
    assert 'INSTALLER = Path("scripts/install_todo_gateway_debian_systemd.py")' in harness


@pytest.mark.parametrize("stage", ["initial", "replay", "rollback"])
def test_real_installer_journal_blocks_cleanup_claim_until_removed(tmp_path, monkeypatch, stage):
    harness_relative = isolated_harness.ACTIVATION_JOURNAL.relative_to(isolated_harness.BASE)
    monkeypatch.setattr(isolated_harness, "ACTIVATION_JOURNAL", tmp_path / harness_relative)
    monkeypatch.setattr(canonical_installer, "BASE", tmp_path)
    nonce = "b" * 32
    candidate = tmp_path / "releases" / ("a" * 12)
    previous = canonical_installer.CurrentSnapshot(False, None, None, None, None)
    canonical_installer._write_activation_journal(candidate, previous, nonce)
    journal = canonical_installer._activation_journal_path()
    before_bytes = journal.read_bytes()
    before_inode = journal.stat().st_ino

    def require_residual_journal_rejection():
        with pytest.raises(HomologationError, match="activation_journal_"):
            isolated_harness.assert_activation_journal_absent(stage)

    require_residual_journal_rejection()
    require_residual_journal_rejection()
    assert journal.read_bytes() == before_bytes
    assert journal.stat().st_ino == before_inode

    # Prove the regression assertion rejects the historical wrong-path implementation.
    with monkeypatch.context() as mutant:
        mutant.setattr(isolated_harness, "ACTIVATION_JOURNAL", tmp_path / ".activation-journal.json")
        with pytest.raises(pytest.fail.Exception, match="DID NOT RAISE"):
            require_residual_journal_rejection()
    require_residual_journal_rejection()

    canonical_installer._clear_activation_journal(nonce)
    isolated_harness.assert_activation_journal_absent(stage)
    isolated_harness.assert_activation_journal_absent(stage)
    assert not journal.exists()
    assert not journal.is_symlink()


@pytest.mark.parametrize("residual_kind", ["broken_symlink", "directory"])
def test_residual_journal_is_rejected_without_mutation(
    tmp_path, monkeypatch, residual_kind
):
    stage = "rollback"
    harness_relative = isolated_harness.ACTIVATION_JOURNAL.relative_to(isolated_harness.BASE)
    monkeypatch.setattr(isolated_harness, "ACTIVATION_JOURNAL", tmp_path / harness_relative)
    monkeypatch.setattr(canonical_installer, "BASE", tmp_path)
    journal = canonical_installer._activation_journal_path()
    target = tmp_path / "missing-target"
    if residual_kind == "broken_symlink":
        journal.symlink_to(target)
        assert not journal.exists()
    else:
        journal.mkdir()
    before = journal.lstat()

    with pytest.raises(HomologationError, match="activation_journal_"):
        isolated_harness.assert_activation_journal_absent(stage)

    after = journal.lstat()
    assert (after.st_dev, after.st_ino, after.st_mode) == (
        before.st_dev, before.st_ino, before.st_mode
    )
    if residual_kind == "broken_symlink":
        assert journal.is_symlink()
        assert journal.readlink() == target
        assert not target.exists()


@pytest.mark.parametrize(
    ("returncode", "stdout_match", "stderr_present"),
    [(7, False, True), (0, False, False), (0, True, True)],
)
def test_failed_initial_process_persists_false_receipt_without_raw_output(
    tmp_path, monkeypatch, capsys, returncode, stdout_match, stderr_present
):
    # Real child-process I/O and receipt filesystem; the installer and VM are simulated.
    secret = "synthetic-receipt-secret-must-not-escape"
    expected_sha = "a" * 40
    expected_stdout = f"TODO_GLOBAL_DEBIAN_READY=true release={expected_sha[:12]}"
    stdout = expected_stdout if stdout_match else secret
    stderr = f"unclassified failure {secret}" if stderr_present else ""
    command = (
        f"import sys; sys.stdout.write({stdout!r}); "
        f"sys.stderr.write({stderr!r}); sys.exit({returncode})"
    )
    result = _run([sys.executable, "-c", command], timeout=10)
    assert result.returncode == returncode
    assert result.stdout == stdout
    assert result.stderr == stderr
    calls = []

    def simulated_installer(repo_root, trace_path, *, skip_packages, timeout):
        calls.append((repo_root, skip_packages, timeout))
        trace_path.write_text(_trace(["psql", *PSQL_ARGS]), encoding="utf-8")
        trace_path.chmod(0o600)
        return result

    monkeypatch.setattr(isolated_harness, "preflight", lambda *args: None)
    monkeypatch.setattr(isolated_harness, "_run_installer", simulated_installer)
    evidence_path = tmp_path / "evidence.json"
    exit_code = isolated_harness.homologate(
        tmp_path,
        expected_sha,
        "synthetic-process-contract",
        evidence_path,
        {**_env(), "RUNNER_TEMP": str(tmp_path)},
    )

    receipt_text = evidence_path.read_text(encoding="utf-8")
    receipt = json.loads(receipt_text)
    assert exit_code == 1
    assert calls == [(tmp_path, False, 900)]
    assert receipt["ready"] is False
    assert receipt["error"] == "initial_install_failed"
    diagnostic = receipt["initial_process_diagnostic"]
    assert diagnostic["returncode"] == returncode
    assert diagnostic["stdout_contract_match"] is stdout_match
    assert diagnostic["stderr_empty"] is (not stderr_present)
    assert "activation_journal_cleared" not in receipt
    assert secret not in receipt_text
    assert secret not in capsys.readouterr().out
    assert list(tmp_path.glob("todo22-installer-audit-*")) == []
