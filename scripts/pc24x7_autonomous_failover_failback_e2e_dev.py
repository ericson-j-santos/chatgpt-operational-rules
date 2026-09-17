from __future__ import annotations

import hashlib
import json
import subprocess
import time
import uuid

DB_CONTAINER = "todo-global-24x7-db-1"
DB_USER = "todo_global_bus_dev_user"
DB_NAME = "todo_global_bus_dev"
DESKTOP_WORKER = "todo-global-24x7-worker-1"
DESKTOP = "DESKTOP-PDQK954"
NOTERI = "Noteri"
TTL_SECONDS = 20
STABILITY_SECONDS = 15


def run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", check=check)


def psql(sql: str) -> str:
    result = run([
        "docker", "exec", DB_CONTAINER, "psql", "-U", DB_USER, "-d", DB_NAME,
        "-At", "-F", "|", "-c", sql,
    ], check=False)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout)[-800:])
    return result.stdout.strip()


def health() -> dict[str, tuple[bool, int, int]]:
    rows = psql(
        "SELECT node_id,healthy,heartbeat_age_seconds,healthy_age_seconds "
        f"FROM todo_bus.get_runtime_node_health('{DESKTOP}','{NOTERI}',{TTL_SECONDS},{STABILITY_SECONDS})"
    )
    result: dict[str, tuple[bool, int, int]] = {}
    for line in rows.splitlines():
        if not line:
            continue
        node, healthy, heartbeat_age, healthy_age = line.split("|", 3)
        result[node] = (healthy == "t", int(heartbeat_age), int(healthy_age))
    return result


def wait_health(node: str, expected: bool, timeout_seconds: int) -> dict[str, tuple[bool, int, int]]:
    deadline = time.monotonic() + timeout_seconds
    last: dict[str, tuple[bool, int, int]] = {}
    while time.monotonic() < deadline:
        last = health()
        if node in last and last[node][0] is expected:
            return last
        time.sleep(1)
    raise AssertionError(f"health timeout node={node} expected={expected} last={last}")


def create_continuation(label: str) -> tuple[str, str]:
    suffix = uuid.uuid4().hex[:12]
    corr = f"ha-{label}-{suffix}"
    req = f"req-{suffix}"
    idem = hashlib.sha256(corr.encode("utf-8")).hexdigest()
    basis = f"evt-{suffix}"
    payload = json.dumps({
        "automation_action": "ai_control_plane.validate_idempotent_continuation.v1",
        "next_action": "validar continuidade idempotente",
        "external_id": f"desktop-24x7-e2e-{suffix}",
    }, separators=(",", ":"))
    psql(
        "INSERT INTO todo_bus.continuation_requests("
        "request_id,idempotency_key,basis_event_id,correlation_id,project,todo_payload) VALUES ("
        f"'{req}','{idem}','{basis}','{corr}','AI Control Plane','{payload}'::jsonb)"
    )
    return req, corr


def wait_completion(req: str, corr: str, timeout_seconds: int = 45) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    last = ""
    while time.monotonic() < deadline:
        last = psql(
            "SELECT c.state,c.attempts,coalesce(d.node_id,''),coalesce(d.reason,''),coalesce(d.fencing_token,0) "
            "FROM todo_bus.continuation_requests c LEFT JOIN todo_bus.host_route_decisions d "
            f"ON d.correlation_id=c.correlation_id WHERE c.request_id='{req}'"
        )
        if last:
            state, attempts, node, reason, token = last.split("|", 4)
            if state in {"COMPLETED", "DLQ", "HUMAN_GATE"}:
                history_row = psql(
                    "SELECT string_agg(to_state,',' ORDER BY history_id),"
                    "string_agg(attempts::text,',' ORDER BY history_id) "
                    f"FROM todo_bus.continuation_history WHERE request_id='{req}'"
                )
                history, history_attempts = history_row.split("|", 1)
                return {
                    "request_id": req,
                    "correlation_id": corr,
                    "state": state,
                    "attempts": int(attempts),
                    "node_id": node,
                    "reason": reason,
                    "fencing_token": int(token),
                    "history": history,
                    "history_attempts": history_attempts,
                }
        time.sleep(0.5)
    raise AssertionError(f"completion timeout req={req} last={last!r}")


def assert_execution(proof: dict[str, object], owner: str) -> None:
    if proof["state"] != "COMPLETED":
        raise AssertionError(f"unexpected state: {proof}")
    if proof["attempts"] != 1:
        raise AssertionError(f"expected exactly one final attempt: {proof}")
    if proof["node_id"] != owner:
        raise AssertionError(f"unexpected owner expected={owner}: {proof}")

    states = str(proof["history"]).split(",")
    history_attempts = [int(x) for x in str(proof["history_attempts"]).split(",") if x]
    if not states or states[0] != "PENDING" or states[-2:] != ["PROCESSING", "COMPLETED"]:
        raise AssertionError(f"unexpected terminal history: {proof}")
    if states.count("COMPLETED") != 1 or any(x in {"DLQ", "HUMAN_GATE"} for x in states):
        raise AssertionError(f"duplicate/error terminal transition: {proof}")
    if not history_attempts or max(history_attempts) > 1:
        raise AssertionError(f"attempt counter exceeded one: {proof}")
    proof["route_release_cycles"] = max(0, states.count("PENDING") - 1)


def main() -> int:
    run(["docker", "inspect", DESKTOP_WORKER])
    initial = wait_health(DESKTOP, True, 45)
    if not initial.get(NOTERI, (False, 0, 0))[0]:
        raise AssertionError(f"Noteri not healthy before E2E: {initial}")

    req_primary, corr_primary = create_continuation("primary")
    primary = wait_completion(req_primary, corr_primary)
    assert_execution(primary, DESKTOP)

    stopped = False
    try:
        run(["docker", "stop", DESKTOP_WORKER])
        stopped = True
        after_stop = wait_health(DESKTOP, False, TTL_SECONDS + 20)
        if not after_stop.get(NOTERI, (False, 0, 0))[0]:
            raise AssertionError(f"Noteri not healthy during failover: {after_stop}")

        req_failover, corr_failover = create_continuation("failover")
        failover = wait_completion(req_failover, corr_failover)
        assert_execution(failover, NOTERI)

        repeated = psql(
            "SELECT node_id,reason,fencing_token FROM todo_bus.claim_host_route("
            f"'{corr_failover}','{DESKTOP}','repeat_same_correlation_control')"
        )
        expected_repeat = f"{NOTERI}|{failover['reason']}|{failover['fencing_token']}"
        if repeated != expected_repeat:
            raise AssertionError(f"route idempotency failed: {repeated!r} != {expected_repeat!r}")

        run(["docker", "start", DESKTOP_WORKER])
        stopped = False
        after_start = wait_health(DESKTOP, True, TTL_SECONDS + STABILITY_SECONDS + 30)
        if after_start[DESKTOP][2] < STABILITY_SECONDS:
            raise AssertionError(f"primary became healthy before stability window: {after_start}")

        req_failback, corr_failback = create_continuation("failback")
        failback = wait_completion(req_failback, corr_failback)
        assert_execution(failback, DESKTOP)

        tokens = [int(primary["fencing_token"]), int(failover["fencing_token"]), int(failback["fencing_token"])]
        if not (tokens[0] < tokens[1] < tokens[2]):
            raise AssertionError(f"fencing tokens not increasing: {tokens}")

        print(json.dumps({
            "status": "ok",
            "primary": primary,
            "failover": failover,
            "failback": failback,
            "route_repeat_same_correlation": repeated,
            "fencing_tokens": tokens,
            "ttl_seconds": TTL_SECONDS,
            "primary_stability_seconds": STABILITY_SECONDS,
            "autonomous_failover": True,
            "autonomous_failback": True,
            "single_execution": True,
        }, sort_keys=True))
        return 0
    finally:
        if stopped:
            run(["docker", "start", DESKTOP_WORKER], check=False)


if __name__ == "__main__":
    raise SystemExit(main())
