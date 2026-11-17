"""Vinculo tipado Risk2 do Noteri, exclusivo de um Gateway previamente validado.

NAO e comando/CLI nem autorizacao do chat. A instalacao e habilitacao fisica
continuam bloqueadas ate readback de politica, host, lease e aprovacoes GitHub.
O Gateway e o unico chamador permitido e deve produzir TrustedGatewayFacts
com fatos de sessao verificados internamente (nunca entrada do solicitante).
"""
from __future__ import annotations

import json
import os
import re
import socket
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from scripts import command_gateway as gateway
from scripts import noteri_github_review_readonly as review

MAINTENANCE_SHA = "fd07d0742ad345efe934c7b800145765aaba6462"
REPOSITORY = "ericson-j-santos/noteri-runtime"
TASK = r"\Automation\NoteriWorkerPoolApi"
CONFIRM = "MAINTAIN-EXISTING-NOTERI-WORKER-POOL-API"
ENVIRONMENT_VARIABLE = "NOTERI_POOL_MAINTENANCE_APPROVED_SHA"
POLICY_FLAG = "noteri_inplace_maintenance_approval_binding"
ID = re.compile(r"[A-Za-z0-9._:-]{3,128}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class BindingBlocked(RuntimeError):
    pass


@dataclass(frozen=True)
class TrustedGatewayFacts:
    """Produzido pelo Gateway APOS validar repositorio, sessao e worktree."""
    session_id: str
    correlation_id: str
    source_repo: str
    expected_head: str
    task_name: str
    risk: int
    mode: str
    command_confirm: str
    state_validated: bool
    session_launch_ok: bool
    clean_worktree: bool
    exclusive_lease: bool
    fencing_token: int
    session_snapshot_sha256: str
    policy_sha256: str
    zero_incremental_cost_verified: bool


def _host_matches() -> bool:
    return os.name == "nt" and socket.gethostname().casefold() == "noteri"


def _validate(facts: TrustedGatewayFacts, policy: dict[str, Any], workflow_sha: str) -> None:
    if not isinstance(facts, TrustedGatewayFacts) or not isinstance(policy, dict):
        raise BindingBlocked("gateway_facts_missing")
    if policy.get(POLICY_FLAG) is not True:
        raise BindingBlocked("capability_not_enabled")
    if not _host_matches():
        raise BindingBlocked("host_not_allowed")
    if (facts.source_repo != REPOSITORY or facts.expected_head != MAINTENANCE_SHA
        or facts.task_name != TASK or type(facts.risk) is not int or facts.risk != 2
        or facts.mode != "apply" or facts.command_confirm != CONFIRM):
        raise BindingBlocked("maintenance_scope_mismatch")
    if (facts.state_validated is not True or facts.session_launch_ok is not True
        or facts.clean_worktree is not True or facts.exclusive_lease is not True
        or facts.zero_incremental_cost_verified is not True
        or type(facts.fencing_token) is not int or facts.fencing_token <= 0):
        raise BindingBlocked("runtime_authority_unverified")
    if (not isinstance(facts.session_id, str) or not ID.fullmatch(facts.session_id)
        or not isinstance(facts.correlation_id, str) or not ID.fullmatch(facts.correlation_id)
        or not isinstance(facts.session_snapshot_sha256, str)
        or not SHA256.fullmatch(facts.session_snapshot_sha256)
        or not isinstance(facts.policy_sha256, str)
        or not SHA256.fullmatch(facts.policy_sha256)):
        raise BindingBlocked("session_identity_invalid")
    if (not isinstance(workflow_sha, str) or not review.SHA40.fullmatch(workflow_sha)
        or policy.get("noteri_approval_workflow_sha") != workflow_sha
        or policy.get("noteri_maintenance_exact_sha") != MAINTENANCE_SHA):
        raise BindingBlocked("installed_policy_binding_invalid")


def bind_once(*, facts: TrustedGatewayFacts, policy: dict[str, Any],
              workflow_run_id: int, workflow_sha: str, github_token: str,
              now: datetime | None = None) -> dict[str, str]:
    """Le a prova oficial e reserva consumo unico ANTES de emitir o env restrito.

    Deve ser chamado apenas dentro da secao critica do Gateway, antes de spawn
    da capability fixa. Nao aceita corpo de aprovacao, bool ou flag do usuario.
    No timeout apos o claim, a reserva continua ocupada ate reconciliacao.
    """
    _validate(facts, policy, workflow_sha)
    if type(workflow_run_id) is not int or workflow_run_id <= 0:
        raise BindingBlocked("workflow_run_id_invalid")
    if not isinstance(github_token, str) or not github_token or len(github_token) > 4096:
        raise BindingBlocked("github_read_token_unavailable")
    try:
        run, approvals, environment = review.consultar_evidencia_github(
            workflow_run_id, token=github_token,
        )
        verdict = review.validar_evidencia(
            execucao=run, revisoes=approvals, ambiente=environment,
            run_id=workflow_run_id, sha_workflow_instalado=workflow_sha,
            agora=now,
        )
    except (review.RecusaDeAutorizacao, ValueError, TypeError) as exc:
        raise BindingBlocked("github_review_readback_failed") from exc
    if (not isinstance(verdict, dict) or verdict.get("result") != "REVIEW_PROVENANCE_VERIFIED"
        or verdict.get("review_provenance_verified") is not True
        or verdict.get("maintenance_sha") != MAINTENANCE_SHA
        or verdict.get("run_id") != workflow_run_id
        or verdict.get("workflow_sha") != workflow_sha
        or verdict.get("execution_authorized") is not False):
        raise BindingBlocked("github_review_not_proven")
    # claim atomico dentro do diretorio privado de estado do Gateway.
    # Repeticao e falha permanente, sem desfazer o registro apos interrupcao.
    name = f"noteri-worker-pool-{workflow_run_id}.json"
    receipt = {
        "kind": "noteri-worker-pool-approval-once-v1",
        "event_id": f"noteri42:approval-consumed:{workflow_run_id}:v1",
        "idempotency_key": f"noteri42:review-run:{workflow_run_id}",
        "run_id": workflow_run_id,
        "workflow_sha": workflow_sha,
        "maintenance_sha": MAINTENANCE_SHA,
        "session_id": facts.session_id,
        "correlation_id": facts.correlation_id,
        "snapshot_sha256": facts.session_snapshot_sha256,
        "policy_sha256": facts.policy_sha256,
        "fencing_token": facts.fencing_token,
    }
    try:
        gateway.secure_state_directory(policy, "noteri-approval-claims")
        claim = gateway.secure_state_path(policy, "noteri-approval-claims", name)
        gateway.write_state_text(
            policy, claim, json.dumps(receipt, sort_keys=True), exclusive=True,
        )
    except FileExistsError as exc:
        raise BindingBlocked("approval_replay_blocked") from exc
    except (gateway.GatewayError, OSError) as exc:
        raise BindingBlocked("durable_consumption_unavailable") from exc
    return {ENVIRONMENT_VARIABLE: MAINTENANCE_SHA}
