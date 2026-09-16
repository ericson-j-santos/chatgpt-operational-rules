from __future__ import annotations

import importlib.util
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "scripts" / "owner_risk3_gateway.py"
spec = importlib.util.spec_from_file_location("owner_risk3_gateway", MODULE)
assert spec and spec.loader
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def action(**overrides):
    payload = {
        "environment": "dev",
        "scope": "/subscriptions/s/resourceGroups/rg/providers/Microsoft.KeyVault/vaults/kv",
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        "command": [
            "az", "role", "assignment", "create", "--assignee-object-id", "principal",
            "--role", "Key Vault Secrets User", "--scope",
            "/subscriptions/s/resourceGroups/rg/providers/Microsoft.KeyVault/vaults/kv",
        ],
    }
    payload.update(overrides)
    return payload


def test_global_gateway_source_keeps_risk3_denied():
    source = (ROOT / "scripts" / "command_gateway.py").read_text(encoding="utf-8")
    assert "if risk not in (1, 2):" in source
    assert "risco 3 não é executado automaticamente" in source


def test_allows_exact_dev_action():
    a = action()
    assert m.validate_action("azure.kv.role_assignment.add.minimal_dev", a["scope"], a) == a["command"]


@pytest.mark.parametrize("environment", ["prod", "production", "stg"])
def test_denies_non_dev_local(environment):
    a = action(environment=environment)
    with pytest.raises(m.Risk3Error, match="limitado a local/dev"):
        m.validate_action("azure.kv.role_assignment.add.minimal_dev", a["scope"], a)


def test_denies_scope_mismatch():
    a = action()
    with pytest.raises(m.Risk3Error, match="escopo"):
        m.validate_action("azure.kv.role_assignment.add.minimal_dev", "/other", a)


def test_denies_production_marker_in_scope_and_command():
    prod_scope = "/subscriptions/s/resourceGroups/rg-prod/providers/Microsoft.KeyVault/vaults/kv"
    a = action(scope=prod_scope)
    with pytest.raises(m.Risk3Error, match="produção"):
        m.validate_action("azure.kv.role.assignment.dev", prod_scope, a)
    a = action(command=["tool", "deploy", "reqsys-production"])
    with pytest.raises(m.Risk3Error, match="produção"):
        m.validate_action("tool.deploy.dev", a["scope"], a)


@pytest.mark.parametrize("role", ["Owner", "Contributor", "User Access Administrator", "Role Based Access Control Administrator"])
def test_denies_broad_roles(role):
    a = action(command=["az", "role", "assignment", "create", "--role", role])
    with pytest.raises(m.Risk3Error, match="papel administrativo"):
        m.validate_action("azure.role.assignment.dev", a["scope"], a)


@pytest.mark.parametrize("token", ["delete", "purge", "destroy", "drop", "prune", "reset"])
def test_denies_destructive_tokens(token):
    a = action(command=["az", "resource", token, "--scope", "x"])
    with pytest.raises(m.Risk3Error, match="destrutiva"):
        m.validate_action("azure.resource.dev", a["scope"], a)


def test_denies_direct_secret_operation():
    a = action(command=["az", "keyvault", "secret", "show", "--vault-name", "kv"])
    with pytest.raises(m.Risk3Error, match="segredos"):
        m.validate_action("azure.kv.secret.read.dev", a["scope"], a)


def test_denies_shell_and_metacharacters():
    a = action(command=["powershell", "-Command", "Write-Host ok"])
    with pytest.raises(m.Risk3Error, match="executável proibido"):
        m.validate_action("local.shell.dev", a["scope"], a)
    a = action(command=["az", "role", "assignment", "create", "&&", "echo"])
    with pytest.raises(m.Risk3Error, match="shell"):
        m.validate_action("azure.role.assignment.dev", a["scope"], a)


def test_denies_secret_options():
    a = action(command=["tool", "run", "--client-secret", "x"])
    with pytest.raises(m.Risk3Error, match="segredo/credencial"):
        m.validate_action("tool.safe.dev", a["scope"], a)


def test_denies_expired_action():
    a = action(expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
    with pytest.raises(m.Risk3Error) as exc:
        m.validate_action("azure.kv.role.assignment.add.minimal_dev", a["scope"], a)
    assert exc.value.exit_code == m.EXIT_EXPIRED


def test_load_config_is_bound_to_current_owner(tmp_path, monkeypatch):
    cfg = tmp_path / "owner-risk3-exceptions.local.json"
    cfg.write_text(json.dumps({"version": 1, "enabled": True, "owner_fingerprint": "wrong", "actions": {}}), encoding="utf-8")
    os.chmod(cfg, 0o600)
    with pytest.raises(m.Risk3Error, match="não pertence"):
        m.load_local_config(cfg.resolve())


def test_load_config_accepts_current_owner(tmp_path):
    cfg = tmp_path / "owner-risk3-exceptions.local.json"
    cfg.write_text(json.dumps({"version": 1, "enabled": True, "owner_fingerprint": m.owner_fingerprint(), "actions": {}}), encoding="utf-8")
    os.chmod(cfg, 0o600)
    loaded = m.load_local_config(cfg.resolve())
    assert loaded["enabled"] is True
