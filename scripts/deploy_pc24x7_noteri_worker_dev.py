from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "docker-compose.pc24x7.noteri.yml"
ENV_FILE = Path(os.environ.get(
    "PC24X7_NOTERI_ENV_FILE",
    str(Path.home() / "AppData" / "Local" / "ReqSys" / "TodoGlobal24x7Noteri" / "runtime.env"),
))


def run(*args: str) -> None:
    subprocess.run(list(args), cwd=ROOT, check=True)


def main() -> int:
    if not ENV_FILE.exists():
        raise SystemExit(f"runtime env not found: {ENV_FILE}")
    run("docker", "compose", "--env-file", str(ENV_FILE), "-f", str(COMPOSE), "build", "worker_noteri")
    run("docker", "compose", "--env-file", str(ENV_FILE), "-f", str(COMPOSE), "up", "-d", "--force-recreate", "worker_noteri")
    run("docker", "inspect", "--format", "{{.State.Health.Status}}", "todo-global-24x7-noteri-worker_noteri-1")
    run("docker", "exec", "todo-global-24x7-noteri-worker_noteri-1", "python", "-c", "import os; assert os.environ['WORKER_NODE_ID']=='Noteri'; print('Noteri')")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
