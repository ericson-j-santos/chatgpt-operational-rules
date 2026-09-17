#!/usr/bin/env python3
"""Consumidor contínuo da fila governada de continuação de TODOs."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Protocol

from scripts.host_router import HostRouter, NodeHealth


@dataclass(frozen=True)
class Continuation:
    request_id: str
    idempotency_key: str
    correlation_id: str
    project: str
    todo: dict[str, Any]
    attempts: int


class Queue(Protocol):
    def reserve_continuations(self, limit: int, lease_seconds: int) -> list[Continuation]: ...
    def complete_continuation(self, request_id: str) -> bool: ...
    def gate_continuation(self, request_id: str, detail: str) -> bool: ...
    def fail_continuation(self, request_id: str, error: str, max_attempts: int, backoff_seconds: int) -> str: ...


class PostgresContinuationQueue:
    def __init__(self, dsn: str) -> None:
        if not dsn:
            raise ValueError("DATABASE_URL is required")
        self.dsn = dsn

    def _connect(self):
        import psycopg
        return psycopg.connect(self.dsn)

    @staticmethod
    def _items(rows) -> list[Continuation]:
        return [Continuation(str(r[0]), str(r[1]), str(r[3]), str(r[4]), dict(r[5]), int(r[7])) for r in rows]

    def reserve_continuations(self, limit: int, lease_seconds: int) -> list[Continuation]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM todo_bus.reserve_continuations(%s,%s)", (limit, lease_seconds))
            rows = cur.fetchall()
        return self._items(rows)

    def reserve_continuations_for_node(self, node_id: str, limit: int, lease_seconds: int) -> list[Continuation]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM todo_bus.reserve_continuations_for_node(%s,%s,%s)",
                        (node_id, limit, lease_seconds))
            rows = cur.fetchall()
        return self._items(rows)

    def _transition(self, sql: str, params: tuple[Any, ...]) -> Any:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()[0]

    def complete_continuation(self, request_id: str) -> bool:
        return bool(self._transition("SELECT todo_bus.complete_continuation(%s)", (request_id,)))

    def gate_continuation(self, request_id: str, detail: str) -> bool:
        return bool(self._transition("SELECT todo_bus.gate_continuation(%s,%s)", (request_id, detail)))

    def fail_continuation(self, request_id: str, error: str, max_attempts: int, backoff_seconds: int) -> str:
        return str(self._transition(
            "SELECT todo_bus.fail_continuation(%s,%s,%s,%s)",
            (request_id, error, max_attempts, backoff_seconds),
        ))

    def release_continuation_route(self, request_id: str) -> bool:
        return bool(self._transition("SELECT todo_bus.release_continuation_route(%s)", (request_id,)))

    def heartbeat(self, worker_id: str, counts: dict[str, int]) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT todo_bus.touch_worker_heartbeat(%s,%s::jsonb)",
                        (worker_id, json.dumps(counts, sort_keys=True)))


class HumanGate(RuntimeError):
    pass


def _validate_idempotent_continuation(item: Continuation) -> None:
    expected = "validar continuidade idempotente"
    if item.project != "AI Control Plane":
        raise HumanGate("automation_action_project_not_allowed")
    if str(item.todo.get("next_action") or "").strip().casefold() != expected:
        raise HumanGate("automation_action_payload_mismatch")
    if not str(item.todo.get("external_id") or "").startswith("desktop-24x7-e2e-"):
        raise HumanGate("automation_action_external_id_not_allowed")


def _validate_head(value: object) -> str:
    head = str(value or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        raise HumanGate("automation_action_expected_head_invalid")
    return head


def _validate_repo(item: Continuation) -> tuple[Path, str]:
    if item.project != "AI Control Plane":
        raise HumanGate("automation_action_project_not_allowed")
    repo = Path(str(item.todo.get("repo_path") or "")).resolve()
    if not repo.is_dir() or not (repo / ".git").exists():
        raise HumanGate("automation_action_repo_invalid")
    return repo, _validate_head(item.todo.get("expected_head"))


def _run_governed(repo: Path, head: str, script: str, extra: list[str] | None = None) -> None:
    root = Path(__file__).resolve().parents[1]
    launcher = root / "scripts" / "session_launcher.py"
    policy = root / "config" / "command-gateway.policy.json"
    session = "auto-" + head[:12]
    corr = "continuation-" + head[:12]
    launch = subprocess.run([sys.executable, str(launcher), "--policy", str(policy), "--repo", str(repo),
        "--session-id", session, "--correlation-id", corr, "--expected-head", head], capture_output=True, text=True)
    if launch.returncode != 0:
        raise RuntimeError("session_launch_failed:" + (launch.stderr or launch.stdout)[-240:])
    data = json.loads(launch.stdout.strip().splitlines()[-1])
    target = Path(data["target_path"])
    gateway = root / "scripts" / "command_gateway.py"
    cmd=[sys.executable, str(gateway), "--policy", str(policy), "--correlation-id", corr, "run", "--cwd", str(target),
         "--session-id", session, "--risk", "1", "--expected-head", head, "--", sys.executable, script]
    if extra: cmd.extend(extra)
    run=subprocess.run(cmd, capture_output=True, text=True)
    if run.returncode != 0:
        raise RuntimeError("governed_execution_failed:" + (run.stdout + run.stderr)[-240:])


def _run_ci_manifest_recovery(item: Continuation) -> None:
    repo, head = _validate_repo(item)
    _run_governed(repo, head, "scripts/pr_ci_self_heal.py", ["--target-root", str(repo), "--expected-head", head])


def _run_e2e_validation(item: Continuation) -> None:
    repo, head = _validate_repo(item)
    script = str(item.todo.get("e2e_script") or "").strip()
    allowed = {"scripts/todo_event_bus_e2e.py", "scripts/todo_gateway_pc24x7_e2e.py", "scripts/session_launcher_e2e.py"}
    if script not in allowed:
        raise HumanGate("automation_action_e2e_not_allowed")
    _run_governed(repo, head, script)


def _run_known_state_recovery(item: Continuation) -> None:
    repo, head = _validate_repo(item)
    recovery = str(item.todo.get("recovery") or "").strip()
    allowed = {"manifest": "scripts/pr_ci_self_heal.py"}
    script = allowed.get(recovery)
    if not script:
        raise HumanGate("automation_action_recovery_not_allowed")
    _run_governed(repo, head, script, ["--target-root", str(repo), "--expected-head", head])


ACTION_REGISTRY = {
    "ai_control_plane.validate_idempotent_continuation.v1": _validate_idempotent_continuation,
    "operational_rules.ci_manifest_recovery.v1": _run_ci_manifest_recovery,
    "operational_rules.e2e_validation.v1": _run_e2e_validation,
    "operational_rules.known_state_recovery.v1": _run_known_state_recovery,
}


def execute(item: Continuation) -> None:
    action = str(item.todo.get("automation_action") or "").strip()
    if not action:
        raise HumanGate("automation_action_missing")
    handler = ACTION_REGISTRY.get(action)
    if handler is None:
        raise HumanGate(f"automation_action_not_registered:{action}")
    handler(item)


def _reserve(queue: Queue, node_id: str | None, limit: int, lease_seconds: int) -> list[Continuation]:
    if node_id and hasattr(queue, "reserve_continuations_for_node"):
        return queue.reserve_continuations_for_node(node_id, limit, lease_seconds)
    return queue.reserve_continuations(limit, lease_seconds)


def process_batch(queue: Queue, *, limit: int = 10, lease_seconds: int = 120,
                  max_attempts: int = 3, backoff_seconds: int = 30,
                  router: HostRouter | None = None, node_id: str | None = None,
                  nodes: list[NodeHealth] | None = None) -> dict[str, int]:
    counts = {"reserved": 0, "completed": 0, "human_gate": 0, "retry": 0, "dlq": 0, "routed_elsewhere": 0}
    for item in _reserve(queue, node_id, limit, lease_seconds):
        counts["reserved"] += 1
        try:
            if router is not None and node_id:
                decision = router.select(item.correlation_id, nodes or [NodeHealth(node_id, True)])
                if decision.node_id != node_id:
                    release = getattr(queue, "release_continuation_route", None)
                    if release is None or not release(item.request_id):
                        raise RuntimeError("route_release_rejected")
                    counts["routed_elsewhere"] += 1
                    continue
            execute(item)
            if not queue.complete_continuation(item.request_id):
                raise RuntimeError("completion_transition_rejected")
            counts["completed"] += 1
        except HumanGate as exc:
            queue.gate_continuation(item.request_id, str(exc))
            counts["human_gate"] += 1
        except Exception as exc:
            state = queue.fail_continuation(item.request_id, str(exc), max_attempts, backoff_seconds)
            counts["dlq" if state == "DLQ" else "retry"] += 1
    return counts


def _routing_runtime() -> tuple[str, HostRouter, list[NodeHealth]]:
    primary = os.environ.get("ROUTER_PRIMARY_NODE", "DESKTOP-PDQK954").strip()
    secondary = os.environ.get("ROUTER_SECONDARY_NODE", "Noteri").strip()
    node_id = os.environ.get("WORKER_NODE_ID", primary).strip()
    healthy_raw = os.environ.get("ROUTER_HEALTHY_NODES", node_id)
    healthy = {x.strip() for x in healthy_raw.split(",") if x.strip()}
    nodes = [NodeHealth(primary, primary in healthy), NodeHealth(secondary, secondary in healthy)]
    return node_id, HostRouter(primary, secondary), nodes


def main() -> int:
    parser = argparse.ArgumentParser(description="Worker contínuo de continuações")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval-seconds", type=int, default=5)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--lease-seconds", type=int, default=120)
    parser.add_argument("--worker-id", default=os.environ.get("WORKER_ID", "continuation-worker-pc24x7"))
    args = parser.parse_args()
    if not 1 <= args.limit <= 100 or not 1 <= args.lease_seconds <= 3600:
        raise SystemExit("invalid worker limits")
    queue = PostgresContinuationQueue(os.environ.get("DATABASE_URL", ""))
    node_id, router, nodes = _routing_runtime()
    while True:
        result = process_batch(queue, limit=args.limit, lease_seconds=args.lease_seconds,
                               router=router, node_id=node_id, nodes=nodes)
        queue.heartbeat(args.worker_id, result)
        print(json.dumps({**result, "node_id": node_id}, sort_keys=True), flush=True)
        if args.once:
            return 0
        time.sleep(max(1, min(args.interval_seconds, 300)))


if __name__ == "__main__":
    raise SystemExit(main())
