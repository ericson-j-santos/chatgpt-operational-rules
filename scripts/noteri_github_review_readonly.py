"""Audita a proveniência de uma aprovação GitHub, SEM liberar manutenção.

A interface é deliberadamente somente leitura. O consumidor deverá comprovar,
por conta própria, sessão/host/lease, política instalada, custo zero e consumo
único antes de autorizar qualquer execução física. O resultado deste módulo
NUNCA deve ser usado sozinho como valor de NOTERI_POOL_MAINTENANCE_APPROVED_SHA.
"""
from __future__ import annotations

import json
import re
import ssl
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener, HTTPSHandler

REPOSITORIO = "ericson-j-santos/noteri-runtime"
WORKFLOW = ".github/workflows/noteri-worker-pool-inplace-maintenance.yml"
AMBIENTE = "noteri-worker-pool-maintenance-dev"
USUARIO_PROPRIETARIO_ID = 60470500
SHA_MANUTENCAO = "fd07d0742ad345efe934c7b800145765aaba6462"
IDADE_MAXIMA_SEGUNDOS = 900
SHA40 = re.compile(r"^[0-9a-f]{40}$")


class RecusaDeAutorizacao(ValueError):
    """Controle negativo: nenhuma ação física pode ser liberada."""


class _SemRedirecionamento(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RecusaDeAutorizacao("redirecionamento_nao_permitido")


def _data(value: Any) -> datetime:
    if not isinstance(value, str):
        raise RecusaDeAutorizacao("data_ausente")
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RecusaDeAutorizacao("data_invalida") from exc
    if d.tzinfo is None:
        raise RecusaDeAutorizacao("fuso_ausente")
    return d.astimezone(timezone.utc)


def _objeto(value: Any, code: str) -> dict:
    if not isinstance(value, dict):
        raise RecusaDeAutorizacao(code)
    return value


def _recusa(codigo: str) -> dict[str, Any]:
    return {"result": "BLOCKED", "reason": codigo,
            "review_provenance_verified": False,
            "execution_authorized": False, "state_changed": False}


def validar_evidencia(
    *, execucao: Any, revisoes: Any, ambiente: Any,
    run_id: int, sha_workflow_instalado: str, agora: datetime | None = None,
) -> dict[str, Any]:
    """Verifica prova *externa* de review; não executa nem autoriza serviço.

    sha_workflow_instalado só é confiável se recebido do Gateway/política
    instalada e conferido independentemente, NUNCA do solicitante de execução.
    """
    try:
        if type(run_id) is not int or run_id <= 0:
            raise RecusaDeAutorizacao("run_id_invalido")
        if not isinstance(sha_workflow_instalado, str) or not SHA40.fullmatch(sha_workflow_instalado):
            raise RecusaDeAutorizacao("sha_workflow_nao_fixado")
        run = _objeto(execucao, "execucao_invalida")
        env = _objeto(ambiente, "ambiente_invalido")
        if not isinstance(revisoes, list):
            raise RecusaDeAutorizacao("revisoes_invalidas")
        repo = _objeto(run.get("repository"), "repositorio_ausente")
        actor = _objeto(run.get("actor"), "autor_ausente")
        if (run.get("id") != run_id or repo.get("full_name") != REPOSITORIO or
            run.get("event") != "workflow_dispatch" or run.get("path") != WORKFLOW or
            run.get("head_branch") != "main" or run.get("head_sha") != sha_workflow_instalado or
            run.get("run_attempt") != 1 or run.get("status") != "in_progress" or
            run.get("conclusion") is not None or
            actor.get("id") != USUARIO_PROPRIETARIO_ID):
            raise RecusaDeAutorizacao("identidade_da_execucao_divergente")
        now = agora or datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise RecusaDeAutorizacao("agora_sem_fuso")
        delta = (now.astimezone(timezone.utc) - _data(run.get("created_at"))).total_seconds()
        if not 0 <= delta <= IDADE_MAXIMA_SEGUNDOS:
            raise RecusaDeAutorizacao("execucao_fora_da_janela")
        if (env.get("name") != AMBIENTE or type(env.get("id")) is not int or
            env.get("id") <= 0 or env.get("can_admins_bypass") is not False):
            raise RecusaDeAutorizacao("ambiente_sem_protecao_estrita")
        regras = env.get("protection_rules")
        if not isinstance(regras, list):
            raise RecusaDeAutorizacao("revisores_ausentes")
        found = False
        for regra in regras:
            if not isinstance(regra, dict) or regra.get("type") != "required_reviewers":
                continue
            reviewers = regra.get("reviewers")
            if isinstance(reviewers, list) and any(
                isinstance(item, dict) and item.get("type") == "User" and
                isinstance(item.get("reviewer"), dict) and
                item["reviewer"].get("id") == USUARIO_PROPRIETARIO_ID
                for item in reviewers
            ):
                found = True
        if not found:
            raise RecusaDeAutorizacao("proprietario_nao_e_revisor_exigido")
        valid = []
        for item in revisoes:
            if not isinstance(item, dict):
                raise RecusaDeAutorizacao("historico_de_revisao_invalido")
            if item.get("state") == "rejected":
                raise RecusaDeAutorizacao("historico_rejeitado")
            if item.get("state") != "approved":
                continue
            who = item.get("user")
            environments = item.get("environments")
            if (not isinstance(who, dict) or who.get("id") != USUARIO_PROPRIETARIO_ID or
                not isinstance(environments, list)):
                continue
            if any(isinstance(e, dict) and e.get("id") == env["id"] and
                   e.get("name") == AMBIENTE for e in environments):
                valid.append(item)
        if len(valid) != 1:
            raise RecusaDeAutorizacao("aprovacao_unica_ausente")
        return {"result": "REVIEW_PROVENANCE_VERIFIED", "run_id": run_id,
                "workflow_sha": sha_workflow_instalado,
                "maintenance_sha": SHA_MANUTENCAO,
                "environment_id": env["id"],
                "review_provenance_verified": True,
                "execution_authorized": False, "state_changed": False,
                "next_gate": "session_host_fencing_and_atomic_consumption_required"}
    except RecusaDeAutorizacao as exc:
        return _recusa(str(exc))


def consultar_evidencia_github(run_id: int, *, token: str = "", timeout: int = 8,
                              leitor: Callable[[str], Any] | None = None) -> tuple[Any, Any, Any]:
    """Consulta SOMENTE três caminhos fixos na API oficial do GitHub."""
    if type(run_id) is not int or run_id <= 0:
        raise RecusaDeAutorizacao("run_id_invalido")
    prefix = f"https://api.github.com/repos/{REPOSITORIO}"
    paths = (
        f"/actions/runs/{run_id}",
        f"/actions/runs/{run_id}/approvals",
        f"/environments/{AMBIENTE}",
    )
    if leitor is None:
        if not isinstance(token, str) or len(token) > 4096:
            raise RecusaDeAutorizacao("token_invalido")
        opener = build_opener(_SemRedirecionamento(), HTTPSHandler(context=ssl.create_default_context()))
        def leitor(url: str) -> Any:
            headers = {"Accept": "application/vnd.github+json",
                       "X-GitHub-Api-Version": "2026-03-10"}
            if token:
                headers["Authorization"] = "Bearer " + token
            req = Request(url, headers=headers, method="GET")
            try:
                with opener.open(req, timeout=timeout) as resp:
                    if resp.status != 200:
                        raise RecusaDeAutorizacao("github_status_invalido")
                    raw = resp.read(131073)
            except (HTTPError, URLError, TimeoutError, OSError) as exc:
                raise RecusaDeAutorizacao("github_indisponivel") from exc
            if len(raw) > 131072:
                raise RecusaDeAutorizacao("github_resposta_grande")
            try:
                return json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise RecusaDeAutorizacao("github_json_invalido") from exc
    return tuple(leitor(prefix + path) for path in paths)
