#!/usr/bin/env python3
"""Weekly Power BI, SQL Server and data-governance technical radar."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

USER_AGENT = "Operational-Rules-Data-Radar/1.0"
PREFIX = "[Data Radar]"
OUT = "artifacts/data-analytics-weekly-radar"
TIMEOUT = 15
RETRIES = 2
MIN_ITEMS, MAX_ITEMS = 3, 5

SOURCES = (
    {"key":"power-bi","name":"Microsoft Power BI Blog","trust":"official","product":"power_bi",
     "feed":"https://powerbi.microsoft.com/en-us/blog/feed/","page":"https://powerbi.microsoft.com/en-us/blog/","match":"/en-us/blog/"},
    {"key":"sql-server","name":"Microsoft SQL Server Blog","trust":"official","product":"sql_server",
     "feed":"https://techcommunity.microsoft.com/gxcuf89792/rss/board?board.id=SQLServer",
     "page":"https://techcommunity.microsoft.com/category/SQL-Server/blog/SQLServer","match":"/blog/sqlserver/"},
    {"key":"purview","name":"Microsoft Purview Blog","trust":"official","product":"governance",
     "page":"https://techcommunity.microsoft.com/category/microsoft-purview/blog/microsoft-purview-blog","match":"/blog/microsoft-purview-blog/"},
    {"key":"fabric","name":"Microsoft Fabric Blog","trust":"official","product":"power_bi",
     "page":"https://blog.fabric.microsoft.com/en-us/blog","match":"/en-us/blog/"},
    {"key":"sqlbi","name":"SQLBI","trust":"independent","product":"power_bi",
     "feed":"https://www.sqlbi.com/feed/","page":"https://www.sqlbi.com/articles/","match":"/articles/"},
)

AXES = {
    "arquitetura": ("architecture","arquitetura","semantic model","direct lake","lakehouse","warehouse","gateway","tabular","star schema","data model","capacity"),
    "qualidade": ("quality","qualidade","validation","validação","testing","lineage","linhagem","catalog","catálogo","governance","governança","schema","contract","purview"),
    "observabilidade": ("observability","observabilidade","monitoring","telemetry","performance","desempenho","query store","metrics","métricas","trace","diagnostic"),
    "automação": ("automation","automação","api","rest","pipeline","deployment","implantação","ci/cd","script","powershell","devops"),
}
DOMAINS = {
    "power_bi": ("power bi","fabric","dax","semantic model","tabular","direct lake","power query","pbir"),
    "sql_server": ("sql server","t-sql","sql database","query store","database engine","always on","availability group"),
    "governance": ("governance","governança","purview","lineage","linhagem","catalog","data quality","qualidade de dados","data policy"),
}

class RadarError(RuntimeError):
    pass

class Links(HTMLParser):
    def __init__(self, base: str, match: str):
        super().__init__(convert_charrefs=True); self.base=base; self.match=match.lower(); self.href=None; self.text=[]; self.items=[]
    def handle_starttag(self, tag, attrs):
        if tag.lower() != "a": return
        href = next((v for k,v in attrs if k.lower()=="href"), None)
        if href:
            url=urljoin(self.base,href)
            if self.match in url.lower(): self.href=url; self.text=[]
    def handle_data(self, data):
        if self.href: self.text.append(data)
    def handle_endtag(self, tag):
        if tag.lower()=="a" and self.href:
            title=clean(" ".join(self.text))
            if 18 <= len(title) <= 240: self.items.append((self.href,title))
            self.href=None; self.text=[]

def request(url: str, *, token: str="", method: str="GET", payload=None):
    data=None if payload is None else json.dumps(payload).encode()
    headers={"User-Agent":USER_AGENT,"Accept":"application/rss+xml, application/atom+xml, application/xml, text/html, application/json;q=0.9, */*;q=0.8"}
    if token:
        headers.update({"Authorization":f"Bearer {token}","X-GitHub-Api-Version":"2022-11-28"})
    last=None
    for attempt in range(RETRIES):
        try:
            with urlopen(Request(url,headers=headers,data=data,method=method),timeout=TIMEOUT) as r:
                return r.read().decode("utf-8",errors="replace")
        except HTTPError as exc:
            last=exc
            if exc.code not in {408,429,500,502,503,504}: raise RadarError(f"HTTP {exc.code} for {url}") from exc
        except (URLError,TimeoutError) as exc: last=exc
        if attempt+1 < RETRIES: time.sleep(2**attempt)
    raise RadarError(f"request failed after {RETRIES} attempts: {url}: {last}")

def gh(repo: str, suffix: str, token: str, *, method="GET", payload=None):
    raw=request(f"https://api.github.com/repos/{repo}/{suffix.lstrip('/')}",token=token,method=method,payload=payload)
    return json.loads(raw) if raw else {}

def clean(value: str) -> str:
    value=re.sub(r"<script.*?</script>|<style.*?</style>"," ",value or "",flags=re.I|re.S)
    value=re.sub(r"<[^>]+>"," ",value); return re.sub(r"\s+"," ",html.unescape(value)).strip()

def norm(url: str) -> str:
    p=urlsplit((url or "").strip()); path=re.sub(r"/+","/",p.path or "/")
    if path != "/": path=path.rstrip("/")
    return urlunsplit(((p.scheme or "https").lower(),p.netloc.lower(),path,"",""))

def date(value: str):
    if not value: return None
    try:
        dt=parsedate_to_datetime(value.strip())
    except (TypeError,ValueError,OverflowError):
        try: dt=datetime.fromisoformat(value.strip().replace("Z","+00:00"))
        except ValueError: return None
    if dt.tzinfo is None: dt=dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()

def feed_items(raw: str, source: dict):
    try: root=ET.fromstring(raw)
    except ET.ParseError as exc: raise RadarError(f"invalid feed XML for {source['name']}: {exc}") from exc
    out=[]
    for entry in [e for e in root.iter() if e.tag.rsplit("}",1)[-1].lower() in {"item","entry"}][:30]:
        vals={}; link=""
        for c in list(entry):
            key=c.tag.rsplit("}",1)[-1].lower(); text=clean(c.text or ""); vals.setdefault(key,[]).append(text)
            if key=="link": link=c.attrib.get("href") or text or link
        title=next((x for x in vals.get("title",[]) if x),"")
        if not link: link=next((x for x in vals.get("guid",[]) if x.startswith("http")),"")
        if not title or not link: continue
        summary=next((x for k in ("description","summary","content") for x in vals.get(k,[]) if x),"")
        raw_date=next((x for k in ("pubdate","published","updated","date") for x in vals.get(k,[]) if x),"")
        out.append({"title":title[:300],"url":norm(link),"source":source["name"],"trust":source["trust"],"product":source["product"],"published":date(raw_date),"summary":summary[:1200]})
    return out

def html_items(raw: str, source: dict):
    p=Links(source["page"],source["match"]); p.feed(raw); seen=set(); out=[]; root=norm(source["page"])
    for href,title in p.items:
        url=norm(href)
        if url==root or url in seen: continue
        seen.add(url); out.append({"title":title[:300],"url":url,"source":source["name"],"trust":source["trust"],"product":source["product"],"published":None,"summary":""})
        if len(out)>=12: break
    return out

def collect(source: dict, getter=request):
    items=[]; errors=[]
    if source.get("feed"):
        try: items=feed_items(getter(source["feed"]),source)
        except RadarError as exc: errors.append(str(exc))
    if len(items)<2 and source.get("page"):
        try:
            merged={x["url"]:x for x in items}
            for x in html_items(getter(source["page"]),source): merged.setdefault(x["url"],x)
            items=list(merged.values())
        except RadarError as exc: errors.append(str(exc))
    return items,{"source":source["name"],"ok":bool(items),"candidate_count":len(items),"errors":errors}

def classify(item: dict, now=None):
    now=now or datetime.now(timezone.utc); text=f"{item['title']} {item['summary']}".lower()
    axes=[a for a,words in AXES.items() if any(w in text for w in words)]
    domains=[d for d,words in DOMAINS.items() if any(w in text for w in words)]
    score=4 if item["trust"]=="official" else 2
    score += 4 if domains else 2
    score += min(6,2*len(axes))
    if not axes and not domains: score=0
    if item["published"]:
        try:
            age=max(0,(now-datetime.fromisoformat(item["published"])).total_seconds()/86400)
            if age<=14: score+=3
            elif age<=45: score+=1
            elif age>180: score-=3
        except ValueError: pass
    if re.search(r"\b(preview|prévia|roadmap|announcement|anúncio)\b",text): score-=1
    return {**item,"score":score,"axes":axes,"domains":domains}

def history_urls(issues):
    out=set()
    for issue in issues:
        if str(issue.get("title","")).startswith(PREFIX):
            out.update(norm(u.rstrip(".,;")) for u in re.findall(r"https?://[^\s)>\]]+",str(issue.get("body",""))))
    return out

def select(items, seen):
    unique={}
    for raw in items:
        if not raw.get("url") or raw["url"] in seen: continue
        item=classify(raw)
        if item["score"]<7: continue
        if raw["url"] not in unique or item["score"]>unique[raw["url"]]["score"]: unique[raw["url"]]=item
    ranked=sorted(unique.values(),key=lambda x:(x["score"],x.get("published") or "",x["title"].lower()),reverse=True)
    chosen=[]
    for product in ("power_bi","sql_server","governance"):
        hit=next((x for x in ranked if x not in chosen and (x["product"]==product or product in x["domains"])),None)
        if hit: chosen.append(hit)
    for item in ranked:
        if item not in chosen: chosen.append(item)
        if len(chosen)>=MAX_ITEMS: break
    if len(chosen)<MIN_ITEMS: raise RadarError(f"insufficient unseen relevant items: {len(chosen)} selected; minimum is {MIN_ITEMS}")
    return chosen[:MAX_ITEMS]

def limitation(item):
    text=f"{item['title']} {item['summary']}".lower(); parts=[]
    if item["trust"]!="official": parts.append("análise externa; confirmar detalhes em fonte oficial")
    if not item["published"]: parts.append("a listagem não expôs data publicável")
    if re.search(r"\b(preview|prévia|roadmap)\b",text): parts.append("prévia/roadmap pode mudar antes da disponibilidade geral")
    return "; ".join(parts) or "resumo usa título/metadados; validar detalhes antes de mudança técnica"

def recommendation(item):
    if item["score"]>=10 or len(item["axes"])>=2:
        return f"Vale testar: validar em ambiente controlado o impacto em {', '.join(item['axes']) or 'impacto técnico'}."
    return "Ignorar por enquanto: manter no radar até existir evidência mais forte de impacto prático."

def week(now=None):
    local=(now or datetime.now(timezone.utc)).astimezone(ZoneInfo("America/Sao_Paulo")); year,num,_=local.isocalendar()
    return f"{year}-W{num:02d}",local.date().isoformat()

def issue_title(now=None): return f"{PREFIX} {week(now)[0]} — Power BI, SQL Server e governança de dados"

def render(items,statuses,sha,run_id,cid,now=None):
    key,day=week(now); lines=[f"# Radar técnico de dados — {key}","",f"- Data: **{day}** (America/Sao_Paulo)",f"- SHA: `{sha or 'indisponível'}`",f"- Run: `{run_id or 'indisponível'}`",f"- Correlation ID: `{cid}`",f"- Itens: **{len(items)}**",""]
    for i,item in enumerate(items,1):
        evidence=clean(item["summary"])[:500] if item["summary"] else f"A fonte listou “{item['title']}” entre os conteúdos atuais."
        lines += [f"## {i}. {item['title']}",f"- Fonte: **{item['source']}** ({'oficial' if item['trust']=='official' else 'independente'})",f"- Publicação: {(item['published'] or 'não informada')[:10]}",f"- Link: {item['url']}",f"- Foco: {', '.join(item['axes']) or 'relevância geral'}",f"- Evidência: {evidence}",f"- Limitação: {limitation(item)}.",f"- Decisão sugerida: **{recommendation(item)}**",""]
    ok=sum(1 for s in statuses if s["ok"]); lines += ["## Qualidade da coleta",f"- Fontes válidas: **{ok}/{len(statuses)}**.","- Seleção determinística, sem LLM pago, com deduplicação por URL.","- Menos de 3 itens inéditos e relevantes bloqueia a publicação.","","### Fontes"]
    for s in statuses:
        detail="OK" if s["ok"] else "falhou"
        if s["errors"]: detail+=f" — {s['errors'][-1][:200]}"
        lines.append(f"- {s['source']}: {detail}; candidatos={s['candidate_count']}")
    return "\n".join(lines)+"\n"

def issue_list(repo,token):
    data=gh(repo,"issues?state=all&per_page=100&sort=updated&direction=desc",token)
    if not isinstance(data,list): raise RadarError("unexpected GitHub issue-list response")
    return [x for x in data if isinstance(x,dict) and "pull_request" not in x]

def write_artifacts(output,report,body):
    output.mkdir(parents=True,exist_ok=True); (output/"report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2,sort_keys=True)+"\n",encoding="utf-8"); (output/"report.md").write_text(body,encoding="utf-8")

def run(repo,token,*,dry_run=False,getter=request,now=None,output=None):
    now=now or datetime.now(timezone.utc); output=output or Path(OUT); issues=issue_list(repo,token) if token else []; title=issue_title(now)
    existing=next((x for x in issues if x.get("title")==title),None)
    if existing and not dry_run:
        body=str(existing.get("body") or ""); report={"schema_version":"1.0.0","generated_at":datetime.now(timezone.utc).isoformat(),"repo":repo,"title":title,"dry_run":False,"replay":True,"issue_url":str(existing.get("html_url") or ""),"selected_count":len(re.findall(r"(?m)^## \d+\.",body)),"selected":[],"sources":[],"correlation_id":hashlib.sha256(f"{repo}|{title}|replay".encode()).hexdigest()[:24],"head_sha":os.getenv("GITHUB_SHA",""),"run_id":os.getenv("GITHUB_RUN_ID","")}
        write_artifacts(output,report,body or f"# {title}\n"); return report
    candidates=[]; statuses=[]
    for source in SOURCES:
        items,status=collect(source,getter); candidates += items; statuses.append(status)
    chosen=select(candidates,history_urls(issues)); cid=hashlib.sha256(f"{repo}|{title}|{'|'.join(x['url'] for x in chosen)}".encode()).hexdigest()[:24]
    body=render(chosen,statuses,os.getenv("GITHUB_SHA",""),os.getenv("GITHUB_RUN_ID",""),cid,now)
    issue_url=None
    if existing: issue_url=str(existing.get("html_url") or "")
    elif not dry_run:
        if not token: raise RadarError("GH_TOKEN/GITHUB_TOKEN is required to publish")
        issue_url=str(gh(repo,"issues",token,method="POST",payload={"title":title,"body":body}).get("html_url") or "")
    report={"schema_version":"1.0.0","generated_at":datetime.now(timezone.utc).isoformat(),"repo":repo,"title":title,"dry_run":dry_run,"replay":bool(existing),"issue_url":issue_url,"selected_count":len(chosen),"selected":[{**x,"limitation":limitation(x),"recommendation":recommendation(x)} for x in chosen],"sources":statuses,"correlation_id":cid,"head_sha":os.getenv("GITHUB_SHA",""),"run_id":os.getenv("GITHUB_RUN_ID","")}
    write_artifacts(output,report,body); return report

def main():
    p=argparse.ArgumentParser(); p.add_argument("--repo",default=os.getenv("GITHUB_REPOSITORY","")); p.add_argument("--dry-run",action="store_true"); p.add_argument("--output-dir",default=OUT); a=p.parse_args(); token=os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN") or ""; output=Path(a.output_dir)
    if not a.repo or "/" not in a.repo: print("RADAR_BLOCKED: invalid repo",file=sys.stderr); return 2
    try: result=run(a.repo,token,dry_run=a.dry_run,output=output)
    except (RadarError,json.JSONDecodeError,ValueError) as exc:
        output.mkdir(parents=True,exist_ok=True); (output/"report.json").write_text(json.dumps({"status":"blocked","repo":a.repo,"head_sha":os.getenv("GITHUB_SHA",""),"run_id":os.getenv("GITHUB_RUN_ID",""),"reason":str(exc)},ensure_ascii=False,indent=2)+"\n",encoding="utf-8"); print(f"RADAR_BLOCKED: {exc}",file=sys.stderr); return 1
    print(json.dumps({k:result[k] for k in ("selected_count","issue_url","replay","dry_run","correlation_id")},ensure_ascii=False,sort_keys=True)); return 0

if __name__ == "__main__": raise SystemExit(main())
