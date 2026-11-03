#!/usr/bin/env python3
"""Conservative, no-cost official AI agent/RAG security change monitor.

Sources are treated as data, never instructions. Only new dated authoritative
items with explicit AI-agent/RAG security controls can produce an issue alert.
This is an advisory watch, not a deployment or vulnerability scan.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

REPO = "ericson-j-santos/chatgpt-operational-rules"
PREFIX = "[AI/RAG Security]"
BASELINE = datetime(2026, 11, 3, 3, 0, tzinfo=timezone.utc)
UA = "ReqSys-AI-RAG-Official-Security-Watch/1.0"
SOURCES = (
    ("CISA", "rss", "https://www.cisa.gov/cybersecurity-advisories/all.xml"),
    ("NIST", "rss", "https://www.nist.gov/news-events/news/rss.xml"),
    ("OWASP GenAI", "release", "https://api.github.com/repos/GenAI-Security-Project/GenAI-LLM-Top10/releases?per_page=30"),
    ("Ollama", "advisory", "https://api.github.com/repos/ollama/ollama/security-advisories?per_page=30"),
    ("MCP", "advisory", "https://api.github.com/repos/modelcontextprotocol/servers/security-advisories?per_page=30"),
)
CONTEXT = re.compile(r"\b(?:llm|large language model|generative ai|genai|agentic|ai agent|prompt injection|rag|retrieval.augmented|model context protocol|mcp server|ollama|copilot studio)\b", re.I)
AXES = {
    "injeção de instruções": re.compile(r"prompt.inject|instruction.inject|jailbreak|retrieval.poison|data.poison|untrusted.content", re.I),
    "acesso a ferramentas": re.compile(r"tool.call|tool.access|excessive.agency|authorization|privilege|permission|identity|agent.tool|mcp.server", re.I),
    "exposição de dados": re.compile(r"exfiltrat|data.leak|information.disclosure|sensitive.data|secret|credential|context.exposure", re.I),
    "auditoria": re.compile(r"audit|telemetr|logging|traceabil|observabil|correlation.id|trace.id", re.I),
}
MATERIAL = re.compile(r"\b(?:security|vulnerabilit|advisory|exploit|mitigat|patch|fix|risk|threat|attack|guidance|protection|CVE-\d{4}-\d+|GHSA-[a-z0-9-]+)\b", re.I)
ADAPT = {
    "injeção de instruções": "Verificar separação entre dados recuperados e instruções; adicionar teste negativo com conteúdo RAG hostil, sem liberar ferramentas.",
    "acesso a ferramentas": "Revalidar autorização por operação, menor privilégio, aprovação humana e negação de chamada indevida em DEV.",
    "exposição de dados": "Revalidar escopo de acesso e redigir segredos/PII dos logs, com teste negativo de exfiltração em DEV.",
    "auditoria": "Revalidar logs sanitizados, correlation_id, rastreabilidade e leitura independente; não reduzir retenção ou gates.",
}

class MonitorBlocked(RuntimeError):
    pass

def safe(value: object, maxlen: int = 280) -> str:
    plain = html.unescape(re.sub(r"<[^>]*>", " ", str(value or "")))
    plain = re.sub(r"\s+", " ", plain).strip()
    return plain.replace("@", "＠").replace("<", "‹").replace(">", "›")[:maxlen]

def canonical(url: str) -> str:
    p = urlsplit(url)
    host = (p.hostname or "").lower()
    if p.scheme != "https" or host not in {"github.com", "cisa.gov", "www.cisa.gov", "nist.gov", "www.nist.gov"} or p.username or p.password:
        raise MonitorBlocked("non-official or insecure source URL")
    return urlunsplit(("https", host, p.path.rstrip("/") or "/", "", ""))

def date(raw: str | None):
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = parsedate_to_datetime(str(raw))
        except (ValueError, TypeError, IndexError, OverflowError):
            return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)

def source_request(url: str, token: str = "") -> bytes:
    headers = {"User-Agent": UA, "Accept": "application/json, application/rss+xml, application/atom+xml, application/xml"}
    if url.startswith("https://api.github.com/") and token:
        headers["Authorization"] = "Bearer " + token
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    try:
        with urlopen(Request(url, headers=headers), timeout=18) as response:
            data = response.read(2_000_001)
            if len(data) > 2_000_000:
                raise MonitorBlocked("source payload exceeds size limit")
            return data
    except (HTTPError, URLError, TimeoutError) as exc:
        raise MonitorBlocked("official source unavailable: " + url.split("?")[0]) from exc

def gh(repo: str, suffix: str, token: str, *, method: str = "GET", payload=None):
    if repo != REPO or not token:
        raise MonitorBlocked("repository/token preflight failed")
    if method not in {"GET", "POST"}:
        raise MonitorBlocked("unsupported GitHub mutation")
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Accept": "application/vnd.github+json", "User-Agent": UA,
               "Authorization": "Bearer " + token, "X-GitHub-Api-Version": "2022-11-28"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    try:
        with urlopen(Request("https://api.github.com/repos/" + repo + "/" + suffix,
                             headers=headers, data=data, method=method), timeout=18) as response:
            return json.loads(response.read(2_000_001).decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        # POST has no automatic retry; reconcile uncertain result by read on next run.
        raise MonitorBlocked("GitHub operation failed: " + method + " " + suffix.split("?")[0]) from exc

def rss_items(raw: bytes, name: str):
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise MonitorBlocked("invalid official RSS/XML") from exc
    records = []
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] not in {"item", "entry"}:
            continue
        fields = {}
        for child in element:
            key = child.tag.rsplit("}", 1)[-1]
            val = child.attrib.get("href") if key == "link" else "".join(child.itertext())
            if val:
                fields[key] = val
        link = fields.get("link") or fields.get("guid") or ""
        if not link:
            continue
        try:
            url = canonical(link)
        except MonitorBlocked:
            continue
        records.append({"source": name, "url": url, "title": safe(fields.get("title")),
                        "description": safe(fields.get("description") or fields.get("summary"), 900),
                        "published": date(fields.get("pubDate") or fields.get("published")),
                        "kind": "guidance"})
        if len(records) >= 60:
            break
    return records

def release_items(raw: bytes, name: str):
    data = json.loads(raw)
    if not isinstance(data, list):
        raise MonitorBlocked("unexpected release API shape")
    out = []
    for item in data[:40]:
        if not isinstance(item, dict) or item.get("draft") or item.get("prerelease"):
            continue
        try:
            url = canonical(str(item.get("html_url") or ""))
        except MonitorBlocked:
            continue
        out.append({"source": name, "url": url, "title": safe(item.get("name") or item.get("tag_name")),
                    "description": safe(item.get("body"), 900),
                    "published": date(item.get("published_at")), "kind": "guidance"})
    return out

def advisory_items(raw: bytes, name: str):
    data = json.loads(raw)
    if not isinstance(data, list):
        raise MonitorBlocked("unexpected advisory API shape")
    out = []
    for item in data[:40]:
        if not isinstance(item, dict) or item.get("state") not in {"published", None}:
            continue
        try:
            url = canonical(str(item.get("html_url") or ""))
        except MonitorBlocked:
            continue
        out.append({"source": name, "url": url, "title": safe(item.get("summary") or item.get("ghsa_id")),
                    "description": safe(item.get("description"), 900),
                    "published": date(item.get("published_at")), "kind": "advisory",
                    "severity": safe(item.get("severity"), 20), "ghsa": safe(item.get("ghsa_id"), 40)})
    return out

def material(item: dict, now: datetime):
    published = item.get("published")
    if not isinstance(published, datetime) or not BASELINE <= published <= now:
        return ()
    text = safe(item.get("title"), 300) + " " + safe(item.get("description"), 900)
    if not CONTEXT.search(text) or not MATERIAL.search(text):
        return ()
    axes = tuple(name for name, pattern in AXES.items() if pattern.search(text))
    # A generic AI announcement or an advisory with no agent/RAG control is not an alert.
    return axes

def alert_title(item: dict) -> str:
    key = hashlib.sha256(item["url"].encode("utf-8")).hexdigest()[:16]
    return f"{PREFIX} {key} — {safe(item['title'], 100)}"

def report(item: dict, axes: tuple[str, ...], sha: str) -> str:
    published = item["published"].strftime("%Y-%m-%d %H:%M UTC")
    risk = item.get("severity") or "a confirmar; nenhuma exploração local comprovada"
    return "\n".join([
        "# Alerta oficial de segurança para agentes de IA/RAG", "",
        f"- Fonte oficial: {item['source']} — {item['url']}",
        f"- Publicação: {published}",
        f"- Tipo: {item['kind']}; severidade declarada: {risk}",
        f"- Controles afetados: {', '.join(axes)}",
        f"- Evidência textual oficial (resumo limitado): {safe(item['description'], 400)}",
        "- FATO: fonte oficial publicada e relacionada ao eixo acima.",
        "- HIPÓTESE: impacto em ReqSys, Llama/Ollama ou RAG exige confirmar dependência, versão, exposição e SHA no ambiente.",
        "- Risco: execução de instrução não confiável, privilégio excessivo, vazamento ou perda de trilha conforme eixo aplicável; não há incidente local comprovado.",
        "- Adaptação mínima segura (proposta; NÃO executada): " + " ".join(ADAPT[x] for x in axes),
        "- Próxima ação e responsável: mantenedor técnico — validar aplicabilidade, versão e controles positivos/negativos, E2E DEV e leitura independente antes de mudança.",
        f"- SHA do monitor: {sha}", "",
        "Somente pesquisa; nenhum deploy, permissão ou custo adicional foi autorizado.",
    ]) + "\n"

def existing_titles(repo: str, token: str):
    result = set()
    for page in range(1, 21):
        batch = gh(repo, f"issues?state=all&per_page=100&page={page}", token)
        if not isinstance(batch, list):
            raise MonitorBlocked("issue history malformed")
        for issue in batch:
            if "pull_request" not in issue and str(issue.get("title", "")).startswith(PREFIX):
                result.add(str(issue["title"]))
        if len(batch) < 100:
            return result
    raise MonitorBlocked("issue history truncated; cannot ensure deduplication")

def publication_guard():
    if (os.getenv("GITHUB_ACTIONS") != "true" or
        os.getenv("GITHUB_EVENT_NAME") not in {"schedule", "workflow_dispatch"} or
        os.getenv("GITHUB_RUN_ATTEMPT") != "1" or
        not re.fullmatch(r"[0-9a-f]{40}", os.getenv("GITHUB_SHA", "")) or
        not re.fullmatch(r"[0-9]+", os.getenv("GITHUB_RUN_ID", ""))):
        raise MonitorBlocked("publisher context not attested")

def collect(token: str = ""):
    records, checks = [], []
    for name, kind, url in SOURCES:
        try:
            raw = source_request(url, token)
            got = {"rss": rss_items, "release": release_items, "advisory": advisory_items}[kind](raw, name)
            records.extend(got)
            checks.append({"source": name, "status": "ok", "count": len(got)})
        except (MonitorBlocked, json.JSONDecodeError, ValueError) as exc:
            checks.append({"source": name, "status": "coverage_incomplete", "reason": safe(exc, 160)})
    if not any(c["status"] == "ok" for c in checks):
        raise MonitorBlocked("all official sources unavailable; no claim of clean security status")
    return records, checks

def execute(repo: str, token: str, now: datetime, *, dry_run: bool = False):
    if repo != REPO:
        raise MonitorBlocked("wrong destination repository")
    if not dry_run:
        publication_guard()
    records, checks = collect(token)
    seen = existing_titles(repo, token) if not dry_run else set()
    eligible = []
    for item in records:
        axes = material(item, now)
        if axes and alert_title(item) not in seen:
            eligible.append((item, axes))
    eligible.sort(key=lambda pair: pair[0]["published"])
    issues = []
    for item, axes in eligible:
        title = alert_title(item)
        if dry_run:
            continue
        body = report(item, axes, os.environ["GITHUB_SHA"])
        created = gh(repo, "issues", token, method="POST", payload={"title": title, "body": body})
        number = created.get("number")
        if type(number) is not int:
            raise MonitorBlocked("uncertain issue creation; do not retry")
        observed = gh(repo, f"issues/{number}", token)
        if observed.get("title") != title or observed.get("body") != body:
            raise MonitorBlocked("independent issue readback mismatch; do not retry")
        issues.append(str(observed.get("html_url")))
        seen.add(title)
    return {"sources": checks, "material_candidates": len(eligible),
            "issues_created": len(issues), "alert_urls": issues,
            "dry_run": dry_run, "coverage_complete": all(x["status"] == "ok" for x in checks)}

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=os.getenv("GITHUB_REPOSITORY", ""))
    parser.add_argument("--dry-run", action="store_true")
    opts = parser.parse_args()
    token = os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN") or ""
    if not token and not opts.dry_run:
        print("MONITOR_BLOCKED: missing repository token", file=sys.stderr)
        return 2
    try:
        result = execute(opts.repo, token, datetime.now(timezone.utc), dry_run=opts.dry_run)
    except (MonitorBlocked, ValueError, KeyError, TypeError) as exc:
        print("MONITOR_BLOCKED: " + safe(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
