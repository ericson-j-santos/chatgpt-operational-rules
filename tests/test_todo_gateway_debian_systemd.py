import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install_todo_gateway_debian_systemd.py"
E2E = ROOT / "scripts" / "todo_gateway_debian_e2e.py"
RESILIENCE = ROOT / "scripts" / "todo_gateway_debian_resilience.py"


def _text(path: Path) -> str:
    value = path.read_text(encoding="utf-8")
    ast.parse(value)
    return value


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
