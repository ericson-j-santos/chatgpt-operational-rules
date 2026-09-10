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
    "AGENTS.md",
    "CHANGELOG.md",
    "README.md",
    "rules/e2e-validation.md",
    "rules/evidence-validation.md",
    "rules/terminal-execution.md",
    "scripts/validate_rules.py",
    ".github/workflows/validate-rules.yml",
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
            errors.append(f"files[{index}] não é objeto.")
            continue
        rel = entry.get("path")
        if not isinstance(rel, str) or not rel:
            errors.append(f"files[{index}].path ausente.")
            continue
        paths.append(rel)

        candidate = Path(rel)
        if candidate.is_absolute() or ".." in candidate.parts:
            errors.append(f"caminho inseguro no manifesto: {rel}")
            continue

        target = root / candidate
        if not target.is_file():
            errors.append(f"arquivo ausente: {rel}")
            continue

        payload = target.read_bytes()
        actual_hash = sha256_bytes(payload)
        actual_size = len(payload)

        if entry.get("sha256") != actual_hash:
            errors.append(f"sha256 divergente: {rel}")
        if entry.get("size") != actual_size:
            errors.append(f"tamanho divergente: {rel}")

    duplicates = sorted({path for path in paths if paths.count(path) > 1})
    if duplicates:
        errors.append("paths duplicados: " + ", ".join(duplicates))

    missing_required = sorted(REQUIRED_PATHS - set(paths))
    if missing_required:
        errors.append("paths obrigatórios ausentes: " + ", ".join(missing_required))

    readme_path = root / "README.md"
    agents_path = root / "AGENTS.md"
    changelog_path = root / "CHANGELOG.md"
    if readme_path.is_file():
        readme = readme_path.read_text(encoding="utf-8")
        match = re.search(r"^Versão:\s*(\S+)\s*$", readme, re.MULTILINE)
        if not match or match.group(1) != version:
            errors.append("versão do README diverge do manifesto.")
        if "rules/e2e-validation.md" not in readme:
            errors.append("README não referencia a regra E2E obrigatória.")
    if agents_path.is_file():
        agents = agents_path.read_text(encoding="utf-8")
        if "rules/e2e-validation.md" not in agents:
            errors.append("AGENTS.md não referencia a regra E2E obrigatória.")
    if changelog_path.is_file() and isinstance(version, str):
        changelog = changelog_path.read_text(encoding="utf-8")
        if f"## {version} -" not in changelog:
            errors.append("CHANGELOG não contém a versão do manifesto.")

    return errors


def run_self_test(manifest: dict[str, Any]) -> list[str]:
    errors: list[str] = []

    baseline_errors = validate(manifest)
    if baseline_errors:
        errors.append("baseline inválida: " + " | ".join(baseline_errors))
        return errors

    tampered = copy.deepcopy(manifest)
    if not tampered["files"]:
        return ["autoteste não encontrou entrada para adulterar."]

    tampered["files"][0]["sha256"] = "0" * 64
    tampered_errors = validate(tampered)
    if not any("sha256 divergente" in item for item in tampered_errors):
        errors.append("autoteste negativo falhou: hash incorreto não foi detectado.")

    missing = copy.deepcopy(manifest)
    missing["files"] = [
        item for item in missing["files"]
        if item.get("path") != "rules/e2e-validation.md"
    ]
    missing_errors = validate(missing)
    if not any("paths obrigatórios ausentes" in item for item in missing_errors):
        errors.append("autoteste negativo falhou: regra E2E ausente não foi detectada.")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Executa casos negativos para provar que o validador detecta falhas.",
    )
    args = parser.parse_args()

    try:
        manifest = load_manifest()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"VALIDATION_FAILED: {exc}", file=sys.stderr)
        return 2

    errors = run_self_test(manifest) if args.self_test else validate(manifest)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        print(f"VALIDATION_FAILED errors={len(errors)}", file=sys.stderr)
        return 1

    mode = "self-test" if args.self_test else "validation"
    print(f"VALIDATION_OK mode={mode} version={manifest['version']} files={len(manifest['files'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
