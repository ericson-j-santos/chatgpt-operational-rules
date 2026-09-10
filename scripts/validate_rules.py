#!/usr/bin/env python3
"""Valida a consistência das regras operacionais e testa o próprio validador."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "MANIFEST.json"

REQUIRED_PATHS = {
    ".github/workflows/validate-rules.yml",
    "AGENTS.md",
    "CHANGELOG.md",
    "README.md",
    "config/command-gateway.policy.json",
    "rules/command-gateway.md",
    "rules/e2e-validation.md",
    "rules/evidence-validation.md",
    "rules/terminal-execution.md",
    "scripts/command_gateway.py",
    "scripts/command_gateway_e2e.py",
    "scripts/validate_rules.py",
    "tests/test_command_gateway.py",
}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_manifest() -> dict[str, Any]:
    with MANIFEST_PATH.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("MANIFEST.json deve conter um objeto JSON.")
    return data


def validate(manifest: dict[str, Any], root: Path = ROOT) -> list[str]:
    errors: list[str] = []
    version = manifest.get("version")
    files = manifest.get("files")
    if not isinstance(version, str) or not version.strip():
        errors.append("manifest.version ausente ou inválido.")
    if not isinstance(files, list):
        return errors + ["manifest.files ausente ou inválido."]

    paths: list[str] = []
    for index, entry in enumerate(files):
        if not isinstance(entry, dict):
            errors.append(f"files[{index}] não é objeto."); continue
        rel = entry.get("path")
        if not isinstance(rel, str) or not rel:
            errors.append(f"files[{index}].path ausente."); continue
        paths.append(rel)
        candidate = Path(rel)
        if candidate.is_absolute() or ".." in candidate.parts:
            errors.append(f"caminho inseguro no manifesto: {rel}"); continue
        target = root / candidate
        if not target.is_file():
            errors.append(f"arquivo ausente: {rel}"); continue
        payload = target.read_bytes()
        if entry.get("sha256") != sha256_bytes(payload):
            errors.append(f"sha256 divergente: {rel}")
        if entry.get("size") != len(payload):
            errors.append(f"tamanho divergente: {rel}")

    duplicates = sorted({path for path in paths if paths.count(path) > 1})
    if duplicates:
        errors.append("paths duplicados: " + ", ".join(duplicates))
    missing_required = sorted(REQUIRED_PATHS - set(paths))
    if missing_required:
        errors.append("paths obrigatórios ausentes: " + ", ".join(missing_required))

    readme_path, agents_path = root / "README.md", root / "AGENTS.md"
    changelog_path = root / "CHANGELOG.md"
    policy_path = root / "config" / "command-gateway.policy.json"
    if readme_path.is_file():
        readme = readme_path.read_text(encoding="utf-8")
        match = re.search(r"^Versão:\s*(\S+)\s*$", readme, re.MULTILINE)
        if not match or match.group(1) != version:
            errors.append("versão do README diverge do manifesto.")
        for ref in ("rules/e2e-validation.md", "rules/command-gateway.md"):
            if ref not in readme:
                errors.append(f"README não referencia {ref}.")
    if agents_path.is_file():
        agents = agents_path.read_text(encoding="utf-8")
        for ref in ("rules/e2e-validation.md", "rules/command-gateway.md"):
            if ref not in agents:
                errors.append(f"AGENTS.md não referencia {ref}.")
    if changelog_path.is_file() and isinstance(version, str):
        if f"## {version} -" not in changelog_path.read_text(encoding="utf-8"):
            errors.append("CHANGELOG não contém a versão do manifesto.")
    if policy_path.is_file():
        try:
            policy = json.loads(policy_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            errors.append("política do Command Gateway não é JSON válido.")
        else:
            if policy.get("version") != 1:
                errors.append("versão da política do Command Gateway inválida.")
            if not policy.get("allowed_roots"):
                errors.append("Command Gateway sem allowlist.")
            if not policy.get("denied_roots"):
                errors.append("Command Gateway sem denied_roots.")
            if int(policy.get("max_timeout_seconds", 0)) <= 0:
                errors.append("Command Gateway sem timeout máximo válido.")
    return errors


def run_self_test(manifest: dict[str, Any]) -> list[str]:
    baseline_errors = validate(manifest)
    if baseline_errors:
        return ["baseline inválida: " + " | ".join(baseline_errors)]
    errors: list[str] = []
    tampered = copy.deepcopy(manifest)
    if not tampered["files"]:
        return ["autoteste não encontrou entrada para adulterar."]
    tampered["files"][0]["sha256"] = "0" * 64
    if not any("sha256 divergente" in item for item in validate(tampered)):
        errors.append("autoteste negativo falhou: hash incorreto não foi detectado.")
    for required in ("rules/e2e-validation.md", "scripts/command_gateway.py"):
        missing = copy.deepcopy(manifest)
        missing["files"] = [item for item in missing["files"] if item.get("path") != required]
        if not any("paths obrigatórios ausentes" in item for item in validate(missing)):
            errors.append(f"autoteste negativo falhou: {required} ausente não foi detectado.")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true",
                        help="Executa casos negativos para provar que o validador detecta falhas.")
    args = parser.parse_args()
    try:
        manifest = load_manifest()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"VALIDATION_FAILED: {exc}", file=sys.stderr); return 2
    errors = run_self_test(manifest) if args.self_test else validate(manifest)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        print(f"VALIDATION_FAILED errors={len(errors)}", file=sys.stderr); return 1
    mode = "self-test" if args.self_test else "validation"
    print(f"VALIDATION_OK mode={mode} version={manifest['version']} files={len(manifest['files'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
