#!/usr/bin/env python3
"""Valida a consistência das regras operacionais e testa o próprio validador."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "MANIFEST.json"
sys.path.insert(0, str(ROOT / "scripts"))
import command_gateway as cg

REQUIRED_PATHS = {
    ".github/workflows/validate-rules.yml",
    "AGENTS.md", "CHANGELOG.md", "README.md", "config/command-gateway.policy.json",
    "config/command-gateway.policy.posix.json", "config/command-gateway.policy.noteri-wsl.json",
    "rules/command-gateway.md", "rules/e2e-validation.md", "rules/evidence-validation.md",
    "rules/log-driven-improvement.md", "rules/todo-gateway-pr185-reconciliation.md",
    "rules/session-bootstrap.md", "rules/terminal-execution.md", "scripts/command_gateway.py",
    "scripts/command_gateway_e2e.py", "scripts/generate_manifest.py", "scripts/host_bootstrap_e2e.py",
    "scripts/install_command_gateway_host.py", "scripts/update_command_gateway_host.py",
    "scripts/reconcile_todo_gateway_pr185_main_governed.py",
    "scripts/session_bootstrap.py", "scripts/session_bootstrap_e2e.py",
    "scripts/session_preflight.py", "scripts/session_preflight_e2e.py", "scripts/session_launcher.py",
    "scripts/runner_version_preflight.py", "tests/test_runner_version_preflight.py",
    "scripts/session_launcher_e2e.py", "scripts/dual_host_preflight.py", "scripts/host_operating_profile.py", "scripts/validate_rules.py",
    "scripts/rdc_host_fix.py", "scripts/rdc_task_resilience.py", "scripts/rdc_owner_arbitration.py", "scripts/rdc_semantic_result.py",
    "rules/dual-host-routing.md", "rules/tool-routing.md", "rules/control-plane-recovery.md", "rules/cost-governance.md", "scripts/tool_router.py",
    "tests/test_command_gateway.py", "tests/test_command_gateway_posix_policy.py",
    "tests/test_validate_rules_policy_profiles.py",
    "tests/test_host_bootstrap.py", "tests/test_update_command_gateway_host.py",
    "tests/test_session_bootstrap.py",
    "tests/test_session_preflight.py", "tests/test_session_launcher.py", "tests/test_dual_host_preflight.py",
    "tests/test_host_operating_profile.py", "tests/test_tool_router.py", "tests/test_rdc_semantic_result.py",
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
    listed = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=False)
    if listed.returncode == 0:
        tracked = {item.decode("utf-8") for item in listed.stdout.split(b"\0") if item}; tracked.discard("MANIFEST.json")
        missing_tracked = sorted(tracked - set(paths))
        if missing_tracked: errors.append("arquivos rastreados ausentes do manifesto: " + ", ".join(missing_tracked))
    readme_path, agents_path = root / "README.md", root / "AGENTS.md"
    changelog_path = root / "CHANGELOG.md"
    policy_paths = {
        "windows": root / "config" / "command-gateway.policy.json",
        "windows_noteri": root / "config" / "command-gateway.policy.noteri-wsl.json",
        "posix": root / "config" / "command-gateway.policy.posix.json",
    }
    if readme_path.is_file():
        readme = readme_path.read_text(encoding="utf-8")
        match = re.search(r"^Versão:\s*(\S+)\s*$", readme, re.MULTILINE)
        if not match or match.group(1) != version:
            errors.append("versão do README diverge do manifesto.")
        for ref in ("rules/e2e-validation.md", "rules/command-gateway.md", "rules/session-bootstrap.md", "rules/control-plane-recovery.md", "rules/log-driven-improvement.md"):
            if ref not in readme:
                errors.append(f"README não referencia {ref}.")
        if "BOOTSTRAP_OK" not in readme:
            errors.append("README não exige BOOTSTRAP_OK.")
        if "scripts/session_preflight.py" not in readme or "state_validated=true" not in readme:
            errors.append("README não define session_preflight.py como ponto de entrada obrigatório.")
    if agents_path.is_file():
        agents = agents_path.read_text(encoding="utf-8")
        for ref in ("rules/e2e-validation.md", "rules/command-gateway.md", "rules/session-bootstrap.md", "rules/control-plane-recovery.md", "rules/log-driven-improvement.md"):
            if ref not in agents:
                errors.append(f"AGENTS.md não referencia {ref}.")
        for required_text in ("scripts/session_preflight.py", "scripts/install_command_gateway_host.py", "BOOTSTRAP_OK", "HOST_BOOTSTRAP_OK", "PowerShell", "fallback", "Remote Desktop Commander"):
            if required_text not in agents:
                errors.append(f"AGENTS.md não contém contrato obrigatório: {required_text}")
    if changelog_path.is_file() and isinstance(version, str):
        if f"## {version} -" not in changelog_path.read_text(encoding="utf-8"):
            errors.append("CHANGELOG não contém a versão do manifesto.")
    for expected_platform, policy_path in policy_paths.items():
        if not policy_path.is_file():
            errors.append(f"política do Command Gateway ausente: {policy_path.name}.")
            continue
        try:
            policy = json.loads(policy_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            errors.append(f"política do Command Gateway não é JSON válido: {policy_path.name}.")
        else:
            if not isinstance(policy, dict):
                errors.append(f"política do Command Gateway não é objeto: {policy_path.name}.")
                continue
            try:
                cg.validate_policy(policy, enforce_platform=False)
            except cg.GatewayError as exc:
                errors.append(f"política do Command Gateway inválida ({policy_path.name}): {exc}")
                continue
            if policy.get("platform") != ("windows" if expected_platform == "windows_noteri" else expected_platform):
                errors.append(f"plataforma da política divergente: {policy_path.name}.")
            if not policy.get("allowed_roots"):
                errors.append(f"Command Gateway sem allowlist: {policy_path.name}.")
            if expected_platform in {"windows", "windows_noteri"} and not policy.get("denied_roots"):
                errors.append("Command Gateway Windows sem denied_roots.")
            timeout = policy.get("max_timeout_seconds")
            if type(timeout) is not int or timeout <= 0:
                errors.append(f"Command Gateway sem timeout máximo válido: {policy_path.name}.")
            if policy.get("require_session_bootstrap") is not True:
                errors.append(f"Command Gateway deve exigir bootstrap de sessão: {policy_path.name}.")
            if policy.get("require_preflight_snapshot") is not True:
                errors.append(f"Command Gateway deve exigir snapshot de preflight: {policy_path.name}.")
            if policy.get("rules_version") != version:
                errors.append(f"rules_version da política diverge do manifesto: {policy_path.name}.")
            if not policy.get("worktree_root") or not policy.get("worktree_prefix"):
                errors.append(f"Command Gateway sem configuração de worktree por sessão: {policy_path.name}.")
            if expected_platform == "posix":
                allowed = policy.get("allowed_roots", [])
                broad_roots = {"${REQSYS_WORKSPACE_ROOT}", "${REQSYS_WORKSPACE_ROOT}/*"}
                if any(item in broad_roots for item in allowed):
                    errors.append("Command Gateway POSIX autoriza o workspace inteiro.")
                expected_environment = {
                    "REQSYS_WORKSPACE_ROOT": "/workspace",
                    "REQSYS_GATEWAY_STATE_DIR": "/workspace/.reqsys-command-gateway-posix",
                }
                if policy.get("required_path_environment") != expected_environment:
                    errors.append(
                        "Command Gateway POSIX não vincula o ambiente aos caminhos confiáveis."
                    )
                if policy.get("state_dir") != "${REQSYS_GATEWAY_STATE_DIR}":
                    errors.append("Command Gateway POSIX não usa state_dir dedicado.")
                if policy.get("worktree_root") != "${REQSYS_WORKSPACE_ROOT}/.chatgpt-workers":
                    errors.append("Command Gateway POSIX não usa namespace dedicado de worktrees.")
                substitutions = {
                    "${" + name + "}": value
                    for name, value in expected_environment.items()
                }

                def resolve_posix(value: str) -> str:
                    for source, target in substitutions.items():
                        value = value.replace(source, target)
                    return value

                invalid_path: str | None = None
                for field in ("allowed_roots", "denied_roots", "session_source_roots"):
                    for value in policy.get(field, []):
                        resolved = resolve_posix(value)
                        if (
                            cg.path_variable_names(resolved)
                            or "$" in resolved
                            or "%" in resolved
                            or not PurePosixPath(resolved).is_absolute()
                        ):
                            invalid_path = f"{field} deve conter somente caminhos POSIX absolutos"
                            break
                    if invalid_path:
                        break
                if not invalid_path:
                    for field in ("state_dir", "worktree_root"):
                        value = policy.get(field)
                        if isinstance(value, str):
                            resolved = resolve_posix(value)
                            if (
                                cg.path_variable_names(resolved)
                                or "$" in resolved
                                or "%" in resolved
                                or not PurePosixPath(resolved).is_absolute()
                            ):
                                invalid_path = f"{field} deve ser caminho POSIX absoluto"
                                break
                if invalid_path:
                    errors.append(f"caminhos da política POSIX inválidos: {invalid_path}")
                expected_worktree_pattern = (
                    str(policy.get("worktree_root", "")).rstrip("/") + "/*"
                )
                if expected_worktree_pattern not in allowed:
                    errors.append(
                        "caminhos da política POSIX inválidos: "
                        "worktree_root não está coberto por allowed_roots"
                    )
    windows_path, noteri_path = policy_paths["windows"], policy_paths["windows_noteri"]
    if windows_path.is_file() and noteri_path.is_file():
        try:
            windows_policy = json.loads(windows_path.read_text(encoding="utf-8"))
            noteri_policy = json.loads(noteri_path.read_text(encoding="utf-8"))
            if not isinstance(windows_policy, dict) or not isinstance(noteri_policy, dict):
                raise ValueError("policy not object")
            if windows_policy.get("noteri_wsl_running_inventory") is True:
                errors.append("política Windows global não pode ativar WSL Noteri.")
            if noteri_policy.pop("noteri_wsl_running_inventory", None) is not True or noteri_policy != windows_policy:
                errors.append("perfil Noteri deve diferir da política Windows somente pelo opt-in WSL.")
        except (OSError, ValueError, json.JSONDecodeError):
            errors.append("perfil Noteri não pôde ser comparado à política Windows.")
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
    for required in ("rules/e2e-validation.md", "rules/log-driven-improvement.md", "scripts/command_gateway.py", "scripts/session_bootstrap.py", "scripts/session_preflight.py", "scripts/install_command_gateway_host.py"):
        missing = copy.deepcopy(manifest)
        missing["files"] = [item for item in missing["files"] if item.get("path") != required]
        if not any("paths obrigatórios ausentes" in item for item in validate(missing)):
            errors.append(f"autoteste negativo falhou: {required} ausente não foi detectado.")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true", help="Executa casos negativos para provar que o validador detecta falhas.")
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
