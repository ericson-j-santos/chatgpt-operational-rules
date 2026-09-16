#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from unittest import mock

from scripts import materialize_clean_repo as m


def git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return completed.stdout.strip()


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="clean-repo-e2e-") as tmp:
        root = Path(tmp)
        source = root / "source"
        source.mkdir()
        git(source, "init", "--quiet")
        git(source, "config", "user.email", "fixture@example.invalid")
        git(source, "config", "user.name", "Fixture")
        marker = "clean-repo-e2e-marker"
        (source / "marker.txt").write_text(marker + "\n", encoding="utf-8")
        git(source, "add", "marker.txt")
        git(source, "commit", "--quiet", "-m", "fixture")
        commit = git(source, "rev-parse", "HEAD")

        workers = root / "workers"
        workers.mkdir()
        target = workers / "materialized"
        with mock.patch.dict(m.REPOSITORIES, {"fixture/repo": str(source)}, clear=False):
            result = m.materialize(
                "fixture/repo", commit, target, workers, "clean-repo-e2e-positive"
            )
            assert result["result"] == "CLEAN_REPO_MATERIALIZED"
            assert result["commit"] == commit
            assert result["clean"] is True
            assert (target / "marker.txt").read_text(encoding="utf-8").strip() == marker
            assert git(target, "status", "--porcelain=v1") == ""

            try:
                m.materialize(
                    "fixture/repo", commit, target, workers, "clean-repo-e2e-replay"
                )
            except m.MaterializeError as exc:
                assert "já existe" in str(exc)
            else:
                raise AssertionError("replay sobre destino existente deveria falhar")

        blocked = workers / "blocked"
        try:
            m.materialize("not/allowed", commit, blocked, workers, "clean-repo-e2e-negative")
        except m.MaterializeError as exc:
            assert "não permitido" in str(exc)
            assert not blocked.exists()
        else:
            raise AssertionError("repositório fora da allowlist deveria falhar")

    print("MATERIALIZE_CLEAN_REPO_E2E_OK positive=1 replay_blocked=1 allowlist_blocked=1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())