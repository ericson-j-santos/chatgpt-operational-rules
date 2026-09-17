from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "docker-compose.pc24x7.yml"
ENV_FILE = Path(os.environ.get("PC24X7_ENV_FILE", r"C:\Users\Windows\AppData\Local\ReqSys\TodoGlobal24x7\runtime.env"))


def run(*args: str) -> None:
    subprocess.run(list(args), cwd=ROOT, check=True)


def main() -> int:
    if not ENV_FILE.exists():
        raise SystemExit(f"runtime env not found: {ENV_FILE}")
    run("docker", "compose", "--env-file", str(ENV_FILE), "-f", str(COMPOSE), "build", "worker")
    run("docker", "compose", "--env-file", str(ENV_FILE), "-f", str(COMPOSE), "up", "-d", "--no-deps", "--force-recreate", "worker")
    run("docker", "inspect", "--format", "{{.State.Health.Status}}", "todo-global-24x7-worker-1")
    run("docker", "exec", "todo-global-24x7-worker-1", "python", "-c", "import os; assert os.environ['WORKER_NODE_ID']=='DESKTOP-PDQK954'; print(os.environ['WORKER_NODE_ID'])")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
