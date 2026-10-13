import ast
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install_todo_gateway_debian_systemd.py"
E2E = ROOT / "scripts" / "todo_gateway_debian_e2e.py"
RESILIENCE = ROOT / "scripts" / "todo_gateway_debian_resilience.py"


def _text(path: Path) -> str:
    value = path.read_text(encoding="utf-8")
    ast.parse(value)
    return value


def _module():
    spec = importlib.util.spec_from_file_location("todo_pilot_installer", INSTALLER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _manifest() -> dict:
    pilot = "desktop-dev-a"
    return {
        "schema_version": 1,
        "profile": "pilot-dev-isolated",
        "pilot_id": pilot,
        "host": "DESKTOP-PDQK954",
        "environment": "dev",
        "source_sha": "a" * 40,
        "release_sha": "a" * 40,
        "bind_host": "127.0.0.1",
        "gateway_port": 18210,
        "postgres_port": 15432,
        "service_name": f"todo-pilot-{pilot}.service",
        "database_name": "todo_pilot_desktop_dev_a_db",
        "database_role": "todo_pilot_desktop_dev_a_role",
        "base_dir": f"/opt/todo-pilots/{pilot}",
        "config_dir": f"/etc/todo-pilots/{pilot}",
        "evidence_dir": f"/var/lib/todo-pilots/{pilot}/evidence",
        "ownership_marker": f"/opt/todo-pilots/{pilot}/.pilot-ownership.json",
        "secrets_transport": "protected_file_no_argv",
    }


def test_installer_uses_native_systemd_and_local_postgres():
    text = _text(INSTALLER)
    assert 'APP_USER = "todo-global"' in text
    assert 'DB_NAME = "todo_global_bus_dev"' in text
    assert 'EnvironmentFile=/etc/todo-global/runtime.env' in text
    assert 'WantedBy=multi-user.target' in text
    assert '["systemctl", "enable", "--now", "todo-global-gateway.service"]' in text
    assert "docker" not in text.lower()


def test_installer_keeps_secrets_local_and_reuses_existing_values():
    text = _text(INSTALLER)
    assert 'current.get("TODO_GATEWAY_TOKEN") or secrets.token_urlsafe(40)' in text
    assert 'current.get("POSTGRES_PASSWORD") or secrets.token_urlsafe(32)' in text
    assert 'os.chmod(ENV_FILE, 0o640)' in text
    assert 'shutil.chown(ENV_FILE, user="root", group=APP_GROUP)' in text


def test_pilot_manifest_builds_static_non_mutating_plan(tmp_path):
    module = _module()
    path = tmp_path / "pilot.json"
    path.write_text(json.dumps(_manifest()), encoding="utf-8")
    plan = module.build_pilot_dry_run(module.load_pilot_manifest(path))
    assert plan["status"] == "static_plan"
    assert plan["inventory_verified"] is False
    assert plan["collisions_status"] == "not_verified"
    assert plan["dry_run"] is True
    assert plan["runtime_mutated"] is False
    assert plan["ready_for_install"] is False
    assert plan["bind_host"] == "127.0.0.1"
    assert plan["secrets_in_argv"] is False
    assert len(plan["manifest_sha256"]) == 64


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("environment", "prod", "pilot_identity_invalid"),
        ("source_sha", "abc", "pilot_release_sha_invalid"),
        ("bind_host", "0.0.0.0", "pilot_bind_must_be_loopback"),
        ("gateway_port", 8000, "pilot_port_collision"),
        ("postgres_port", 5432, "pilot_port_collision"),
        ("service_name", "todo-global-gateway.service", "pilot_resource_name_invalid"),
        ("base_dir", "/opt/todo-global", "pilot_path_not_isolated"),
        ("secrets_transport", "argv", "pilot_secrets_transport_invalid"),
    ],
)
def test_pilot_manifest_rejects_unsafe_values(tmp_path, field, value, error):
    module = _module()
    payload = _manifest()
    payload[field] = value
    path = tmp_path / "pilot.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match=error):
        module.load_pilot_manifest(path)


def test_verified_pilot_collision_fails_closed():
    module = _module()
    plan = module.build_pilot_dry_run(
        _manifest(), occupied={"todo-pilot-desktop-dev-a.service"}
    )
    assert plan["status"] == "blocked"
    assert plan["inventory_verified"] is True
    assert plan["collisions_status"] == "verified"
    assert plan["collisions"] == ["todo-pilot-desktop-dev-a.service"]
    assert plan["runtime_mutated"] is False


def test_cli_pilot_dry_run_never_calls_mutators(tmp_path, monkeypatch, capsys):
    module = _module()
    manifest = tmp_path / "pilot.json"
    manifest.write_text(json.dumps(_manifest()), encoding="utf-8")
    called = []
    for name in (
        "require_root", "install_packages", "ensure_service_user",
        "ensure_runtime_env", "ensure_postgres", "install_release",
        "install_unit", "smoke", "run",
    ):
        monkeypatch.setattr(module, name, lambda *args, _name=name, **kwargs: called.append(_name))
    monkeypatch.setattr(sys, "argv", ["installer", "--pilot-manifest", str(manifest), "--dry-run"])
    assert module.main() == 0
    assert called == []
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "static_plan"
    assert result["inventory_verified"] is False
    assert result["collisions_status"] == "not_verified"


@pytest.mark.parametrize(
    "argv,error",
    [
        (["installer", "--pilot-manifest", "pilot.json"], "pilot_profile_requires_dry_run"),
        (["installer", "--dry-run"], "dry_run_requires_pilot_manifest"),
    ],
)
def test_cli_rejects_incomplete_dry_run_modes(monkeypatch, argv, error):
    module = _module()
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(RuntimeError, match=error):
        module.main()


def test_debian_e2e_proves_replay_and_independent_sql_readback():
    text = _text(E2E)
    assert '"event_replay_idempotent"' in text
    assert '"continuation_replay_idempotent"' in text
    assert '"sql_event_readback"' in text
    assert '"sql_continuation_readback"' in text
    assert '"runuser", "-u", "postgres"' in text
    assert "docker" not in text.lower()


def test_debian_resilience_proves_restart_and_backup_restore():
    text = _text(RESILIENCE)
    assert '["systemctl", "restart", "postgresql"]' in text
    assert '["systemctl", "restart", "todo-global-gateway.service"]' in text
    assert '"pg_dump"' in text
    assert '"pg_restore"' in text
    assert '"restore_verified"' in text
    assert "docker" not in text.lower()
