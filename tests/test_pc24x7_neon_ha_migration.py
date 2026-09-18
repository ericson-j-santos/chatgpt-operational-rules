import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "scripts" / "migrate_todo_bus_to_neon_dev.py"
DESKTOP = ROOT / "scripts" / "cutover_todo_bus_neon_desktop_dev.py"
NOTERI = ROOT / "scripts" / "cutover_todo_bus_neon_noteri_dev.py"


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
