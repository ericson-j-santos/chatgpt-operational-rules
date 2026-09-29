#!/usr/bin/env python3
"""Monitor official Microsoft Power Platform ALM changes with low-noise alerts.

State is kept in one closed GitHub issue. The first run establishes a silent
baseline. Later runs create a new issue only when a material ALM change is
confirmed in an official Microsoft source.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

STATE_TITLE = "[monitor-state] Microsoft Power Platform ALM official sources"
STATE_BEGIN = "<!-- POWERPLATFORM_ALM_MONITOR_STATE"
STATE_END = "POWERPLATFORM_ALM_MONITOR_STATE -->"
ROADMAP_API = "https://www.microsoft.com/releasecommunications/api/v1/m365"
USER_AGENT = "ReqSys-PowerPlatform-ALM-Monitor/1.0"

DOC_SOURCES = (
    {
        "key": "pipelines",
        "repo": "MicrosoftDocs/power-platform",
        "path": "power-platform/alm/pipelines.md",
    },
    {
        "key": "run-pipeline",
        "repo": "MicrosoftDocs/power-platform",
        "path": "power-platform/alm/run-pipeline.md",
    },
    {
        "key": "custom-host-pipelines",
        "repo": "MicrosoftDocs/power-platform",
        "path": "power-platform/alm/custom-host-pipelines.md",
    },
    {
        "key": "solution-concepts",
        "repo": "MicrosoftDocs/power-platform",
        "path": "power-platform/alm/solution-concepts-alm.md",
    },
    {
        "key": "deployment-settings",
        "repo": "MicrosoftDocs/power-platform",
        "path": "power-platform/alm/conn-ref-env-variables-build-tools.md",
    },
    {
        "key": "environment-variables",
        "repo": "MicrosoftDocs/powerapps-docs",
        "path": "powerapps-docs/maker/data-platform/EnvironmentVariables.md",
    },
    {
        "key": "connection-references",
        "repo": "MicrosoftDocs/powerapps-docs",
        "path": "powerapps-docs/maker/data-platform/create-connection-reference.md",
    },
)

SCOPE_RE = re.compile(
    r"(?i)(application lifecycle management|\balm\b|managed solution|"
    r"solution import|solution export|\bpipeline(?:s)?\b|environment variable|"
    r"connection reference|deployment settings|managed environment|"
    r"target environment|source environment|service principal)"
)
STRONG_IMPACT_RE = re.compile(
    r"(?i)(\brequired\b|\brequires?\b|\bmust\b|no longer|deprecated|"
    r"retir(?:e|ed|ing)|unsupported|not supported|starting\s+[A-Z]?[a-z]+\s+\d{4}|"
    r"\bdefault\b|automatic(?:ally)?|\bcannot\b|\bcan't\b|\bwon't\b|"
    r"\blicen[cs]e\b|\bpermission\b|\blimitation\b|\bupgrade\b|"
    r"\bdelete\b|\bremoved?\b|\bblocked\b|breaking|general availability|"
    r"public preview|managed environment)"
)
NUMERIC_LIMIT_RE = re.compile(r"(?i)(limit|maximum|minimum|up to|hours?|days?).*\b\d+\b")
PRODUCT_NAMES = {
    "microsoft power platform governance and administration",
    "power apps",
    "power automate",
    "microsoft dataverse",
}


class MonitorError(RuntimeError):
    pass


def _request(
    url: str,
    *,
    token: str | None = None,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    retries: int = 3,
) -> bytes:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {
        "Accept": "application/vnd.github+json, application/json",
        "User-Agent": USER_AGENT,
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-GitHub-Api-Version"] = "2022-11-28"

    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = Request(url, data=data, headers=headers, method=method)
            with urlopen(req, timeout=30) as response:
                return response.read()
        except HTTPError as exc:
            last = exc
            if exc.code not in {408, 429, 500, 502, 503, 504}:
                raise MonitorError(f"HTTP {exc.code} for {url}") from exc
        except (URLError, TimeoutError) as exc:
            last = exc
        if attempt + 1 < retries:
            time.sleep(2**attempt)
    raise MonitorError(f"request failed after {retries} attempts: {url}: {last}")


def request_json(url: str, *, token: str | None = None, method: str = "GET",
                 payload: dict[str, Any] | None = None) -> Any:
    return json.loads(_request(url, token=token, method=method, payload=payload).decode("utf-8"))


def request_text(url: str, *, token: str | None = None) -> str:
    return _request(url, token=token).decode("utf-8")


def github_api(repo: str, suffix: str, token: str, *, method: str = "GET",
               payload: dict[str, Any] | None = None) -> Any:
    return request_json(
        f"https://api.github.com/repos/{repo}/{suffix.lstrip('/')}",
        token=token,
        method=method,
        payload=payload,
    )


def normalize_markdown(text: str) -> str:
    text = text.replace("\r\n", "\n")
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            text = text[end + 5 :]
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    lines = [re.sub(r"[ \t]+$", "", line) for line in text.splitlines()]
    return "\n".join(lines).strip() + "\n"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def changed_content_lines(before: str, after: str) -> list[str]:
    before_lines = normalize_markdown(before).splitlines()
    after_lines = normalize_markdown(after).splitlines()
    result: list[str] = []
    for line in difflib.unified_diff(before_lines, after_lines, lineterm=""):
        if line.startswith(("+++", "---", "@@")):
            continue
        if line.startswith(("+", "-")):
            value = line[1:].strip()
            if value and not value.startswith(":::image"):
                result.append(value)
    return result


def is_material_doc_change(lines: list[str]) -> bool:
    if not lines:
        return False
    for line in lines:
        if SCOPE_RE.search(line) and (STRONG_IMPACT_RE.search(line) or NUMERIC_LIMIT_RE.search(line)):
            return True
    joined = "\n".join(lines)
    return bool(SCOPE_RE.search(joined) and STRONG_IMPACT_RE.search(joined))


def latest_file_commit(repo: str, path: str, token: str) -> str:
    encoded = quote(path, safe="/")
    data = request_json(
        f"https://api.github.com/repos/{repo}/commits?path={encoded}&per_page=1",
        token=token,
    )
    if not isinstance(data, list) or not data or not data[0].get("sha"):
        raise MonitorError(f"no upstream commit found for {repo}:{path}")
    return str(data[0]["sha"])


def raw_github_file(repo: str, commit: str, path: str, token: str) -> str:
    del token  # MicrosoftDocs sources are public; avoid forwarding repo credentials.
    return request_text(
        f"https://raw.githubusercontent.com/{repo}/{commit}/{path}",
    )


def strip_html(value: str) -> str:
    value = re.sub(r"<br\s*/?>", "\n", value or "", flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def roadmap_products(item: dict[str, Any]) -> set[str]:
    products = item.get("tagsContainer", {}).get("products", [])
    return {
        str(entry.get("tagName", "")).strip().lower()
        for entry in products
        if isinstance(entry, dict)
    }


def roadmap_is_in_scope(item: dict[str, Any]) -> bool:
    products = roadmap_products(item)
    if not (products & PRODUCT_NAMES):
        return False
    text = f"{item.get('title', '')} {strip_html(str(item.get('description', '')))}"
    return bool(SCOPE_RE.search(text))


def canonical_roadmap_item(item: dict[str, Any]) -> dict[str, Any]:
    description = strip_html(str(item.get("description", "")))
    release_phases = sorted(
        str(v.get("tagName", ""))
        for v in item.get("tagsContainer", {}).get("releasePhase", [])
        if isinstance(v, dict) and v.get("tagName")
    )
    return {
        "title": str(item.get("title", "")).strip(),
        "description_hash": sha256_text(description),
        "description_excerpt": description[:700],
        "status": str(item.get("status", "")).strip(),
        "publicRoadmapStatus": str(item.get("publicRoadmapStatus", "")).strip(),
        "publicDisclosureAvailabilityDate": str(item.get("publicDisclosureAvailabilityDate", "")).strip(),
        "publicPreviewDate": str(item.get("publicPreviewDate", "")).strip(),
        "releasePhase": release_phases,
    }


def fetch_roadmap_state() -> dict[str, dict[str, Any]]:
    data = request_json(ROADMAP_API)
    if not isinstance(data, list):
        raise MonitorError("unexpected Microsoft roadmap payload")
    scoped: dict[str, dict[str, Any]] = {}
    for item in data:
        if not isinstance(item, dict) or item.get("id") is None:
            continue
        if roadmap_is_in_scope(item):
            scoped[str(item["id"])] = canonical_roadmap_item(item)
    return scoped


def infer_subject(text: str) -> str:
    low = text.lower()
    if "connection reference" in low:
        return "referências de conexão"
    if "environment variable" in low:
        return "variáveis de ambiente"
    if "managed solution" in low:
        return "soluções gerenciadas"
    if "managed environment" in low:
        return "ambientes gerenciados"
    if "pipeline" in low:
        return "pipelines"
    if "deployment" in low or "target environment" in low:
        return "implantação entre ambientes"
    return "ALM do Power Platform"


def infer_risk(text: str) -> str:
    if re.search(r"(?i)(no longer|deprecated|retir|unsupported|not supported|cannot|can't|must|required|breaking|blocked)", text):
        return "alto"
    return "médio"


def adaptation_for(subject: str) -> str:
    if subject == "referências de conexão":
        return (
            "Não alterar PROD automaticamente. Revalidar o mapeamento de connection references "
            "e ownership/share das conexões em DEV/TEST, com leitura antes/depois e replay sem "
            "duplicidade; promover somente o mesmo artefato managed validado."
        )
    if subject == "variáveis de ambiente":
        return (
            "Manter valores fora do artefato quando aplicável e revalidar o deployment settings "
            "por ambiente em DEV/TEST, incluindo ausência de valor indevido e replay idempotente."
        )
    if subject in {"pipelines", "ambientes gerenciados"}:
        return (
            "Revalidar preflight, requisitos de ambiente/host/stage e o caminho DEV→TEST com o "
            "mesmo artefato; ajustar somente o gate afetado e manter PROD sem mudança até prova."
        )
    if subject == "soluções gerenciadas":
        return (
            "Revalidar export managed, import/upgrade e dependências em DEV/TEST usando o mesmo "
            "ZIP/SHA; não converter para unmanaged nem promover PROD para contornar a mudança."
        )
    return (
        "Adicionar ou ajustar um teste de contrato no ALM e reexecutar o caminho DEV→TEST no "
        "artefato/SHA atual antes de qualquer promoção. A ação é reversível e não requer custo."
    )


def effect_for(subject: str) -> str:
    mapping = {
        "referências de conexão": "Pode alterar o binding, a validação ou o ownership das conexões durante importação e ativação de flows.",
        "variáveis de ambiente": "Pode alterar como valores específicos de cada ambiente são fornecidos, preservados ou validados no deploy.",
        "soluções gerenciadas": "Pode alterar empacotamento, importação, upgrade ou manutenção do artefato promovido a TEST/PROD.",
        "ambientes gerenciados": "Pode introduzir ou alterar pré-requisitos para ambientes-alvo usados por pipelines.",
        "pipelines": "Pode alterar pré-validação, ordem de estágios, identidade de implantação ou requisitos de execução.",
        "implantação entre ambientes": "Pode alterar o contrato DEV→TEST→PROD ou as pré-condições de importação.",
    }
    return mapping.get(subject, "Pode exigir ajuste no contrato de ALM ou nos testes de implantação do ReqSys.")


def doc_event(source: dict[str, str], old_commit: str, new_commit: str,
              lines: list[str]) -> dict[str, Any]:
    text = "\n".join(lines)
    subject = infer_subject(text)
    return {
        "kind": "documentation",
        "source": source["key"],
        "url": f"https://github.com/{source['repo']}/compare/{old_commit}...{new_commit}",
        "evidence": f"{source['repo']}:{source['path']} {old_commit[:12]} → {new_commit[:12]}",
        "subject": subject,
        "risk": infer_risk(text),
        "effect": effect_for(subject),
        "adaptation": adaptation_for(subject),
        "details": lines[:20],
    }


def roadmap_events(previous: dict[str, dict[str, Any]],
                   current: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for item_id in sorted(set(previous) | set(current), key=lambda x: int(x)):
        before = previous.get(item_id)
        after = current.get(item_id)
        if before == after:
            continue
        if before is None:
            change = "novo item oficial no roadmap"
            evidence_text = json.dumps(after, ensure_ascii=False, sort_keys=True)
            title = after.get("title", "")
        elif after is None:
            change = "item removido do roadmap oficial"
            evidence_text = json.dumps(before, ensure_ascii=False, sort_keys=True)
            title = before.get("title", "")
        else:
            changed = [k for k in after if before.get(k) != after.get(k)]
            change = "campos alterados: " + ", ".join(changed)
            evidence_text = json.dumps({"before": before, "after": after}, ensure_ascii=False, sort_keys=True)
            title = after.get("title", before.get("title", ""))
        subject = infer_subject(f"{title} {evidence_text}")
        events.append(
            {
                "kind": "roadmap",
                "source": f"AI at Work Roadmap #{item_id}",
                "url": f"https://www.microsoft.com/en-us/microsoft-365/roadmap?id={item_id}",
                "evidence": change,
                "subject": subject,
                "risk": infer_risk(evidence_text),
                "effect": effect_for(subject),
                "adaptation": adaptation_for(subject),
                "details": [title, change],
            }
        )
    return events


def issue_list(repo: str, token: str) -> list[dict[str, Any]]:
    data = github_api(repo, "issues?state=all&per_page=100&sort=updated&direction=desc", token)
    return [item for item in data if isinstance(item, dict)]


def find_issue(repo: str, token: str, title: str) -> dict[str, Any] | None:
    for issue in issue_list(repo, token):
        if issue.get("title") == title and "pull_request" not in issue:
            return issue
    return None


def parse_state(body: str) -> dict[str, Any] | None:
    pattern = re.compile(re.escape(STATE_BEGIN) + r"\s*(.*?)\s*" + re.escape(STATE_END), re.S)
    match = pattern.search(body or "")
    if not match:
        return None
    return json.loads(match.group(1))


def state_body(state: dict[str, Any]) -> str:
    raw = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True)
    return (
        "# Estado interno — monitor oficial Power Platform ALM\n\n"
        "Issue fechado e mantido automaticamente. Não representa alerta. "
        "Alertas materiais são criados em issues separadas.\n\n"
        f"{STATE_BEGIN}\n{raw}\n{STATE_END}\n"
    )


def save_state(repo: str, token: str, issue: dict[str, Any] | None,
               state: dict[str, Any]) -> dict[str, Any]:
    body = state_body(state)
    if issue is None:
        created = github_api(repo, "issues", token, method="POST", payload={"title": STATE_TITLE, "body": body})
        number = created["number"]
        return github_api(
            repo,
            f"issues/{number}",
            token,
            method="PATCH",
            payload={"state": "closed", "body": body},
        )
    return github_api(
        repo,
        f"issues/{issue['number']}",
        token,
        method="PATCH",
        payload={"state": "closed", "body": body},
    )


def alert_title(events: list[dict[str, Any]]) -> str:
    fingerprint = sha256_text(json.dumps(events, ensure_ascii=False, sort_keys=True))[:12]
    return f"[ALM Monitor] Mudança oficial material Power Platform - {fingerprint}"


def alert_body(events: list[dict[str, Any]], head_sha: str) -> str:
    parts = [
        "# Mudança oficial material — Microsoft Power Platform ALM",
        "",
        f"Monitor SHA: `{head_sha}`" if head_sha else "Monitor SHA: indisponível",
        "",
    ]
    for index, event in enumerate(events, 1):
        parts.extend(
            [
                f"## {index}. Mudança confirmada — {event['subject']}",
                f"- Fonte oficial: {event['url']}",
                f"- Evidência: {event['evidence']}",
                f"- Risco: **{event['risk']}**",
                f"- Efeito provável no ReqSys: {event['effect']}",
                f"- Menor adaptação segura, idempotente e sem custo: {event['adaptation']}",
            ]
        )
        details = [str(v)[:700] for v in event.get("details", []) if str(v).strip()]
        if details:
            parts.append("- Alterações relevantes observadas:")
            parts.extend(f"  - {value}" for value in details[:20])
        parts.append("")
    parts.extend(
        [
            "### Limite da evidência",
            "A mudança está confirmada na fonte oficial. Para itens de roadmap, datas e escopo "
            "continuam sujeitos a alteração pela Microsoft; isso não prova que a funcionalidade "
            "já esteja disponível no tenant do ReqSys.",
        ]
    )
    return "\n".join(parts) + "\n"


def create_alert_if_new(repo: str, token: str, events: list[dict[str, Any]],
                        head_sha: str) -> str | None:
    if not events:
        return None
    title = alert_title(events)
    existing = find_issue(repo, token, title)
    if existing:
        return str(existing.get("html_url") or "")
    created = github_api(
        repo,
        "issues",
        token,
        method="POST",
        payload={"title": title, "body": alert_body(events, head_sha)},
    )
    return str(created.get("html_url") or "")


def collect_docs(token: str, previous: dict[str, Any] | None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    state: dict[str, Any] = {}
    events: list[dict[str, Any]] = []
    prev_docs = (previous or {}).get("docs", {})
    for source in DOC_SOURCES:
        commit = latest_file_commit(source["repo"], source["path"], token)
        current_text = raw_github_file(source["repo"], commit, source["path"], token)
        current_norm = normalize_markdown(current_text)
        state[source["key"]] = {
            "repo": source["repo"],
            "path": source["path"],
            "commit": commit,
            "hash": sha256_text(current_norm),
        }
        old = prev_docs.get(source["key"])
        if not old or old.get("commit") == commit:
            continue
        old_commit = str(old["commit"])
        old_text = raw_github_file(source["repo"], old_commit, source["path"], token)
        lines = changed_content_lines(old_text, current_text)
        if is_material_doc_change(lines):
            events.append(doc_event(source, old_commit, commit, lines))
    return state, events


def build_state(docs: dict[str, Any], roadmap: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "docs": docs,
        "roadmap": roadmap,
    }


def write_summary(events: list[dict[str, Any]], baseline: bool, alert_url: str | None) -> None:
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = [
        "## Microsoft Power Platform ALM official monitor",
        "",
        f"- Baseline: {'criada' if baseline else 'existente'}",
        f"- Mudanças materiais: {len(events)}",
        f"- Alerta: {alert_url or 'nenhum'}",
    ]
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def run(repo: str, token: str, head_sha: str) -> dict[str, Any]:
    issue = find_issue(repo, token, STATE_TITLE)
    previous = parse_state(str(issue.get("body", ""))) if issue else None

    docs, doc_events = collect_docs(token, previous)
    roadmap = fetch_roadmap_state()
    baseline = previous is None
    events = [] if baseline else doc_events + roadmap_events(previous.get("roadmap", {}), roadmap)

    new_state = build_state(docs, roadmap)
    alert_url = create_alert_if_new(repo, token, events, head_sha)
    save_state(repo, token, issue, new_state)
    write_summary(events, baseline, alert_url)
    return {
        "baseline": baseline,
        "material_events": len(events),
        "alert_url": alert_url,
        "docs": len(docs),
        "roadmap_items": len(roadmap),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=os.getenv("GITHUB_REPOSITORY", ""))
    args = parser.parse_args()
    token = os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN") or ""
    if not args.repo or "/" not in args.repo:
        print("MONITOR_BLOCKED: invalid --repo/GITHUB_REPOSITORY", file=sys.stderr)
        return 2
    if not token:
        print("MONITOR_BLOCKED: GH_TOKEN/GITHUB_TOKEN is required", file=sys.stderr)
        return 2
    try:
        result = run(args.repo, token, os.getenv("GITHUB_SHA", ""))
    except (MonitorError, KeyError, ValueError, json.JSONDecodeError) as exc:
        print(f"MONITOR_BLOCKED: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
