from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "docker-compose.pc24x7.yml"
BOOTSTRAP = ROOT / "scripts" / "todo_gateway_pc24x7.py"
LIVE_E2E = ROOT / "scripts" / "todo_gateway_pc24x7_e2e.py"
RESILIENCE = ROOT / "scripts" / "todo_gateway_pc24x7_resilience.py"
DOCKERFILE = ROOT / "services" / "todo_gateway" / "Dockerfile.pc24x7"


def test_pc24x7_compose_has_persistent_postgres_and_restart_policy():
    text = COMPOSE.read_text(encoding="utf-8")
    assert "postgres:16-alpine" in text
    assert "todo_global_pgdata:/var/lib/postgresql/data" in text
    assert text.count("restart: unless-stopped") == 2
    assert "127.0.0.1:${TODO_GATEWAY_PORT:-8094}:8000" in text
    assert "condition: service_healthy" in text


def test_pc24x7_gateway_uses_runtime_env_and_does_not_require_notion():
    text = COMPOSE.read_text(encoding="utf-8")
    assert "DATABASE_URL: ${DATABASE_URL}" in text
    assert "TODO_GATEWAY_TOKEN: ${TODO_GATEWAY_TOKEN}" in text
    assert "NOTION_TOKEN" not in text
    assert "NOTION_DATA_SOURCE_ID" not in text


def test_bootstrap_generates_secrets_locally_and_can_start_docker_desktop():
    text = BOOTSTRAP.read_text(encoding="utf-8")
    assert "PORT = 8094" in text
    assert "secrets.token_urlsafe" in text
    assert 'root / "runtime.env"' in text
    assert "ensure_port_setting(path)" in text
    assert "ensure_docker_ready" in text
    assert "Docker Desktop.exe" in text
    assert "TODO_GATEWAY_TOKEN={token}" in text
    assert '"TODO_GATEWAY_TOKEN": token' not in text
    assert '"POSTGRES_PASSWORD": password' not in text
    assert "postgresql://[REDACTED]@" in text


def test_bootstrap_propagates_programdata_to_docker_children():
    text = BOOTSTRAP.read_text(encoding="utf-8")
    assert "def windows_child_env" in text
    assert 'env.get("ProgramData")' in text
    assert 'env.get("ALLUSERSPROFILE")' in text
    assert 'r"C:\\ProgramData"' in text
    assert 'env["ProgramData"] = program_data' in text
    assert text.count("env=windows_child_env()") >= 3


def test_pc24x7_live_e2e_requires_api_replay_continuation_and_independent_sql():
    text = LIVE_E2E.read_text(encoding="utf-8")
    assert '"POST", "/v1/events"' in text
    assert 'f"/v1/todos/{key}/continue"' in text
    assert '"/v1/continuations?limit=200"' in text
    assert "docker\", \"exec" in text
    assert "sql_event_readback" in text
    assert "sql_continuation_readback" in text
    assert "event_replay_idempotent" in text
    assert "continuation_replay_idempotent" in text


def test_pc24x7_resilience_covers_restart_backup_and_restore():
    text = RESILIENCE.read_text(encoding="utf-8")
    assert '"restart", "db"' in text
    assert '"restart", "gateway"' in text
    assert '"pg_dump"' in text
    assert '"pg_restore"' in text
    assert '"createdb"' in text
    assert '"dropdb"' in text
    assert "restore_verified" in text


def test_pc24x7_image_starts_supervised_gateway_with_required_packages():
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert 'CMD ["python", "-m", "services.todo_gateway.service_main"]' in text
    assert "COPY services /app/services" in text
    assert "COPY scripts /app/scripts" in text
    assert "COPY sql /app/sql" in text
