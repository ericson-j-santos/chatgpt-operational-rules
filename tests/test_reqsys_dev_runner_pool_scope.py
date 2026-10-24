from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCOPE = ROOT / "config" / "reqsys-dev-runner-pool-scope.json"


def load_scope() -> dict:
    return json.loads(SCOPE.read_text(encoding="utf-8"))


def test_scope_is_pinned_to_merged_reqsys_contract() -> None:
    scope = load_scope()
    source = scope["source_contract"]
    assert source == {
        "repository": "ericson-j-santos/reqsys-v2-enterprise-real",
        "merged_sha": "e07c31ba551adc2c99f2d1143391e3c71d622f86",
        "path": "config/reqsys-dev-runner-pool.json",
    }


def test_eligibility_is_capability_based_without_physical_host_affinity() -> None:
    scope = load_scope()
    eligibility = scope["eligibility"]
    assert eligibility["based_on_capabilities"] is True
    assert eligibility["shared_label"] == "reqsys-dev-pool-v1"
    assert eligibility["identity_field"] == "runner_instance_id"
    assert eligibility["forbid_fixed_runner_id"] is True
    assert "DESKTOP-PDQK954" in eligibility["forbidden_hostname_affinity"]
    assert "runner_name" not in eligibility
    assert "runner_id" not in eligibility


def test_scope_remains_dev_only_and_fail_closed() -> None:
    scope = load_scope()
    boundaries = scope["boundaries"]
    assert boundaries["allowed_environments"] == ["dev"]
    assert boundaries["denied_environments"] == ["hml", "stg", "prod"]
    assert boundaries["automatic_retry"] is False
    assert boundaries["deploy_authorized"] is False
    assert boundaries["production_touch_authorized"] is False


def test_onboarding_and_canary_require_separate_owner_gates() -> None:
    scope = load_scope()
    assert scope["onboarding"]["physical_owner_authorization_required"] is True
    assert scope["onboarding"]["github_runner_registration_performed_by_script"] is False
    assert scope["onboarding"]["eligibility_requires_positive_preflight"] is True
    assert scope["canary"]["owner_authorization_required"] is True
    assert scope["canary"]["single_attempt"] is True
    assert scope["canary"]["automatic_retry"] is False
