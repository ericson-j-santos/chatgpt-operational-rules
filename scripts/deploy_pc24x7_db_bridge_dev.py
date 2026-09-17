from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "docker-compose.pc24x7.bridge.yml"


def run(*args: str) -> None:
    subprocess.run(list(args), cwd=ROOT, check=True)


def main() -> int:
    run("docker", "compose", "-f", str(COMPOSE), "build", "db_bridge")
    run("docker", "compose", "-f", str(COMPOSE), "up", "-d", "--force-recreate", "db_bridge")
    run("docker", "inspect", "--format", "{{.State.Health.Status}}", "todo-global-24x7-db_bridge-1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
