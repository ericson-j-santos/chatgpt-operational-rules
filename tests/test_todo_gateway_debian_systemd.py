import ast
import importlib.util
import json
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
    assert "docker" not in text.lower()


def test_pilot_manifest_builds_non_mutating_isolated_plan(tmp_path):
    module = _module()
    path = tmp_path / "pilot.json"
    path.write_text(json.dumps(_manifest()), encoding="utf-8")
    plan = module.build_pilot_dry_run(module.load_pilot_manifest(path))
    assert plan["status"] == "planned"
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


def test_pilot_collision_fails_closed():
    module = _module()
    plan = module.build_pilot_dry_run(
        _manifest(), occupied={"todo-pilot-desktop-dev-a.service"}
    )
    assert plan["status"] == "blocked"
    assert plan["collisions"] == ["todo-pilot-desktop-dev-a.service"]
    assert plan["runtime_mutated"] is False


def test_debian_harnesses_remain_non_github_hosted_and_dockerless():
    e2e = _text(E2E)
    resilience = _text(RESILIENCE)
    assert "GITHUB_ACTIONS" in e2e
    assert "GITHUB_ACTIONS" in resilience
    assert "docker" not in e2e.lower()
    assert "docker" not in resilience.lower()
