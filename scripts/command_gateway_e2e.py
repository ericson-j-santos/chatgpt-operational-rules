#!/usr/bin/env python3
"""E2E do Command Gateway com controles negativos contra falso positivo."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().with_name("command_gateway.py")


def run_cli(args: list[str], expected: int) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), *args], text=True, capture_output=True,
        check=False, timeout=30,
    )
    if completed.returncode != expected:
        raise AssertionError(
            f"retorno inesperado: esperado={expected} atual={completed.returncode}\n"
            f"stdout={completed.stdout}\nstderr={completed.stderr}"
        )
    return completed


def git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=str(cwd), text=True, capture_output=True,
        check=True, timeout=20,
    )
    return completed.stdout.strip()


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="command-gateway-e2e-"))
    try:
        repo, blocked, state = root / "repo", root / "blocked", root / "state"
        repo.mkdir(); blocked.mkdir()
        git(repo, "init")
        git(repo, "config", "user.email", "gateway-e2e@example.invalid")
        git(repo, "config", "user.name", "Gateway E2E")
        (repo / ".gitignore").write_text(".tmp/\n", encoding="utf-8", newline="\n")
        (repo / "baseline.txt").write_text("baseline\n", encoding="utf-8", newline="\n")
        git(repo, "add", "baseline.txt", ".gitignore"); git(repo, "commit", "-m", "baseline")
        head = git(repo, "rev-parse", "HEAD")

        python_name = Path(sys.executable).name
        policy = {
            "version": 1, "allowed_roots": [str(root)],
            "denied_roots": [str(blocked)], "denied_segments": [".ssh", ".azure"],
            "denied_names": [".env", ".env.*", "*.pem"],
            "allowed_executables": ["git", "python", "python3", python_name],
            "blocked_executables": ["cmd", "powershell", "bash", "sh"],
            "deny_inline_code": False, "require_git_repo": True,
            "risk2_requires_clean_tree": True, "git_untracked_excludes": [".tmp/**"], "max_timeout_seconds": 120,
            "state_dir": str(state),
        }
        policy_path = root / "policy.json"
        policy_path.write_text(json.dumps(policy), encoding="utf-8")
        base = ["--policy", str(policy_path)]

        run_cli([*base, "inspect", "--cwd", str(repo)], 0)
        run_cli([*base, "run", "--cwd", str(repo), "--risk", "1", "--", "git", "status", "--short"], 0)
        run_cli([*base, "inspect", "--cwd", str(blocked)], 20)
        run_cli([*base, "run", "--cwd", str(repo), "--risk", "2", "--expected-head", "0" * 40,
                 "--", "git", "status", "--short"], 20)
        run_cli([*base, "run", "--cwd", str(repo), "--risk", "1", "--", "git", "diff", "--", ".env"], 20)
        run_cli([*base, "run", "--cwd", str(repo), "--risk", "3", "--", "git", "status", "--short"], 20)

        sys.path.insert(0, str(SCRIPT.parent))
        import command_gateway as cg
        state_before = cg.git_state(repo, True)
        assert state_before is not None
        lock_dir = state / "locks"; lock_dir.mkdir(parents=True, exist_ok=True)
        lock_path = lock_dir / f"{cg.state_key(state_before)}.lock"
        lock_path.write_text('{"test":true}', encoding="utf-8")
        try:
            run_cli([*base, "run", "--cwd", str(repo), "--risk", "1", "--", "git", "status", "--short"], 21)
        finally:
            lock_path.unlink(missing_ok=True)

        mutation = repo / "mutation.txt"
        run_cli([*base, "run", "--cwd", str(repo), "--risk", "1", "--", sys.executable, "-c",
                 "__import__('pathlib').Path('mutation.txt').write_text('x', encoding='utf-8')"], 23)
        if not mutation.exists():
            raise AssertionError("mutação controlada não ocorreu; teste negativo seria inválido")
        mutation.unlink()
        if git(repo, "status", "--porcelain"):
            raise AssertionError("repo não voltou ao baseline após teste negativo")

        controlled = repo / "controlled.txt"
        run_cli([*base, "run", "--cwd", str(repo), "--risk", "2", "--expected-head", head, "--",
                 sys.executable, "-c", "__import__('pathlib').Path('controlled.txt').write_text('ok', encoding='utf-8')"], 0)
        if not controlled.exists() or git(repo, "rev-parse", "HEAD") != head:
            raise AssertionError("efeito positivo risco 2 não foi comprovado")
        controlled.unlink()

        lines = [json.loads(line) for line in (state / "events.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        if not lines or not all(item.get("correlation_id") for item in lines):
            raise AssertionError("eventos sem correlation_id")
        if sum(1 for item in lines if item.get("result") == "blocked") < 5:
            raise AssertionError("tentativas bloqueadas não foram auditadas")
        if git(repo, "status", "--porcelain"):
            raise AssertionError("estado final divergente do baseline")
        print("COMMAND_GATEWAY_E2E_OK positive=3 negative=6 false_positive_guard=ok final_state=clean")
        return 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
