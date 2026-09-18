import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "todo_gateway_windows_headless_boot.py"
E2E = ROOT / "scripts" / "todo_gateway_pc24x7_e2e.py"
INSTALLER = ROOT / "scripts" / "install_todo_gateway_windows_s4u.ps1"
NOTERI_INSTALLER = ROOT / "scripts" / "install_pc24x7_noteri_windows_s4u.ps1"
BOOT_SURVIVAL = ROOT / "scripts" / "pc24x7_boot_survival_dev.py"
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
    assert '"todo-global-headless-boot-v4"' in text
    assert 'e2e_evidence.unlink(missing_ok=True)' in text
    assert '"e2e_evidence_fresh"' in text
    assert '"-m", "scripts.todo_gateway_pc24x7_e2e"' in text
    assert '"e2e_ready"' in text
    assert '"e2e_event_id"' in text
    assert '"e2e_request_id"' in text
    assert 'result["ready"] = e2e_rc == 0 and result["e2e_evidence_fresh"] and result["e2e_ready"]' in text


def test_s4u_e2e_resolves_docker_without_relying_only_on_path():
    text = E2E.read_text(encoding="utf-8")
    ast.parse(text)
    assert 'shutil.which("docker")' in text
    assert 'DockerDesktop/resources/bin/docker.exe' in text
    assert '[docker_executable(), "exec", DB_CONTAINER' in text


def test_s4u_e2e_retries_only_connection_errors_with_a_bound():
    text = E2E.read_text(encoding="utf-8")
    ast.parse(text)
    assert "HTTPError, URLError" in text
    assert "API_CONNECT_ATTEMPTS = 10" in text
    assert "API_CONNECT_RETRY_SECONDS = 2.0" in text
    assert "for attempt in range(1, API_CONNECT_ATTEMPTS + 1):" in text
    assert "except HTTPError as exc:" in text
    assert "except URLError:" in text
    assert "time.sleep(API_CONNECT_RETRY_SECONDS)" in text
    assert "if attempt >= API_CONNECT_ATTEMPTS:" in text


def test_headless_runner_emits_stage_beacons_before_terminal_result():
    text = RUNNER.read_text(encoding="utf-8")
    ast.parse(text)
    for stage in (
        "runner_started",
        "postboot_started",
        "postboot_completed",
        "e2e_started",
        "e2e_completed",
    ):
        assert f'stage="{stage}"' in text
    assert '"terminal": terminal' in text
    assert "socket.create_connection" in text


def test_headless_runner_bounds_postboot_and_e2e_runtime():
    text = RUNNER.read_text(encoding="utf-8")
    assert 'parser.add_argument("--postboot-timeout", type=int, default=480)' in text
    assert 'parser.add_argument("--e2e-timeout", type=int, default=180)' in text
    assert "time.monotonic() + timeout_seconds" in text
    assert '"postboot_timeout"' in text
    assert '"e2e_timeout"' in text
    assert "return 13" in text
    assert "return 14" in text


def test_installer_uses_s4u_at_startup_without_password_and_passes_repo_root():
    text = INSTALLER.read_text(encoding="utf-8")
    assert "New-ScheduledTaskTrigger -AtStartup" in text
    assert "-LogonType S4U" in text
    assert "-RunLevel Limited" in text
    assert "Password" not in text
    assert "StartWhenAvailable" in text
    assert "[Parameter(Mandatory=$true)][string]$RepoRoot" in text
    assert '--repo-root "' in text


def test_noteri_installer_uses_s4u_at_startup_without_password():
    text = NOTERI_INSTALLER.read_text(encoding="utf-8")
    assert "New-ScheduledTaskTrigger -AtStartup" in text
    assert "-LogonType S4U" in text
    assert "-RunLevel Limited" in text
    assert "Password" not in text
    assert "StartWhenAvailable" in text
    assert "deploy_pc24x7_noteri_ha_worker_dev.py" in text


def test_boot_survival_installer_is_versioned_and_never_reboots():
    text = BOOT_SURVIVAL.read_text(encoding="utf-8")
    ast.parse(text)
    assert "schtasks.exe" in text
    assert "install_desktop" in text
    assert "install_noteri" in text
    assert "shutdown" not in text.casefold()
    assert "restart-computer" not in text.casefold()
    assert '"S4U"' in text
    assert '"BootTrigger"' in text


def test_beacon_listener_collects_progress_until_terminal_or_timeout():
    text = LISTENER.read_text(encoding="utf-8")
    ast.parse(text)
    assert "while len(records) < args.max_events" in text
    assert "time.monotonic() + args.timeout" in text
    assert 'decoded.get("terminal") is True' in text
    assert '"terminal_received"' in text
    assert '"timed_out"' in text
    assert "persist(out, records" in text
    assert "return 2 if records else 3" in text
