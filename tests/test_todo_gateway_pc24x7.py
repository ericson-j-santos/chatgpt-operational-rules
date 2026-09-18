import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "docker-compose.pc24x7.yml"
BOOTSTRAP = ROOT / "scripts" / "todo_gateway_pc24x7.py"
LIVE_E2E = ROOT / "scripts" / "todo_gateway_pc24x7_e2e.py"
RESILIENCE = ROOT / "scripts" / "todo_gateway_pc24x7_resilience.py"
DOCKERFILE = ROOT / "services" / "todo_gateway" / "Dockerfile.pc24x7"
RUNTIME_RULE = ROOT / "rules" / "runtime-routing.md"
RENDER_WORKFLOW = ROOT / ".github" / "workflows" / "render-todo-gateway-bootstrap.yml"


class Pc24x7RuntimeTests(unittest.TestCase):
    def test_compose_has_persistent_postgres_and_restart_policy(self):
        text = COMPOSE.read_text(encoding="utf-8")
        self.assertIn("postgres:16-alpine", text)
        self.assertIn("todo_global_pgdata:/var/lib/postgresql/data", text)
        self.assertEqual(text.count("restart: unless-stopped"), 5)
        self.assertIn("127.0.0.1:${TODO_GATEWAY_PORT:-8094}:8000", text)
        self.assertIn("condition: service_healthy", text)


    def test_scheduler_bridge_is_supervised_and_uses_internal_gateway(self):
        text = COMPOSE.read_text(encoding="utf-8")
        self.assertIn("scheduler:", text)
        self.assertIn("scripts.github_schedule_bridge", text)
        self.assertIn("TODO_GATEWAY_URL: http://gateway:8000", text)
        self.assertIn("GITHUB_SCHEDULE_WORKFLOW: todo-global-hourly-cycle.yml", text)
        self.assertIn("todo_scheduler_state:/var/lib/todo-scheduler", text)
        self.assertNotIn("GITHUB_TOKEN:", text)

    def test_gateway_uses_runtime_env_and_does_not_require_notion(self):
        text = COMPOSE.read_text(encoding="utf-8")
        self.assertIn("DATABASE_URL: ${DATABASE_URL}", text)
        self.assertIn("TODO_GATEWAY_TOKEN: ${TODO_GATEWAY_TOKEN}", text)
        self.assertNotIn("NOTION_TOKEN", text)
        self.assertNotIn("NOTION_DATA_SOURCE_ID", text)

    def test_bootstrap_generates_secrets_locally_and_can_start_docker_desktop(self):
        text = BOOTSTRAP.read_text(encoding="utf-8")
        self.assertIn("PORT = 8094", text)
        self.assertIn("secrets.token_urlsafe", text)
        self.assertIn('root / "runtime.env"', text)
        self.assertIn("ensure_port_setting(path)", text)
        self.assertIn("ensure_docker_ready", text)
        self.assertIn("Docker Desktop.exe", text)
        self.assertIn("TODO_GATEWAY_TOKEN={token}", text)
        self.assertNotIn('"TODO_GATEWAY_TOKEN": token', text)
        self.assertNotIn('"POSTGRES_PASSWORD": password', text)
        self.assertIn("postgresql://[REDACTED]@", text)

    def test_bootstrap_propagates_programdata_to_docker_children(self):
        text = BOOTSTRAP.read_text(encoding="utf-8")
        self.assertIn("def windows_child_env", text)
        self.assertIn('env.get("ProgramData")', text)
        self.assertIn('env.get("ALLUSERSPROFILE")', text)
        self.assertIn('r"C:\\ProgramData"', text)
        self.assertIn('env["ProgramData"] = program_data', text)
        self.assertGreaterEqual(text.count("env=windows_child_env()"), 3)

    def test_live_e2e_requires_replay_continuation_and_independent_sql(self):
        text = LIVE_E2E.read_text(encoding="utf-8")
        self.assertIn('"POST", "/v1/events"', text)
        self.assertIn('f"/v1/todos/{key}/continue"', text)
        self.assertIn('"/v1/continuations?limit=200"', text)
        self.assertIn("sql_event_readback", text)
        self.assertIn("sql_continuation_readback", text)
        self.assertIn("event_replay_idempotent", text)
        self.assertIn("continuation_replay_idempotent", text)

    def test_resilience_covers_restart_backup_and_restore(self):
        text = RESILIENCE.read_text(encoding="utf-8")
        self.assertIn('"restart", "db"', text)
        self.assertIn('"restart", "gateway"', text)
        self.assertIn('"pg_dump"', text)
        self.assertIn('"pg_restore"', text)
        self.assertIn('"createdb"', text)
        self.assertIn('"dropdb"', text)
        self.assertIn("restore_verified", text)

    def test_image_starts_supervised_gateway_with_required_packages(self):
        text = DOCKERFILE.read_text(encoding="utf-8")
        self.assertIn('CMD ["python", "-m", "services.todo_gateway.service_main"]', text)
        self.assertIn("COPY services /app/services", text)
        self.assertIn("COPY scripts /app/scripts", text)
        self.assertIn("COPY sql /app/sql", text)

    def test_runtime_rule_declares_pc24x7_canonical_and_provider_boundaries(self):
        text = RUNTIME_RULE.read_text(encoding="utf-8")
        self.assertIn("PC24x7 Desktop` é o runtime primário e canônico", text)
        self.assertIn("Render é legado/contingência", text)
        self.assertIn("Fly.io não é runtime do TODO Global", text)
        self.assertIn("runtime_target=pc24x7", text)

    def test_render_fallback_is_manual_only(self):
        text = RENDER_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", text)
        self.assertNotIn("\n  push:", text)
        self.assertNotIn("\n  schedule:", text)
        self.assertIn("Bootstrap legacy Render contingency", text)


if __name__ == "__main__":
    unittest.main()
