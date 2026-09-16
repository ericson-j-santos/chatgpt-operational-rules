import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "todo_gateway_windows_headless_boot.py"
INSTALLER = ROOT / "scripts" / "install_todo_gateway_windows_s4u.ps1"
LISTENER = ROOT / "scripts" / "todo_gateway_headless_beacon_listener.py"


def test_headless_runner_is_fail_closed_for_interactive_sessions():
    text = RUNNER.read_text(encoding="utf-8")
    ast.parse(text)
    assert "blocked_interactive_session" in text
    assert "interactive_session_appeared" in text
    assert "return 10" in text
    assert "return 11" in text
    assert "postboot_ready" in text


def test_headless_runner_requires_fresh_e2e_before_ready():
    text = RUNNER.read_text(encoding="utf-8")
    assert '"todo-global-headless-boot-v3"' in text
    assert '"-m", "scripts.todo_gateway_pc24x7_e2e"' in text
    assert '"e2e_ready"' in text
    assert '"e2e_event_id"' in text
    assert '"e2e_request_id"' in text
    assert '"status"] = "ready" if result["ready"] else "e2e_failed"' in text


def test_headless_runner_emits_independent_beacon():
    text = RUNNER.read_text(encoding="utf-8")
    assert "socket.create_connection" in text
    assert '"ready"' in text
    assert '"completed_at"' in text
    assert 'result["beacon_sent"] = True' in text


def test_installer_uses_s4u_at_startup_without_password_and_passes_repo_root():
    text = INSTALLER.read_text(encoding="utf-8")
    assert "New-ScheduledTaskTrigger -AtStartup" in text
    assert "-LogonType S4U" in text
    assert "-RunLevel Limited" in text
    assert "Password" not in text
    assert "StartWhenAvailable" in text
    assert "[Parameter(Mandatory=$true)][string]$RepoRoot" in text
    assert '--repo-root "' in text


def test_beacon_listener_is_bounded_and_persists_evidence():
    text = LISTENER.read_text(encoding="utf-8")
    ast.parse(text)
    assert "server.settimeout(args.timeout)" in text
    assert "out.write_text" in text
    assert "received_at" in text
    assert "payload" in text
