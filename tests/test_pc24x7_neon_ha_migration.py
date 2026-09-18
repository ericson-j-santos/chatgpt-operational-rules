import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "scripts" / "migrate_todo_bus_to_neon_dev.py"
DESKTOP = ROOT / "scripts" / "cutover_todo_bus_neon_desktop_dev.py"
NOTERI = ROOT / "scripts" / "cutover_todo_bus_neon_noteri_dev.py"
NOTERI_DEPLOY = ROOT / "scripts" / "deploy_pc24x7_noteri_ha_worker_dev.py"
INDEPENDENCE_E2E = ROOT / "scripts" / "pc24x7_neon_independence_e2e_dev.py"
PHYSICAL_CYCLE_NOTERI = ROOT / "scripts" / "pc24x7_physical_cycle_noteri_e2e_dev.py"
PHYSICAL_CYCLE_DESKTOP = ROOT / "scripts" / "pc24x7_physical_cycle_desktop_dev.py"


def read(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    ast.parse(text)
    return text


def test_migration_is_pinned_to_expected_neon_database_and_preserves_history():
    text = read(MIGRATION)
    assert 'EXPECTED_TARGET_DB = "todo_global_bus_dev_ha"' in text
    assert 'endswith(".neon.tech")' in text
    assert "target is not empty" in text
    assert "DISABLE TRIGGER trg_continuation_audit" in text
    assert "ENABLE TRIGGER trg_continuation_audit" in text
    assert '"history_preserved"' in text
    assert "target_after != source" in text


def test_desktop_cutover_has_local_config_rollback_and_keeps_local_db():
    text = read(DESKTOP)
    assert 'BACKUP_ENV = BASE / "runtime.env.pre-neon-ha"' in text
    assert "shutil.copy2(BACKUP_ENV, RUNTIME_ENV)" in text
    assert '"gateway", "worker"' in text
    assert '"local_db_preserved_for_rollback": True' in text
    assert "print(dsn)" not in text


def test_noteri_cutover_has_config_rollback_and_removes_bridge_dependency():
    text = read(NOTERI)
    assert 'BACKUP_ENV = BASE / "runtime.env.pre-neon-ha"' in text
    assert "shutil.copy2(BACKUP_ENV, RUNTIME_ENV)" in text
    assert "stop_worker()" in text
    assert '"local_bridge_no_longer_required": True' in text
    assert "print(dsn)" not in text


def test_noteri_deploy_accepts_default_postgres_port():
    text = read(NOTERI_DEPLOY)
    assert "port = parsed.port or 5432" in text
    assert "socket.create_connection((parsed.hostname, port)" in text


def test_neon_independence_e2e_removes_local_db_dependency_and_requires_noteri():
    text = read(INDEPENDENCE_E2E)
    assert 'LOCAL_DB = "todo-global-24x7-db-1"' in text
    assert 'LOCAL_BRIDGE = "todo-global-24x7-db_bridge-1"' in text
    assert 'for name in (WORKER, LOCAL_BRIDGE, LOCAL_DB)' in text
    assert 'if proof["attempts"] != 1' in text
    assert 'if proof["node_id"] != "Noteri"' in text
    assert 'duplicate completion detected' in text
    assert 'run(["docker", "start", WORKER])' in text
    assert '"desktop_local_db_stopped": True' in text
    assert '"local_database_preserved_for_rollback": True' in text


def test_physical_cycle_noteri_proves_failover_failback_and_single_execution():
    text = read(PHYSICAL_CYCLE_NOTERI)
    assert 'DESKTOP = "DESKTOP-PDQK954"' in text
    assert 'NOTERI = "Noteri"' in text
    assert 'assert_execution(failover, NOTERI)' in text
    assert 'assert_execution(failback, DESKTOP)' in text
    assert 'if proof["attempts"] != 1' in text
    assert 'route idempotency failed' in text
    assert '"single_execution": True' in text
    assert '"waiting_desktop_return"' in text


def test_physical_cycle_desktop_is_host_pinned_restart_only():
    text = read(PHYSICAL_CYCLE_DESKTOP)
    assert 'TARGET = "DESKTOP-PDQK954"' in text
    assert '"shutdown.exe"' in text
    assert '"/r"' in text
    assert '"/s"' not in text
    assert 'refusing physical cycle on unexpected host' in text
    assert 'delay_seconds = 10' in text
