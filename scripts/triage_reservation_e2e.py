"""Bootstrap a disposable governed session, then run HTTP/Postgres E2E through the gateway."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import session_launcher


def main() -> int:
    expected = os.environ.get("EXPECTED_SHA", "")
    ref = os.environ.get("EXPECTED_REF", "")
    if os.environ.get("GITHUB_ACTIONS") != "true" or not re.fullmatch(r"[0-9a-f]{40}", expected) or not ref:
        raise RuntimeError("governed E2E requires immutable GitHub Actions context")
    run_id = os.environ["GITHUB_RUN_ID"]
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")
    session_id = f"triage139-{run_id}-{attempt}"
    correlation = f"triage-reservation-{run_id}-{attempt}"
    output = Path(os.environ["RUNNER_TEMP"]) / "triage-reservation-evidence.json"
    evidence = {"source_sha": expected, "run_id": run_id, "correlation_id": correlation,
                "live_notion": False, "physical_hosts_touched": False, "fixture_data": True,
                "overall_passed": False}
    try:
        with tempfile.TemporaryDirectory(prefix="triage-governed-", dir=os.environ["RUNNER_TEMP"]) as directory:
            temp = Path(directory)
            work = temp / "work"
            work.mkdir()
            policy = json.loads((ROOT / "config/command-gateway.policy.json").read_text())
            policy.update(allowed_roots=[str(work / "*")], worktree_root=str(work),
                          state_dir=str(temp / "state"), session_source_roots=[str(ROOT)],
                          allowed_executables=["python", "python3", "git"], max_timeout_seconds=180)
            policy_path = temp / "policy.json"
            policy_path.write_text(json.dumps(policy), encoding="utf-8")
            launched = session_launcher.launch(ROOT, policy_path, session_id, None, correlation,
                                               expected, sync_ref=f"origin/{ref}")
            if launched.get("result") != "SESSION_LAUNCH_OK" or not launched.get("state_validated") or launched.get("head") != expected:
                raise RuntimeError("session bootstrap did not validate the exact revision")
            evidence["session"] = launched
            command = [sys.executable, str(ROOT / "scripts/command_gateway.py"),
                       "--policy", str(policy_path), "--correlation-id", correlation, "run",
                       "--cwd", launched["target_path"], "--session-id", session_id,
                       "--risk", "2", "--timeout", "180", "--expected-head", expected, "--",
                       sys.executable, "-m", "unittest", "-v", "tests/test_triage_reservation.py"]
            result = subprocess.run(command, capture_output=True, text=True, timeout=190, check=False)
            print(result.stdout)
            print(result.stderr, file=sys.stderr)
            if result.returncode != 0:
                raise RuntimeError("governed reservation E2E failed; no fallback permitted")
            receipt = json.loads(result.stdout.strip().splitlines()[-1])
            if (receipt.get("result") != "ok" or receipt.get("command_exit_code") != 0
                    or receipt.get("gateway_exit_code") != 0 or receipt["after"]["head"] != expected
                    or receipt["after"]["status_count"] != 0):
                raise RuntimeError("gateway effect or final checkout did not validate")
            evidence["gateway"] = receipt
            evidence["overall_passed"] = True
            print("TRIAGE_RESERVATION_E2E_OK real_http=true real_postgres=true governed_session=true live_notion=false")
    except Exception as exc:
        evidence["blocked_error_type"] = type(exc).__name__
        raise
    finally:
        output.write_text(json.dumps(evidence, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
