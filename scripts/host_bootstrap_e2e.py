#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().with_name("install_command_gateway_host.py")
sys.path.insert(0, str(SCRIPT.parent))

import install_command_gateway_host as hb


def make_bundle(root: Path) -> Path:
    bundle = root / "bundle"
    entries = []
    for source in hb.RUNTIME_MAP:
        payload = f"runtime:{source}\n".encode("utf-8")
        target = bundle / source
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        entries.append({"path": source, "sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)})
    (bundle / "MANIFEST.json").write_text(json.dumps({"version": "1.5.0", "files": entries}), encoding="utf-8")
    return bundle


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="host-bootstrap-e2e-"))
    try:
        bundle = make_bundle(root)
        install_root = root / "install"
        work_root = root / "workers"
        commit = "d" * 40
        first = hb.install_bundle(bundle, install_root, work_root, commit)
        if first["result"] != "HOST_BOOTSTRAP_OK" or not work_root.is_dir():
            raise AssertionError("instalação positiva não comprovada")
        for dest in hb.RUNTIME_MAP.values():
            if not (install_root / dest).is_file():
                raise AssertionError(f"runtime ausente: {dest}")
        second = hb.install_bundle(bundle, install_root, work_root, commit)
        if not second.get("backup_dir") or not Path(second["backup_dir"]).is_dir():
            raise AssertionError("reinstalação não criou backup")
        source = next(iter(hb.RUNTIME_MAP))
        original = (bundle / source).read_bytes()
        (bundle / source).write_text("tampered\n", encoding="utf-8")
        try:
            hb.verify_bundle(bundle)
        except hb.HostBootstrapError:
            pass
        else:
            raise AssertionError("bundle adulterado foi aceito")
        (bundle / source).write_bytes(original)
        try:
            hb.validate_commit("short")
        except hb.HostBootstrapError:
            pass
        else:
            raise AssertionError("commit curto foi aceito")
        receipt = json.loads((install_root / "install-receipt.json").read_text(encoding="utf-8"))
        if receipt.get("source_commit") != commit or receipt.get("rules_version") != "1.5.0":
            raise AssertionError("recibo não comprova revisão instalada")
        source_repo = root / "source-repo"
        source_repo.mkdir()
        import subprocess
        subprocess.run(["git", "init"], cwd=source_repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "e2e@example.invalid"], cwd=source_repo, check=True)
        subprocess.run(["git", "config", "user.name", "E2E"], cwd=source_repo, check=True)
        (source_repo / "x.txt").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "add", "x.txt"], cwd=source_repo, check=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=source_repo, check=True, capture_output=True)
        source_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=source_repo, check=True, text=True, capture_output=True).stdout.strip()
        validation_repo, observed_head = hb.prepare_validation_repo(root / "verify-workers", source_head, str(source_repo))
        if observed_head != source_head or not (validation_repo / ".git").exists():
            raise AssertionError("clone de validação não comprovado")
        print("HOST_BOOTSTRAP_E2E_OK positive=3 negative=2 manifest=sha256 validation_repo=exact_commit")
        return 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
