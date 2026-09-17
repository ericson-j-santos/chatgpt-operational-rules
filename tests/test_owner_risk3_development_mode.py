from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "scripts" / "set_owner_risk3_development_mode.py"
spec = importlib.util.spec_from_file_location("set_owner_risk3_development_mode", MODULE)
assert spec and spec.loader
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def config_path(tmp_path: Path) -> Path:
    return (tmp_path / "owner-risk3-exceptions.local.json").resolve()


def test_enable_creates_private_dev_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "owner_fingerprint", lambda: "owner-fingerprint")
    path = config_path(tmp_path)
    result = m.enable(path, 14)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert result["status"] == "enabled"
    assert result["environment"] == "dev"
    assert saved["owner_fingerprint"] == "owner-fingerprint"
    assert saved["development_mode"]["enabled"] is True
    assert saved["development_mode"]["reason"] == m.DEV_MODE_REASON
    assert saved["development_mode"]["reactivation_required_before_non_dev"] is True
    assert saved["development_mode"]["allowed_executables"] == ["python", "python3"]
    if os.name != "nt":
        assert path.stat().st_mode & 0o077 == 0


def test_enable_preserves_existing_actions(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "owner_fingerprint", lambda: "fp")
    path = config_path(tmp_path)
    path.write_text(json.dumps({
        "version": 1,
        "enabled": True,
        "owner_fingerprint": "fp",
        "actions": {"existing.action": {"scope": "x"}},
    }), encoding="utf-8")
    os.chmod(path, 0o600)
    m.enable(path, 7)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert "existing.action" in saved["actions"]


def test_disable_reactivates_allowlist_requirement(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "owner_fingerprint", lambda: "fp")
    path = config_path(tmp_path)
    m.enable(path, 7)
    result = m.disable(path)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert result == {"status": "disabled", "allowlist_required": True}
    assert saved["development_mode"]["enabled"] is False
    assert saved["development_mode"]["reactivation_required_before_non_dev"] is False


def test_status_never_exposes_owner_fingerprint(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "owner_fingerprint", lambda: "sensitive-fingerprint")
    path = config_path(tmp_path)
    m.enable(path, 3)
    result = m.status(path)
    assert result["status"] == "enabled"
    assert "sensitive-fingerprint" not in json.dumps(result)


@pytest.mark.parametrize("days", [0, 31, 365])
def test_enable_rejects_invalid_duration(tmp_path, monkeypatch, days):
    monkeypatch.setattr(m, "owner_fingerprint", lambda: "fp")
    with pytest.raises(m.ModeError, match="days"):
        m.enable(config_path(tmp_path), days)


def test_existing_config_must_match_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "owner_fingerprint", lambda: "expected")
    path = config_path(tmp_path)
    path.write_text(json.dumps({
        "version": 1,
        "enabled": True,
        "owner_fingerprint": "other",
        "actions": {},
    }), encoding="utf-8")
    os.chmod(path, 0o600)
    with pytest.raises(m.ModeError, match="outro usuário/máquina"):
        m.enable(path, 7)
