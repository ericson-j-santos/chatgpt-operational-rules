#!/usr/bin/env python3
"""One target-only GitHub Environment remediation with independent readback.

Does not approve a deployment, dispatch a workflow, update a host or grant a
maintenance SHA.  No arbitrary URL, repository or environment arguments.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

SOURCE_REPO = "ericson-j-santos/chatgpt-operational-rules"
TARGET_REPO = "ericson-j-santos/noteri-runtime"
OWNER = "ericson-j-santos"
OWNER_ID = 60470500
TARGET_ENVIRONMENT = "noteri-worker-pool-maintenance-dev"
ENV_PATH = f"/repos/{TARGET_REPO}/environments/{TARGET_ENVIRONMENT}"
CONTEXT_REF = "refs/heads/main"
WORKFLOW_PATH = ".github/workflows/noteri-environment-protection.yml"
API = "https://api.github.com"
VERSION = "2026-03-10"
SHA = re.compile(r"[0-9a-f]{40}\Z")
CONFIRM = "CONFIGURE-NOTERI-REVIEW-ONLY-DEV"
MAX_BODY_BYTES = 128 * 1024


class Blocked(RuntimeError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Blocked("api_redirect_blocked")


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate_json_key")
        value[key] = item
    return value


class GitHubAPI:
    """Fixed-host API transport; no arbitrary URL input or redirect follows."""

    def __init__(self, token: str):
        if not token or len(token) > 4096 or "\n" in token or "\r" in token:
            raise Blocked("governance_token_missing_or_invalid")
        self.token = token
        self.opener = build_opener(_NoRedirect(), HTTPSHandler(context=ssl.create_default_context()))

    def request(self, method: str, path: str, payload: dict | None = None) -> tuple[int, Any]:
        allowed = {
            ("GET", f"/repos/{TARGET_REPO}"),
            ("GET", f"/users/{OWNER}"),
            ("GET", ENV_PATH),
            ("PUT", ENV_PATH),
        }
        if (method, path) not in allowed or (method != "PUT" and payload is not None):
            raise Blocked("api_path_or_method_not_allowed")
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode() if payload is not None else None
        headers = {
            "Authorization": "Bearer " + self.token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": VERSION,
            "User-Agent": "noteri-exact-environment-guard/1",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            request = Request(API + path, headers=headers, method=method, data=body)
            with self.opener.open(request, timeout=8) as response:
                status = response.status
                raw = response.read(MAX_BODY_BYTES + 1)
        except HTTPError as exc:
            if exc.code == 404 and method == "GET" and path == ENV_PATH:
                return 404, None
            raise Blocked("github_api_denied_or_unavailable") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise Blocked("github_api_unavailable") from exc
        if status != 200 or len(raw) > MAX_BODY_BYTES:
            raise Blocked("github_api_unexpected_response")
        try:
            value = json.loads(raw, object_pairs_hook=_unique_pairs)
        except (ValueError, UnicodeDecodeError) as exc:
            raise Blocked("github_api_invalid_json") from exc
        if not isinstance(value, dict):
            raise Blocked("github_api_invalid_object")
        return status, value


def workflow_context(env: dict[str, str], *, expected_sha: str) -> None:
    if not isinstance(expected_sha, str) or not SHA.fullmatch(expected_sha):
        raise Blocked("rules_sha_invalid")
    fixed = {
        "GITHUB_REPOSITORY": SOURCE_REPO,
        "GITHUB_REF": CONTEXT_REF,
        "GITHUB_ACTOR": OWNER,
        "GITHUB_TRIGGERING_ACTOR": OWNER,
        "GITHUB_WORKFLOW_REF": f"{SOURCE_REPO}/{WORKFLOW_PATH}@{CONTEXT_REF}",
        "GITHUB_SHA": expected_sha,
        "GITHUB_RUN_ATTEMPT": "1",
    }
    for key, value in fixed.items():
        if env.get(key) != value:
            raise Blocked("trusted_workflow_context_invalid")
    if env.get("GITHUB_EVENT_NAME") not in {"push", "workflow_dispatch"}:
        raise Blocked("workflow_event_not_allowed")
    if not re.fullmatch(r"[1-9][0-9]{0,19}", env.get("GITHUB_RUN_ID", "")):
        raise Blocked("workflow_run_id_invalid")


def _environment_state(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise Blocked("environment_invalid")
    if value.get("name") != TARGET_ENVIRONMENT or type(value.get("id")) is not int or value["id"] <= 0:
        raise Blocked("environment_identity_invalid")
    bypass = value.get("can_admins_bypass")
    if type(bypass) is not bool:
        raise Blocked("admin_bypass_readback_unavailable")
    rules = value.get("protection_rules")
    if not isinstance(rules, list):
        raise Blocked("environment_rules_unavailable")
    if any(not isinstance(rule, dict) or rule.get("type") not in {"required_reviewers", "wait_timer", "branch_policy"} for rule in rules):
        raise Blocked("unrelated_protection_rules_present")
    timer = [r.get("wait_timer") for r in rules if r.get("type") == "wait_timer"]
    if len(timer) > 1 or (timer and (type(timer[0]) is not int or not 0 <= timer[0] <= 43200)):
        raise Blocked("wait_timer_invalid")
    review = [r for r in rules if r["type"] == "required_reviewers"]
    if len(review) > 1:
        raise Blocked("ambiguous_review_rules")
    branch = value.get("deployment_branch_policy")
    if branch is not None and (
        not isinstance(branch, dict)
        or type(branch.get("protected_branches")) is not bool
        or type(branch.get("custom_branch_policies")) is not bool
        or branch["protected_branches"] == branch["custom_branch_policies"]
    ):
        raise Blocked("branch_policy_invalid")
    reviewers = review[0].get("reviewers") if review else None
    is_owner = (
        isinstance(reviewers, list) and len(reviewers) == 1
        and isinstance(reviewers[0], dict) and reviewers[0].get("type") == "User"
        and isinstance(reviewers[0].get("reviewer"), dict)
        and reviewers[0]["reviewer"].get("id") == OWNER_ID
    )
    return {
        "id": value["id"],
        "ready": bypass is False and len(review) == 1
        and review[0].get("prevent_self_review") is False and is_owner,
        "bypass": bypass,
        "wait_timer": timer[0] if timer else 0,
        "deployment_branch_policy": branch,
    }


def reconcile(client: Any, *, mode: str, context: dict[str, str],
              expected_rules_sha: str, confirm: str = "") -> dict[str, Any]:
    if mode not in {"plan", "apply"}:
        raise Blocked("mode_invalid")
    workflow_context(context, expected_sha=expected_rules_sha)
    status, repo = client.request("GET", f"/repos/{TARGET_REPO}")
    if status != 200 or not isinstance(repo, dict) or repo.get("full_name") != TARGET_REPO or repo.get("private") is not False or repo.get("default_branch") != "main" or (not isinstance(repo.get("owner"), dict) or repo["owner"].get("id") != OWNER_ID):
        raise Blocked("target_repository_not_proven_public")
    status, owner = client.request("GET", f"/users/{OWNER}")
    if status != 200 or not isinstance(owner, dict) or owner.get("id") != OWNER_ID or owner.get("login") != OWNER:
        raise Blocked("owner_identity_mismatch")
    status, value = client.request("GET", ENV_PATH)
    if status not in {200, 404} or (status == 404 and value is not None):
        raise Blocked("environment_readback_unavailable")
    before = _environment_state(value)
    if before is not None and before["ready"]:
        return {"result": "PROTECTION_VERIFIED", "changed": False,
                "admin_bypass_disabled": True, "review_required": True,
                "execution_authorized": False}
    if mode == "plan":
        return {"result": "PROTECTION_PLAN_REQUIRED", "changed": False,
                "environment_exists": before is not None,
                "admin_bypass_disabled": False, "review_required": False,
                "execution_authorized": False}
    if confirm != CONFIRM:
        raise Blocked("apply_confirmation_missing")
    payload = {
        "wait_timer": before["wait_timer"] if before else 0,
        "prevent_self_review": False,
        "reviewers": [{"type": "User", "id": OWNER_ID}],
        "deployment_branch_policy": before["deployment_branch_policy"] if before else None,
        # This field is supported by GitHub in practice but is NOT documented
        # in the REST input schema. Never assume it worked without GET readback.
        "can_admins_bypass": False,
    }
    applied_status, _ = client.request("PUT", ENV_PATH, payload)
    if applied_status != 200:
        raise Blocked("github_environment_write_not_accepted")
    status, final = client.request("GET", ENV_PATH)
    if status != 200:
        raise Blocked("write_result_unverifiable")
    after = _environment_state(final)
    if after is None or not after["ready"] or after["wait_timer"] != payload["wait_timer"] or after["deployment_branch_policy"] != payload["deployment_branch_policy"]:
        raise Blocked("write_result_unverified_or_bypass_active")
    return {"result": "PROTECTION_VERIFIED", "changed": True,
            "environment_id": after["id"], "admin_bypass_disabled": True,
            "review_required": True, "execution_authorized": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("plan", "apply"), required=True)
    parser.add_argument("--expected-rules-sha", required=True)
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    try:
        workflow_context(dict(os.environ), expected_sha=args.expected_rules_sha)
        api = GitHubAPI(os.environ.get("REPOSITORY_GOVERNANCE_TOKEN", ""))
        response = reconcile(api, mode=args.mode, context=dict(os.environ),
                             expected_rules_sha=args.expected_rules_sha, confirm=args.confirm)
        print(json.dumps(response, sort_keys=True))
        return 0 if response["result"] == "PROTECTION_VERIFIED" else 2
    except Blocked as exc:
        print(json.dumps({"result": "BLOCKED", "reason": str(exc),
                          "changed": "unknown", "execution_authorized": False}, sort_keys=True))
        return 20


if __name__ == "__main__":
    sys.exit(main())
