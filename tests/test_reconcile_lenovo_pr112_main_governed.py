from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "reconcile_lenovo_pr112_main_governed.py"
SPEC = importlib.util.spec_from_file_location("reconcile_lenovo_pr112_main_governed", SCRIPT)
assert SPEC and SPEC.loader
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)


def command(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def prepared(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    origin = tmp_path / "origin.git"
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(origin), str(repo)], check=True, capture_output=True)
    command(repo, "config", "user.name", "Fixture")
    command(repo, "config", "user.email", "fixture@example.invalid")
    command(repo, "checkout", "-b", "main")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    command(repo, "add", "base.txt")
    command(repo, "commit", "-m", "base")
    base = command(repo, "rev-parse", "HEAD")
    command(repo, "checkout", "-b", "pr112")
    (repo / "shared.txt").write_text("same change\n", encoding="utf-8")
    command(repo, "add", "shared.txt")
    command(repo, "commit", "-m", "parent with main tree")
    (repo / "native.txt").write_text("native readback\n", encoding="utf-8")
    command(repo, "add", "native.txt")
    command(repo, "commit", "-m", "native readback")
    old = command(repo, "rev-parse", "HEAD")
    command(repo, "push", "origin", "pr112")
    command(repo, "checkout", "main")
    (repo / "shared.txt").write_text("same change\n", encoding="utf-8")
    command(repo, "add", "shared.txt")
    command(repo, "commit", "-m", "squash of shared change")
    main = command(repo, "rev-parse", "HEAD")
    command(repo, "push", "origin", "main")
    command(repo, "checkout", "pr112")

    monkeypatch.setattr(m, "REPO", repo)
    monkeypatch.setattr(m, "BRANCH", "pr112")
    monkeypatch.setattr(m, "OLD_HEAD", old)
    monkeypatch.setattr(m, "MAIN_SHA", main)
    monkeypatch.setattr(m, "BASE_SHA", base)
    monkeypatch.setattr(m.socket, "gethostname", lambda: "DESKTOP-RP23OGS")
    real_output = m.output

    def local_output(*args: str) -> str:
        if args == ("remote", "get-url", "origin"):
            return "https://github.com/ericson-j-santos/desktop-pc24x7-runtime.git"
        return real_output(*args)

    monkeypatch.setattr(m, "output", local_output)
    return repo, origin


def test_tree_equivalent_reconcile_and_replay_are_nonmutating(
    prepared: tuple[Path, Path]
) -> None:
    repo, origin = prepared
    before_tree = command(repo, "show", "-s", "--format=%T", "HEAD")
    first = m.reconcile("lenovo-pr112-test")
    assert first["result"] == "RECONCILED_LOCALLY"
    assert first["parents"] == [m.OLD_HEAD, m.MAIN_SHA]
    assert command(repo, "show", "-s", "--format=%T", "HEAD") == before_tree
    assert command(repo, "status", "--porcelain") == ""
    assert command(origin, "rev-parse", "refs/heads/pr112") == m.OLD_HEAD
    second = m.reconcile("lenovo-pr112-test")
    assert second["result"] == "ALREADY_RECONCILED"
    assert second["head"] == first["head"]


def test_changed_remote_branch_blocks_before_local_commit(
    prepared: tuple[Path, Path]
) -> None:
    repo, origin = prepared
    command(origin, "update-ref", "refs/heads/pr112", m.BASE_SHA)
    with pytest.raises(m.ReconcileBlocked, match="remote_ref_changed"):
        m.reconcile("lenovo-pr112-test")
    assert command(repo, "rev-parse", "HEAD") == m.OLD_HEAD


def test_non_equivalent_main_tree_blocks(
    prepared: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, origin = prepared
    command(repo, "checkout", "main")
    (repo / "other.txt").write_text("different\n", encoding="utf-8")
    command(repo, "add", "other.txt")
    command(repo, "commit", "-m", "different main")
    changed_main = command(repo, "rev-parse", "HEAD")
    command(repo, "push", "origin", "main")
    command(repo, "checkout", "pr112")
    command(repo, "fetch", "origin", "main")
    monkeypatch.setattr(m, "MAIN_SHA", changed_main)
    with pytest.raises(m.ReconcileBlocked, match="main_tree_not_equivalent_to_parent"):
        m.reconcile("lenovo-pr112-test")
    assert command(repo, "rev-parse", "HEAD") == m.OLD_HEAD


def test_wrong_host_blocks_before_git(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(m.socket, "gethostname", lambda: "OTHER")
    with pytest.raises(m.ReconcileBlocked, match="host_mismatch"):
        m.reconcile("lenovo-pr112-test")
