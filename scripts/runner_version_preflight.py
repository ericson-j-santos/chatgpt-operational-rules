#!/usr/bin/env python3
"""Preflight fail-closed de versão para GitHub Actions self-hosted runners."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REGISTRATION_MINIMUM = (2, 329, 0)
VERSION_RE = re.compile(r"(?<!\d)(\d+)\.(\d+)\.(\d+)(?!\d)")
API_VERSION = "2026-03-10"


@dataclass(frozen=True, order=True)
class RunnerVersion:
    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, value: str) -> "RunnerVersion":
        match = VERSION_RE.search(value.strip())
        if not match:
            raise ValueError("runner_version_invalid")
        return cls(*(int(part) for part in match.groups()))

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def runner_root_from_environment() -> Path:
    temp = os.environ.get("RUNNER_TEMP", "").strip()
    if not temp:
        raise RuntimeError("runner_temp_missing")
    path = Path(temp)
    if len(path.parents) < 2:
        raise RuntimeError("runner_temp_unexpected")
    return path.parents[1]


def detect_runner_version(root: Path | None = None) -> RunnerVersion:
    root = root or runner_root_from_environment()
    candidates = [
        root / "bin" / "Runner.Listener.exe",
        root / "bin" / "Runner.Listener",
    ]
    executable = next((item for item in candidates if item.is_file()), None)
    if executable is None:
        raise RuntimeError("runner_listener_missing")
    completed = subprocess.run(
        [str(executable), "--version"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("runner_version_probe_failed")
    return RunnerVersion.parse((completed.stdout or "") + "\n" + (completed.stderr or ""))


def request_json(url: str, token: str | None = None, timeout: float = 10.0) -> tuple[str, dict[str, Any] | None]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "runner-version-preflight",
        "X-GitHub-Api-Version": API_VERSION,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403, 404}:
            return f"http_{exc.code}", None
        raise RuntimeError(f"github_api_http_{exc.code}") from exc
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        return f"unavailable:{type(exc).__name__}", None
    if not isinstance(payload, dict):
        return "invalid_payload", None
    return "ok", payload


def fetch_deprecation(repository: str, version: RunnerVersion, token: str | None) -> tuple[str, dict[str, Any] | None]:
    if not re.fullmatch(r"[^/\s]+/[^/\s]+", repository):
        raise ValueError("repository_invalid")
    owner, name = repository.split("/", 1)
    url = (
        f"https://api.github.com/repos/{owner}/{name}/actions/runners/"
        f"deprecations/{version}"
    )
    return request_json(url, token=token)


def fetch_latest_release() -> tuple[str, dict[str, Any] | None]:
    return request_json("https://api.github.com/repos/actions/runner/releases/latest")


def evaluate_policy(
    *,
    installed: RunnerVersion,
    deprecation: dict[str, Any] | None,
    deprecation_status: str,
    latest_release: dict[str, Any] | None,
    latest_status: str,
    now: datetime,
) -> dict[str, Any]:
    registration_supported = installed >= RunnerVersion(*REGISTRATION_MINIMUM)
    registration_deprecates_at = parse_timestamp(
        str(deprecation.get("registration_deprecates_at") or "")
        if deprecation else None
    )
    runtime_deprecates_at = parse_timestamp(
        str(deprecation.get("runtime_deprecates_at") or "")
        if deprecation else None
    )

    if registration_deprecates_at is not None and now >= registration_deprecates_at:
        registration_supported = False

    # Este script só executa depois que o serviço Actions entregou o job ao runner.
    # Portanto o pickup atual é evidência independente de suporte de runtime naquele instante.
    runtime_supported_now = runtime_deprecates_at is None or now < runtime_deprecates_at
    if deprecation is None:
        runtime_supported_now = True

    latest_version: RunnerVersion | None = None
    latest_published_at: str | None = None
    if latest_release:
        try:
            latest_version = RunnerVersion.parse(str(latest_release.get("tag_name") or ""))
        except ValueError:
            latest_version = None
        latest_published_at = str(latest_release.get("published_at") or "") or None

    latest_comparison = "unknown"
    if latest_version is not None:
        latest_comparison = (
            "current" if installed == latest_version
            else "behind" if installed < latest_version
            else "ahead"
        )

    ok = registration_supported and runtime_supported_now
    return {
        "ok": ok,
        "installed_version": str(installed),
        "registration_minimum": ".".join(str(part) for part in REGISTRATION_MINIMUM),
        "registration_supported": registration_supported,
        "runtime_supported_now": runtime_supported_now,
        "runtime_support_evidence": "current_job_pickup",
        "deprecation_api_status": deprecation_status,
        "registration_deprecates_at": (
            registration_deprecates_at.isoformat() if registration_deprecates_at else None
        ),
        "runtime_deprecates_at": (
            runtime_deprecates_at.isoformat() if runtime_deprecates_at else None
        ),
        "latest_release_api_status": latest_status,
        "latest_public_version": str(latest_version) if latest_version else None,
        "latest_public_published_at": latest_published_at,
        "latest_public_comparison": latest_comparison,
        "latest_public_is_advisory": True,
    }


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temp, path)


def build_evidence(*, installed: RunnerVersion, token: str | None = None) -> dict[str, Any]:
    repository = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not repository:
        raise RuntimeError("github_repository_missing")
    dep_status, deprecation = fetch_deprecation(repository, installed, token)
    latest_status, latest = fetch_latest_release()
    result = evaluate_policy(
        installed=installed,
        deprecation=deprecation,
        deprecation_status=dep_status,
        latest_release=latest,
        latest_status=latest_status,
        now=utc_now(),
    )
    result.update(
        {
            "schema_version": "1",
            "repository": repository,
            "source_sha": os.environ.get("GITHUB_SHA", ""),
            "runner_name": os.environ.get("RUNNER_NAME", ""),
            "runner_os": os.environ.get("RUNNER_OS", ""),
            "runner_arch": os.environ.get("RUNNER_ARCH", ""),
            "checked_at": utc_now().isoformat(),
        }
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-file", type=Path, required=True)
    parser.add_argument(
        "--version",
        help="Versão explícita apenas para diagnóstico/teste; por padrão lê Runner.Listener.",
    )
    args = parser.parse_args()

    try:
        installed = (
            RunnerVersion.parse(args.version)
            if args.version
            else detect_runner_version()
        )
        token = os.environ.get("RUNNER_DEPRECATION_API_TOKEN") or os.environ.get("GITHUB_TOKEN")
        evidence = build_evidence(installed=installed, token=token)
        code = 0 if evidence["ok"] else 3
    except Exception as exc:
        evidence = {
            "schema_version": "1",
            "ok": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "repository": os.environ.get("GITHUB_REPOSITORY", ""),
            "source_sha": os.environ.get("GITHUB_SHA", ""),
            "runner_name": os.environ.get("RUNNER_NAME", ""),
            "checked_at": utc_now().isoformat(),
        }
        code = 2

    atomic_json(args.evidence_file, evidence)
    print(json.dumps(evidence, ensure_ascii=False, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
