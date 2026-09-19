from __future__ import annotations

import importlib.util
import json
import os
import socket
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "scripts" / "owner_host_power_once.py"
spec = importlib.util.spec_from_file_location("owner_host_power_once", MODULE)
assert spec and spec.loader
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def config_path(tmp_path: Path) -> Path:
    return tmp_path / "owner-host-power-once.local.json"


def authorize(tmp_path: Path, **overrides):
    kwargs = {
        "action_id": "host-power.reboot-once.test",
        "host": socket.gethostname(),
        "minutes": 10,
        "confirm": m.AUTHORIZE_CONFIRM,
        "config_path": config_path(tmp_path),
        "authorization_ref": "chat-explicit-authorization-test",
    }
    kwargs.update(overrides)
    return m.authorize(**kwargs)


def test_authorization_is_bound_to_current_host_and_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "append_audit", lambda payload: None)
    result = authorize(tmp_path)
    assert result["ok"] is True

    payload = m.load_authorization(
        action_id="host-power.reboot-once.test",
        config_path=config_path(tmp_path),
    )
    assert payload["operation"] == "reboot"
    assert payload["host"].casefold() == socket.gethostname().casefold()
    assert payload["owner_fingerprint"] == m.owner_fingerprint()
    assert payload["consumed_at"] is None


def test_authorize_rejects_other_host(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "append_audit", lambda payload: None)
    with pytest.raises(m.HostPowerError, match="host local"):
        authorize(tmp_path, host="OTHER-HOST")


@pytest.mark.parametrize("minutes", [0, m.MAX_WINDOW_MINUTES + 1])
def test_authorize_rejects_invalid_window(tmp_path, monkeypatch, minutes):
    monkeypatch.setattr(m, "append_audit", lambda payload: None)
    with pytest.raises(m.HostPowerError, match="janela"):
        authorize(tmp_path, minutes=minutes)


def test_load_rejects_expired_authorization(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "append_audit", lambda payload: None)
    authorize(tmp_path, minutes=1)
    path = config_path(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["expires_at"] = m.utc_iso(m.utc_now() - timedelta(seconds=1))
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(m.HostPowerError, match="expirada") as exc:
        m.load_authorization(
            action_id="host-power.reboot-once.test",
            config_path=path,
        )
    assert exc.value.exit_code == m.EXIT_EXPIRED


def test_execute_consumes_before_submitting_reboot(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "append_audit", lambda payload: None)
    authorize(tmp_path)
    observed = {}

    def fake_submit(delay_seconds: int):
        payload = json.loads(config_path(tmp_path).read_text(encoding="utf-8"))
        observed["consumed_before_submit"] = bool(payload["consumed_at"])
        observed["delay"] = delay_seconds
        return subprocess.CompletedProcess(
            ["shutdown.exe"], 0, stdout="scheduled", stderr=""
        )

    monkeypatch.setattr(m, "submit_reboot", fake_submit)
    result = m.execute(
        action_id="host-power.reboot-once.test",
        confirm=m.EXECUTE_CONFIRM,
        config_path=config_path(tmp_path),
        correlation_id="corr-test-reboot",
        delay_seconds=5,
    )
    assert result["ok"] is True
    assert observed == {"consumed_before_submit": True, "delay": 5}

    with pytest.raises(m.HostPowerError, match="consumida"):
        m.execute(
            action_id="host-power.reboot-once.test",
            confirm=m.EXECUTE_CONFIRM,
            config_path=config_path(tmp_path),
            correlation_id="corr-replay",
            delay_seconds=5,
        )


def test_execute_never_accepts_shutdown_or_poweroff_operation(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "append_audit", lambda payload: None)
    authorize(tmp_path)
    path = config_path(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["operation"] = "shutdown"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(m.HostPowerError, match="somente reboot"):
        m.load_authorization(
            action_id="host-power.reboot-once.test",
            config_path=path,
        )


def test_revoke_removes_authorization(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "append_audit", lambda payload: None)
    authorize(tmp_path)
    path = config_path(tmp_path)
    assert path.exists()
    result = m.revoke(confirm=m.REVOKE_CONFIRM, config_path=path)
    assert result == {"ok": True, "revoked": True}
    assert not path.exists()


def test_general_risk3_gateway_still_denies_host_power():
    source = (ROOT / "scripts" / "owner_risk3_gateway.py").read_text(
        encoding="utf-8"
    )
    assert "HOST_POWER_MARKER_RE" in source
    assert "reinicialização/desligamento de host permanece bloqueado" in source
