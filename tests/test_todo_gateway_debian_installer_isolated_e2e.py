import subprocess
import signal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scripts.todo_gateway_debian_installer_isolated_e2e import (
    HomologationError,
    PSQL_ARGS,
    _run,
    assert_replay_unchanged,
    assert_local_database_url,
    assert_sensitive_values_absent,
    audit_psql_execs,
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
        "GITHUB_REF_NAME": "fix/todo22-installer-isolated-gate-20261019",
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
        ({"GITHUB_REF_NAME": "main"}, 0, "systemd", False, [], "ref_mismatch"),
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


def test_isolated_homologation_is_manual_hosted_and_reuses_canonical_installer():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    job = workflow.split("  debian-installer-isolated:\n", 1)[1]
    assert "run-name: Operational Rules / ${{ inputs.correlation_id || github.ref_name }}" in workflow
    assert "inputs:\n      expected_sha:" in workflow
    assert "      correlation_id:" in workflow
    assert "if: github.event_name == 'workflow_dispatch'" in job
    assert "needs: validate" in job
    assert "runs-on: ubuntu-24.04" in job
    assert "persist-credentials: false" in job
    assert "TODO22_RUNNER_ENVIRONMENT: ${{ runner.environment }}" in job
    assert "scripts/todo_gateway_debian_installer_isolated_e2e.py" in job
    assert "--preflight-only" in job
    harness = (ROOT / "scripts" / "todo_gateway_debian_installer_isolated_e2e.py").read_text(
        encoding="utf-8"
    )
    assert 'INSTALLER = Path("scripts/install_todo_gateway_debian_systemd.py")' in harness
