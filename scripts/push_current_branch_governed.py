from __future__ import annotations
import subprocess
import sys

EXPECTED_BRANCH = "feat/continuous-continuation-worker-20260917"
EXPECTED_SHA = "a0eff1ced29e05dddbdaa09ce7122e1297282988"


def run(*args: str) -> str:
    return subprocess.check_output(args, text=True).strip()


def main() -> int:
    branch = run("git", "branch", "--show-current")
    head = run("git", "rev-parse", "HEAD")
    if branch != EXPECTED_BRANCH or head != EXPECTED_SHA:
        raise SystemExit(f"state_mismatch branch={branch} head={head}")
    if run("git", "status", "--porcelain"):
        raise SystemExit("worktree_not_clean")
    subprocess.run(["git", "push", "-u", "origin", EXPECTED_BRANCH], check=True)
    remote = run("git", "ls-remote", "--heads", "origin", EXPECTED_BRANCH).split()[0]
    if remote != EXPECTED_SHA:
        raise SystemExit(f"remote_sha_mismatch remote={remote}")
    print(f"PUSH_OK branch={EXPECTED_BRANCH} sha={remote}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
