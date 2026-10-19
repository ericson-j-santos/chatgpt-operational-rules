import ast
import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
import threading
import time
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


def _release_fixture(module, tmp_path, monkeypatch, revision="a" * 12):
    base = tmp_path / "opt" / "todo-global"
    repo = tmp_path / "repo"
    (repo / "services" / "todo_gateway").mkdir(parents=True)
    (repo / "scripts").mkdir()
    (repo / "sql").mkdir()
    (repo / "services" / "todo_gateway" / "requirements.txt").write_text(
        "# fixture\n", encoding="utf-8"
    )
    (repo / "scripts" / "worker.py").write_text("pass\n", encoding="utf-8")
    (repo / "sql" / "schema.sql").write_text("SELECT 1;\n", encoding="utf-8")
    revision_ref = {"value": revision}
    calls = []

    def successful_run(args, **kwargs):
        calls.append((list(args), dict(kwargs)))
        if list(args[:3]) == ["python3", "-m", "venv"]:
            python = Path(args[3]) / "bin" / "python"
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_text("", encoding="utf-8")
            python.chmod(0o755)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(module, "BASE", base)
    monkeypatch.setattr(module, "git_revision", lambda _repo: revision_ref["value"])
    monkeypatch.setattr(
        module,
        "_app_identity",
        lambda: (os.geteuid(), os.getegid()),
        raising=False,
    )
    monkeypatch.setattr(module.os, "chown", lambda *args, **kwargs: None)
    monkeypatch.setattr(module.shutil, "chown", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "run", successful_run)
    return base, repo, revision_ref, calls, successful_run


def _current_target(base: Path) -> Path:
    current = base / "current"
    raw = Path(os.readlink(current))
    return (raw if raw.is_absolute() else current.parent / raw).absolute()


def _tree_snapshot(root: Path) -> tuple:
    snapshot = []
    for path in [root, *sorted(root.rglob("*"))]:
        metadata = path.lstat()
        relative = "." if path == root else path.relative_to(root).as_posix()
        if stat.S_ISLNK(metadata.st_mode):
            payload = ("symlink", os.readlink(path))
        elif stat.S_ISREG(metadata.st_mode):
            payload = ("file", hashlib.sha256(path.read_bytes()).hexdigest())
        else:
            payload = ("directory", None)
        snapshot.append(
            (
                relative,
                metadata.st_ino,
                stat.S_IMODE(metadata.st_mode),
                metadata.st_uid,
                metadata.st_gid,
                metadata.st_mtime_ns,
                payload,
            )
        )
    return tuple(snapshot)


def _write_ready_release_marker(release: Path, revision: str) -> None:
    marker = release / ".todo-release.json"
    marker.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "owner": "todo-global-debian-systemd",
                "release": revision,
                "state": "ready",
                "nonce": "b" * 32,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    marker.chmod(0o600)


def test_installer_uses_native_systemd_and_local_postgres():
    text = _text(INSTALLER)
    assert 'APP_USER = "todo-global"' in text
    assert 'DB_NAME = "todo_global_bus_dev"' in text
    assert 'EnvironmentFile=/etc/todo-global/runtime.env' in text
    assert 'WantedBy=multi-user.target' in text
    assert '["systemctl", "enable", "todo-global-gateway.service"]' in text
    assert '["systemctl", "restart", "todo-global-gateway.service"]' in text
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


def test_postgres_sql_uses_stdin_with_on_error_stop_and_timeout(monkeypatch):
    module = _module()
    captured = {}
    secret = "todo22-password-canary"
    sql = f"ALTER ROLE role_name WITH LOGIN PASSWORD '{secret}'"

    def fake_run(args, **kwargs):
        captured["args"] = list(args)
        captured["kwargs"] = dict(kwargs)
        return subprocess.CompletedProcess(args, 0, stdout="1\n", stderr="")

    monkeypatch.setattr(module, "run", fake_run)
    assert module.postgres_scalar(sql) == "1"
    argv = "\n".join(captured["args"])
    assert secret not in argv
    assert "ALTER ROLE" not in argv
    assert captured["kwargs"]["input_text"] == sql + "\n"
    assert 0 < captured["kwargs"]["timeout"] <= 30
    assert "--no-psqlrc" in captured["args"]
    assert "--no-password" in captured["args"]
    assert "--set=ON_ERROR_STOP=1" in captured["args"]
    assert "-c" not in captured["args"]
    assert "--command" not in captured["args"]
    assert not any(
        arg.startswith("-") and not arg.startswith("--") and "c" in arg[1:]
        for arg in captured["args"]
    )


def test_postgres_failure_is_sanitized(monkeypatch, capsys):
    module = _module()
    secret = "todo22-sql-error-canary"
    sql = f"ALTER ROLE role_name WITH LOGIN PASSWORD '{secret}'"
    captured = {}

    class FailedProcess:
        returncode = 3

        def communicate(self, input=None, timeout=None):
            captured["input"] = input
            captured["timeout"] = timeout
            return f"stdout {secret}", f"stderr {sql}"

    def fake_popen(args, **kwargs):
        captured["args"] = list(args)
        captured["kwargs"] = dict(kwargs)
        return FailedProcess()

    def fake_subprocess_run(args, **kwargs):
        captured["args"] = list(args)
        return subprocess.CompletedProcess(
            args, 3, stdout=f"stdout {secret}", stderr=f"stderr {sql}"
        )

    monkeypatch.setattr(module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(module.subprocess, "run", fake_subprocess_run)
    with pytest.raises(RuntimeError) as exc:
        module.postgres_scalar(sql)
    captured_output = capsys.readouterr()
    observed = str(exc.value) + captured_output.out + captured_output.err
    assert secret not in observed
    assert sql not in observed
    assert secret not in "\n".join(captured["args"])


def test_postgres_timeout_is_bounded_and_sanitized(monkeypatch, capsys):
    module = _module()
    secret = "todo22-timeout-canary"
    sql = f"ALTER ROLE role_name WITH LOGIN PASSWORD '{secret}'"
    captured = {}

    class TimedOutProcess:
        pid = 424242
        returncode = -9
        calls = 0

        def communicate(self, input=None, timeout=None):
            self.calls += 1
            captured["timeout"] = timeout
            if self.calls == 1:
                raise subprocess.TimeoutExpired(
                    captured["args"], timeout, output=f"stdout {secret}", stderr=f"stderr {sql}"
                )
            return f"stdout {secret}", f"stderr {sql}"

    def fake_popen(args, **kwargs):
        captured["args"] = list(args)
        return TimedOutProcess()

    def fake_subprocess_run(args, **kwargs):
        captured["args"] = list(args)
        captured["timeout"] = kwargs.get("timeout")
        raise subprocess.TimeoutExpired(
            args, kwargs.get("timeout"), output=f"stdout {secret}", stderr=f"stderr {sql}"
        )

    monkeypatch.setattr(module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(module.subprocess, "run", fake_subprocess_run)
    monkeypatch.setattr(module.os, "killpg", lambda *args: None)
    with pytest.raises(RuntimeError) as exc:
        module.postgres_scalar(sql)
    captured_output = capsys.readouterr()
    observed = str(exc.value) + captured_output.out + captured_output.err
    assert 0 < captured["timeout"] <= 30
    assert secret not in observed
    assert sql not in observed
    assert secret not in "\n".join(captured["args"])


def test_timeout_terminates_descendant_process_group(tmp_path, monkeypatch):
    module = _module()
    pid_file = tmp_path / "descendant.pid"
    child = (
        "import signal,time;"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
        "time.sleep(60)"
    )
    parent = (
        "import pathlib,subprocess,sys,time;"
        f"p=subprocess.Popen([sys.executable,'-c',{child!r}]);"
        "pathlib.Path(sys.argv[1]).write_text(str(p.pid));"
        "time.sleep(60)"
    )
    monkeypatch.setattr(module, "PROCESS_TERMINATE_GRACE_SECONDS", 0.1)

    with pytest.raises(RuntimeError, match="timed out"):
        module.run([sys.executable, "-c", parent, str(pid_file)], timeout=0.5)

    descendant_pid = int(pid_file.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        status = Path(f"/proc/{descendant_pid}/stat")
        if not status.exists() or status.read_text(encoding="utf-8").split()[2] == "Z":
            break
        time.sleep(0.02)
    else:
        pytest.fail("descendant process survived timeout")


def test_postgres_error_stops_followup_sql_and_createdb(monkeypatch):
    module = _module()
    sql_calls = []
    command_calls = []

    def failing_scalar(sql):
        sql_calls.append(sql)
        if len(sql_calls) == 1:
            return "0"
        raise RuntimeError("command failed rc=3: runuser")

    monkeypatch.setattr(module, "postgres_scalar", failing_scalar)

    def record_command(args, **kwargs):
        command_calls.append(list(args))
        if "createdb" in args:
            pytest.fail("createdb must not run after SQL failure")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(module, "run", record_command)
    with pytest.raises(RuntimeError, match="command failed"):
        module.ensure_postgres("local-placeholder")
    assert len(sql_calls) == 2
    assert command_calls == [["systemctl", "enable", "--now", "postgresql"]]


def test_active_release_replay_is_exact_noop(tmp_path, monkeypatch):
    module = _module()
    base, repo, _, calls, _ = _release_fixture(module, tmp_path, monkeypatch)
    release = module.install_release(repo)
    sentinel = release / "operator-sentinel.txt"
    sentinel.write_text("preserve-me", encoding="utf-8")
    release_inode = release.stat().st_ino
    current_inode = (base / "current").lstat().st_ino
    current_raw = os.readlink(base / "current")
    release_snapshot = _tree_snapshot(release)
    call_count = len(calls)

    assert module.install_release(repo) == release
    assert release.stat().st_ino == release_inode
    assert sentinel.read_text(encoding="utf-8") == "preserve-me"
    assert _tree_snapshot(release) == release_snapshot
    assert (base / "current").lstat().st_ino == current_inode
    assert os.readlink(base / "current") == current_raw
    assert len(calls) == call_count


def test_main_replay_preserves_release_current_and_sentinel(
    tmp_path, monkeypatch
):
    module = _module()
    base, repo, _, calls, _ = _release_fixture(module, tmp_path, monkeypatch)
    release = module.install_release(repo)
    sentinel = release / "operator-sentinel.txt"
    sentinel.write_text("preserve-me", encoding="utf-8")
    before_tree = _tree_snapshot(release)
    before_current = (base / "current").lstat()

    monkeypatch.setattr(module, "require_root", lambda: None)
    monkeypatch.setattr(module, "ensure_service_user", lambda: None)
    monkeypatch.setattr(module, "ensure_runtime_env", lambda: ("unused", "unused"))
    monkeypatch.setattr(module, "ensure_postgres", lambda _password: None)
    monkeypatch.setattr(module, "install_unit", lambda: None)
    monkeypatch.setattr(module, "smoke", lambda: True)
    monkeypatch.setattr(
        sys,
        "argv",
        ["installer", "--repo-root", str(repo), "--skip-packages"],
    )

    assert module.main() == 0
    after_current = (base / "current").lstat()
    assert _current_target(base) == release.absolute()
    assert before_current.st_dev == after_current.st_dev
    assert before_current.st_ino == after_current.st_ino
    assert _tree_snapshot(release) == before_tree
    assert sentinel.read_text(encoding="utf-8") == "preserve-me"
    assert [args for args, _kwargs in calls if args[:2] == ["systemctl", "restart"]] == [
        ["systemctl", "restart", "todo-global-gateway.service"]
    ]


def test_markerless_legacy_active_release_replay_is_noop(tmp_path, monkeypatch):
    module = _module()
    base, repo, _, calls, _ = _release_fixture(module, tmp_path, monkeypatch)
    release = module.install_release(repo)
    marker = release / ".todo-release.json"
    if marker.exists():
        marker.unlink()
    sentinel = release / "legacy-operator-sentinel.txt"
    sentinel.write_text("preserve-legacy", encoding="utf-8")
    release_snapshot = _tree_snapshot(release)
    current_snapshot = ((base / "current").lstat().st_ino, os.readlink(base / "current"))
    call_count = len(calls)

    assert module.install_release(repo) == release
    assert _tree_snapshot(release) == release_snapshot
    assert ((base / "current").lstat().st_ino, os.readlink(base / "current")) == current_snapshot
    assert len(calls) == call_count


def test_failed_new_release_preserves_previous_and_has_no_partial_final(tmp_path, monkeypatch):
    module = _module()
    base, repo, revision_ref, _, successful_run = _release_fixture(
        module, tmp_path, monkeypatch
    )
    previous = module.install_release(repo)
    sentinel = previous / "operator-sentinel.txt"
    sentinel.write_text("preserve-me", encoding="utf-8")
    revision_ref["value"] = "c" * 12

    def fail_during_pip(args, **kwargs):
        result = successful_run(args, **kwargs)
        if "pip" in args:
            assert _current_target(base) == previous.absolute()
            assert sentinel.read_text(encoding="utf-8") == "preserve-me"
            raise RuntimeError("injected preparation failure")
        return result

    monkeypatch.setattr(module, "run", fail_during_pip)
    with pytest.raises(RuntimeError, match="injected preparation failure"):
        module.install_release(repo)
    assert _current_target(base) == previous.absolute()
    assert sentinel.read_text(encoding="utf-8") == "preserve-me"
    assert not (base / "releases" / revision_ref["value"]).exists()
    assert not any((base / "releases" / ".staging").iterdir())

    monkeypatch.setattr(module, "run", successful_run)
    recovered = module.install_release(repo)
    assert recovered == base / "releases" / revision_ref["value"]
    assert _current_target(base) == recovered.absolute()
    assert sentinel.read_text(encoding="utf-8") == "preserve-me"
    module._validate_ready_release(recovered, revision_ref["value"])


def test_retry_recovers_only_owned_abandoned_staging(tmp_path, monkeypatch):
    module = _module()
    base, repo, revision_ref, _, _ = _release_fixture(module, tmp_path, monkeypatch)
    previous = module.install_release(repo)
    revision_ref["value"] = "7" * 12
    nonce = "8" * 32
    abandoned = base / "releases" / ".staging" / f"{revision_ref['value']}-{nonce}"
    abandoned.mkdir()
    module._write_release_marker(abandoned, revision_ref["value"], "staging", nonce)
    (abandoned / "partial.txt").write_text("partial", encoding="utf-8")

    recovered = module.install_release(repo)
    assert recovered == base / "releases" / revision_ref["value"]
    assert _current_target(base) == recovered.absolute()
    assert previous.exists()
    assert not abandoned.exists()


def test_foreign_abandoned_staging_is_preserved_and_blocks_replay(tmp_path, monkeypatch):
    module = _module()
    base, repo, _, _, _ = _release_fixture(module, tmp_path, monkeypatch)
    previous = module.install_release(repo)
    sentinel = previous / "operator-sentinel.txt"
    sentinel.write_text("preserve-me", encoding="utf-8")
    foreign = base / "releases" / ".staging" / "foreign"
    foreign.mkdir()
    foreign_sentinel = foreign / "foreign-sentinel.txt"
    foreign_sentinel.write_text("foreign", encoding="utf-8")

    with pytest.raises(RuntimeError, match="staging ownership"):
        module.install_release(repo)
    assert _current_target(base) == previous.absolute()
    assert sentinel.read_text(encoding="utf-8") == "preserve-me"
    assert foreign_sentinel.read_text(encoding="utf-8") == "foreign"


def test_unowned_existing_release_collision_fails_closed(tmp_path, monkeypatch):
    module = _module()
    base, repo, revision_ref, _, _ = _release_fixture(module, tmp_path, monkeypatch)
    previous = module.install_release(repo)
    revision_ref["value"] = "d" * 12
    collision = base / "releases" / revision_ref["value"]
    collision.mkdir()
    sentinel = collision / "foreign-sentinel.txt"
    sentinel.write_text("foreign", encoding="utf-8")

    with pytest.raises(RuntimeError, match="verification"):
        module.install_release(repo)
    assert _current_target(base) == previous.absolute()
    assert sentinel.read_text(encoding="utf-8") == "foreign"


def test_completed_release_is_recovered_without_recopy(tmp_path, monkeypatch):
    module = _module()
    base, repo, revision_ref, _, _ = _release_fixture(module, tmp_path, monkeypatch)
    previous = module.install_release(repo)
    revision_ref["value"] = "e" * 12
    recovered = base / "releases" / revision_ref["value"]
    module.shutil.copytree(previous, recovered)
    sentinel = recovered / "completed-sentinel.txt"
    sentinel.write_text("completed", encoding="utf-8")
    _write_ready_release_marker(recovered, revision_ref["value"])

    def must_not_recopy(*args, **kwargs):
        raise AssertionError("completed release must be reused")

    monkeypatch.setattr(module, "run", must_not_recopy)
    assert module.install_release(repo) == recovered
    assert _current_target(base) == recovered.absolute()
    assert sentinel.read_text(encoding="utf-8") == "completed"


def test_marker_alone_is_not_a_recoverable_release(tmp_path, monkeypatch):
    module = _module()
    base, repo, revision_ref, _, _ = _release_fixture(module, tmp_path, monkeypatch)
    previous = module.install_release(repo)
    revision_ref["value"] = "4" * 12
    incomplete = base / "releases" / revision_ref["value"]
    incomplete.mkdir()
    sentinel = incomplete / "foreign-sentinel.txt"
    sentinel.write_text("foreign", encoding="utf-8")
    _write_ready_release_marker(incomplete, revision_ref["value"])

    with pytest.raises(RuntimeError, match="completeness"):
        module.install_release(repo)
    assert _current_target(base) == previous.absolute()
    assert sentinel.read_text(encoding="utf-8") == "foreign"


def test_writable_release_marker_is_rejected_without_current_change(tmp_path, monkeypatch):
    module = _module()
    base, repo, _, _, _ = _release_fixture(module, tmp_path, monkeypatch)
    release = module.install_release(repo)
    marker = release / module.RELEASE_MARKER_NAME
    marker.chmod(0o666)
    current_snapshot = ((base / "current").lstat().st_ino, os.readlink(base / "current"))

    with pytest.raises(RuntimeError, match="ownership"):
        module.install_release(repo)
    assert ((base / "current").lstat().st_ino, os.readlink(base / "current")) == current_snapshot


def test_concurrent_current_change_fails_closed(tmp_path, monkeypatch):
    module = _module()
    base, repo, revision_ref, _, successful_run = _release_fixture(
        module, tmp_path, monkeypatch
    )
    previous = module.install_release(repo)
    previous_sentinel = previous / "operator-sentinel.txt"
    previous_sentinel.write_text("preserve-me", encoding="utf-8")
    concurrent_revision = "f" * 12
    concurrent = base / "releases" / concurrent_revision
    concurrent.mkdir()
    _write_ready_release_marker(concurrent, concurrent_revision)
    revision_ref["value"] = "1" * 12
    changed = False

    def change_current_during_prepare(args, **kwargs):
        nonlocal changed
        result = successful_run(args, **kwargs)
        if not changed and list(args[:3]) == ["python3", "-m", "venv"]:
            replacement = base / ".external-current"
            replacement.symlink_to(concurrent, target_is_directory=True)
            replacement.replace(base / "current")
            changed = True
        return result

    monkeypatch.setattr(module, "run", change_current_during_prepare)
    with pytest.raises(RuntimeError, match="concurrent"):
        module.install_release(repo)
    assert _current_target(base) == concurrent.absolute()
    assert previous_sentinel.read_text(encoding="utf-8") == "preserve-me"
    assert not (base / "releases" / revision_ref["value"]).exists()


def test_concurrent_release_collision_is_no_clobber(tmp_path, monkeypatch):
    module = _module()
    base, repo, revision_ref, _, successful_run = _release_fixture(
        module, tmp_path, monkeypatch
    )
    previous = module.install_release(repo)
    revision_ref["value"] = "5" * 12
    collision = base / "releases" / revision_ref["value"]
    injected = False

    def create_collision_during_prepare(args, **kwargs):
        nonlocal injected
        result = successful_run(args, **kwargs)
        if not injected and "pip" in args:
            collision.mkdir()
            (collision / "foreign-sentinel.txt").write_text("foreign", encoding="utf-8")
            injected = True
        return result

    monkeypatch.setattr(module, "run", create_collision_during_prepare)
    with pytest.raises(RuntimeError, match="concurrent release publication"):
        module.install_release(repo)
    assert _current_target(base) == previous.absolute()
    assert (collision / "foreign-sentinel.txt").read_text(encoding="utf-8") == "foreign"
    assert not any((base / "releases" / ".staging").iterdir())


def test_post_replace_failure_rolls_back_current(tmp_path, monkeypatch):
    module = _module()
    base, repo, revision_ref, _, _ = _release_fixture(module, tmp_path, monkeypatch)
    previous = module.install_release(repo)
    sentinel = previous / "operator-sentinel.txt"
    sentinel.write_text("preserve-me", encoding="utf-8")
    revision_ref["value"] = "6" * 12
    candidate = base / "releases" / revision_ref["value"]
    durable_sync = module._fsync_directory
    base_syncs = 0
    observed_at_failure = []

    def fail_post_replace_base_sync(path):
        nonlocal base_syncs
        if path == base:
            base_syncs += 1
            if base_syncs == 2:
                observed_at_failure.append(_current_target(base))
                raise OSError("injected durable activation failure")
        durable_sync(path)

    monkeypatch.setattr(module, "_fsync_directory", fail_post_replace_base_sync)
    with pytest.raises(OSError, match="injected durable activation failure"):
        module.install_release(repo)
    assert observed_at_failure == [candidate.absolute()]
    assert base_syncs == 4
    assert _current_target(base) == previous.absolute()
    assert sentinel.read_text(encoding="utf-8") == "preserve-me"
    assert not (base / module.ACTIVATION_JOURNAL_NAME).exists()


def test_pending_activation_journal_recovers_previous_after_crash(tmp_path, monkeypatch):
    module = _module()
    base, repo, _, _, _ = _release_fixture(module, tmp_path, monkeypatch)
    previous = module.install_release(repo)
    sentinel = previous / "operator-sentinel.txt"
    sentinel.write_text("preserve-me", encoding="utf-8")
    previous_snapshot = module._current_snapshot()
    candidate_revision = "9" * 12
    candidate = base / "releases" / candidate_revision
    module.shutil.copytree(previous, candidate)
    _write_ready_release_marker(candidate, candidate_revision)
    nonce = "a" * 32
    module._write_activation_journal(candidate, previous_snapshot, nonce)
    module._replace_current(candidate.as_posix(), previous_snapshot)
    assert _current_target(base) == candidate.absolute()

    assert module.install_release(repo) == previous
    assert _current_target(base) == previous.absolute()
    assert sentinel.read_text(encoding="utf-8") == "preserve-me"
    assert not (base / module.ACTIVATION_JOURNAL_NAME).exists()


def test_main_recovers_missing_crash_candidate_before_failing_prerequisite(
    tmp_path, monkeypatch
):
    module = _module()
    base, repo, _, calls, _ = _release_fixture(module, tmp_path, monkeypatch)
    previous = module.install_release(repo)
    sentinel = previous / "operator-sentinel.txt"
    sentinel.write_text("preserve-me", encoding="utf-8")
    previous_snapshot = module._current_snapshot()
    candidate_revision = "8" * 12
    candidate = base / "releases" / candidate_revision
    module.shutil.copytree(previous, candidate)
    _write_ready_release_marker(candidate, candidate_revision)
    nonce = "b" * 32
    module._write_activation_journal(candidate, previous_snapshot, nonce)
    module._replace_current(candidate.as_posix(), previous_snapshot)
    module.shutil.rmtree(candidate)
    assert (base / "current").is_symlink()
    assert not (base / "current").exists()

    monkeypatch.setattr(module, "require_root", lambda: None)

    def fail_packages_after_recovery():
        assert _current_target(base) == previous.absolute()
        assert sentinel.read_text(encoding="utf-8") == "preserve-me"
        assert not (base / module.ACTIVATION_JOURNAL_NAME).exists()
        raise RuntimeError("injected package failure")

    monkeypatch.setattr(module, "install_packages", fail_packages_after_recovery)
    monkeypatch.setattr(
        sys,
        "argv",
        ["installer", "--repo-root", str(repo)],
    )

    with pytest.raises(RuntimeError, match="injected package failure"):
        module.main()
    assert _current_target(base) == previous.absolute()
    assert sentinel.read_text(encoding="utf-8") == "preserve-me"
    assert [args for args, _kwargs in calls if args[:2] == ["systemctl", "restart"]] == [
        ["systemctl", "restart", "todo-global-gateway.service"]
    ]


def test_concurrent_installer_lock_is_bounded_and_preserves_current(tmp_path, monkeypatch):
    module = _module()
    base, repo, revision_ref, _, _ = _release_fixture(module, tmp_path, monkeypatch)
    previous = module.install_release(repo)
    sentinel = previous / "operator-sentinel.txt"
    sentinel.write_text("preserve-me", encoding="utf-8")
    revision_ref["value"] = "3" * 12
    monkeypatch.setattr(module, "INSTALL_LOCK_TIMEOUT_SECONDS", 0.1, raising=False)
    lock_path = base / ".install.lock"
    lock_path.touch(exist_ok=True, mode=0o600)
    lock_path.chmod(0o600)
    ready = tmp_path / "lock-holder.ready"
    holder_script = (
        "import fcntl,os,pathlib,sys,time;"
        "fd=os.open(sys.argv[1],os.O_RDWR);"
        "fcntl.flock(fd,fcntl.LOCK_EX);"
        "pathlib.Path(sys.argv[2]).write_text('ready');"
        "time.sleep(60)"
    )
    holder = subprocess.Popen([sys.executable, "-c", holder_script, str(lock_path), str(ready)])
    try:
        deadline = time.monotonic() + 2
        while not ready.exists() and holder.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()
        with pytest.raises(RuntimeError, match="concurrent release installation"):
            module.install_release(repo)
    finally:
        holder.terminate()
        holder.wait(timeout=5)
    assert _current_target(base) == previous.absolute()
    assert sentinel.read_text(encoding="utf-8") == "preserve-me"
    assert not (base / "releases" / revision_ref["value"]).exists()


def test_main_serializes_prerequisites_under_the_install_lock(tmp_path, monkeypatch):
    module = _module()
    base, repo, _, _, _ = _release_fixture(module, tmp_path, monkeypatch)
    previous = module.install_release(repo)
    sentinel = previous / "operator-sentinel.txt"
    sentinel.write_text("preserve-me", encoding="utf-8")
    monkeypatch.setattr(module, "INSTALL_LOCK_TIMEOUT_SECONDS", 0.15)
    monkeypatch.setattr(module, "INSTALL_LOCK_POLL_SECONDS", 0.01)
    monkeypatch.setattr(module, "require_root", lambda: None)
    package_entered = threading.Event()
    release_packages = threading.Event()
    mutators = []

    def install_packages():
        mutators.append("packages")
        if len([name for name in mutators if name == "packages"]) == 1:
            package_entered.set()
            assert release_packages.wait(timeout=5)

    monkeypatch.setattr(module, "install_packages", install_packages)
    monkeypatch.setattr(module, "ensure_service_user", lambda: mutators.append("user"))

    def ensure_runtime_env():
        mutators.append("env")
        return "unused", "unused"

    monkeypatch.setattr(module, "ensure_runtime_env", ensure_runtime_env)
    monkeypatch.setattr(module, "ensure_postgres", lambda _password: mutators.append("postgres"))
    monkeypatch.setattr(module, "install_unit", lambda: mutators.append("unit"))

    def smoke():
        mutators.append("smoke")
        return True

    monkeypatch.setattr(module, "smoke", smoke)
    monkeypatch.setattr(sys, "argv", ["installer", "--repo-root", str(repo)])
    first_result = []
    first_error = []

    def first_main():
        try:
            first_result.append(module.main())
        except BaseException as exc:
            first_error.append(exc)

    first = threading.Thread(target=first_main, daemon=True)
    first.start()
    assert package_entered.wait(timeout=2)
    try:
        with pytest.raises(RuntimeError, match="concurrent release installation"):
            module.main()
    finally:
        release_packages.set()
        first.join(timeout=5)

    assert not first.is_alive()
    assert first_error == []
    assert first_result == [0]
    assert mutators == ["packages", "user", "env", "postgres", "unit", "smoke"]
    assert _current_target(base) == previous.absolute()
    assert sentinel.read_text(encoding="utf-8") == "preserve-me"


def test_readiness_failure_rolls_back_current(tmp_path, monkeypatch):
    module = _module()
    base, repo, revision_ref, calls, successful_run = _release_fixture(
        module, tmp_path, monkeypatch
    )
    previous = module.install_release(repo)
    sentinel = previous / "operator-sentinel.txt"
    sentinel.write_text("preserve-me", encoding="utf-8")
    revision_ref["value"] = "2" * 12
    monkeypatch.setattr(module, "require_root", lambda: None)
    monkeypatch.setattr(module, "ensure_service_user", lambda: None)
    monkeypatch.setattr(module, "ensure_runtime_env", lambda: ("unused", "unused"))
    monkeypatch.setattr(module, "ensure_postgres", lambda _password: None)
    monkeypatch.setattr(module, "install_unit", lambda: None)
    monkeypatch.setattr(module, "smoke", lambda: False)
    lock_checked = False

    def observe_service_restore(args, **kwargs):
        nonlocal lock_checked
        result = successful_run(args, **kwargs)
        restarts = [
            command
            for command, _options in calls
            if command[:2] == ["systemctl", "restart"]
        ]
        if len(restarts) == 2:
            checker = (
                "import fcntl,os,sys;"
                "fd=os.open(sys.argv[1],os.O_RDWR);"
                "\ntry: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)"
                "\nexcept BlockingIOError: raise SystemExit(73)"
            )
            checked = subprocess.run(
                [sys.executable, "-c", checker, str(base / ".install.lock")],
                check=False,
            )
            assert checked.returncode == 73
            lock_checked = True
        return result

    monkeypatch.setattr(module, "run", observe_service_restore)
    monkeypatch.setattr(
        sys,
        "argv",
        ["installer", "--repo-root", str(repo), "--skip-packages"],
    )

    assert module.main() == 1
    assert _current_target(base) == previous.absolute()
    assert sentinel.read_text(encoding="utf-8") == "preserve-me"
    assert lock_checked is True
    service_commands = [
        args for args, _kwargs in calls if args[:2] == ["systemctl", "restart"]
    ]
    assert service_commands == [
        ["systemctl", "restart", "todo-global-gateway.service"],
        ["systemctl", "restart", "todo-global-gateway.service"],
    ]
