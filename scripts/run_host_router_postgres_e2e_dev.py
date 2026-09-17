#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTAINER = 'todo-global-24x7-worker-1'
CANDIDATE = '/tmp/host_router_candidate.py'
E2E = '/tmp/host_router_postgres_e2e.py'


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        check=False,
        encoding='utf-8',
        errors='replace',
    )


def main() -> int:
    copies = [
        ['docker', 'cp', str(ROOT / 'scripts' / 'host_router.py'), f'{CONTAINER}:{CANDIDATE}'],
        ['docker', 'cp', str(ROOT / 'scripts' / 'host_router_postgres_e2e.py'), f'{CONTAINER}:{E2E}'],
    ]
    for command in copies:
        result = run(command)
        if result.returncode != 0:
            raise RuntimeError(f'docker cp failed exit={result.returncode}: {result.stderr.strip()}')

    launcher = (
        "import importlib.util,sys;"
        "s=importlib.util.spec_from_file_location('scripts.host_router','/tmp/host_router_candidate.py');"
        "m=importlib.util.module_from_spec(s);sys.modules['scripts.host_router']=m;s.loader.exec_module(m);"
        "exec(compile(open('/tmp/host_router_postgres_e2e.py',encoding='utf-8').read(),'/tmp/host_router_postgres_e2e.py','exec'))"
    )
    result = run(['docker', 'exec', CONTAINER, 'python', '-c', launcher])
    if result.returncode != 0:
        raise RuntimeError(f'e2e failed exit={result.returncode}: {result.stderr.strip()}')

    output = result.stdout.strip()
    payload = json.loads(output.splitlines()[-1])
    if payload.get('status') != 'ok' or payload.get('persistent_restart') is not True:
        raise AssertionError(f'unexpected e2e payload: {payload!r}')
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
