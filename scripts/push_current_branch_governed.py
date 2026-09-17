from __future__ import annotations
import subprocess
import sys

EXPECTED_BRANCH = "feat/continuous-continuation-worker-20260917"


def run(*args: str) -> str:
    return subprocess.check_output(args, text=True).strip()


def main() -> int:
    if len(sys.argv) != 2 or len(sys.argv[1]) != 40:
        raise SystemExit("usage: push_current_branch_governed.py EXPECTED_SHA")
    expected_sha = sys.argv[1].lower()
    branch = run("git", "branch", "--show-current")
    head = run("git", "rev-parse", "HEAD").lower()
    if branch != EXPECTED_BRANCH or head != expected_sha:
        raise SystemExit(f"state_mismatch branch={branch} head={head}")
    if run("git", "status", "--porcelain"):
        raise SystemExit("worktree_not_clean")
    subprocess.run(["git", "push", "-u", "origin", EXPECTED_BRANCH], check=True)
    remote = run("git", "ls-remote", "--heads", "origin", EXPECTED_BRANCH).split()[0].lower()
    if remote != expected_sha:
        raise SystemExit(f"remote_sha_mismatch remote={remote}")
    print(f"PUSH_OK branch={EXPECTED_BRANCH} sha={remote}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
