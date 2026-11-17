#!/usr/bin/env python3
"""Rules #219: one tightly scoped local-Qwen proposal; no source edits.

Installed Gateway/Worker Pool must separately attest session, lease, model
digest, cost, task identity and artifact durability. This sidecar is not CAS.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "tests/test_generate_manifest_builder.py"
MODEL = "qwen2.5-coder:7b"
END = "\n\nif __name__=='__main__':"


def require(ok: bool, reason: str) -> None:
    if not ok:
        raise SystemExit(reason)


def git(*args: str) -> bytes:
    try:
        p = subprocess.run(
            ["git", *args], cwd=ROOT, check=False, capture_output=True,
            timeout=15, env={
                "PATH": os.environ.get("PATH", os.defpath),
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C",
            },
        )
    except (OSError, subprocess.TimeoutExpired):
        raise SystemExit("RULES219_GIT_UNAVAILABLE") from None
    require(p.returncode == 0 and len(p.stdout) < 2_000_000,
            "RULES219_GIT_READBACK_FAILED")
    return p.stdout


def state_dir() -> Path:
    repo = Path(git("rev-parse", "--show-toplevel").decode().strip()).resolve()
    require(repo == ROOT and git("remote") == b"",
            "RULES219_WORKSPACE_NOT_DETACHED")
    raw = git("rev-parse", "--absolute-git-dir").decode().strip()
    root = Path(raw).resolve()
    require(root.is_dir() and not root.is_symlink(), "RULES219_GITDIR_INVALID")
    state = root / "rules219"
    require(not state.is_symlink(), "RULES219_STATE_UNTRUSTED")
    state.mkdir(mode=0o700, exist_ok=True)
    return state


def identity() -> tuple[str, str, str]:
    require(len(sys.argv) == 1, "RULES219_ARGUMENTS_FORBIDDEN")
    task = os.environ.get("RULES219_TASK_ID", "")
    base = os.environ.get("RULES219_APPROVED_SHA", "")
    digest = os.environ.get("RULES219_MODEL_DIGEST", "")
    require(re.fullmatch(r"cwp-[0-9a-f]{24}", task) is not None
            and re.fullmatch(r"[0-9a-f]{40}", base) is not None
            and re.fullmatch(r"[0-9a-f]{64}", digest) is not None,
            "RULES219_INSTALLED_CONTEXT_REQUIRED")
    require(git("rev-parse", "HEAD").decode().strip() == base,
            "RULES219_HEAD_DRIFT")
    return task, base, digest


def candidate(source: bytes, method: str) -> bytes:
    """Model must contribute actual safe test assertions; no arbitrary Python."""
    require(type(method) is str and 90 <= len(method) <= 1200
            and method.startswith("    def test_")
            and method.endswith("\n") and "\r" not in method,
            "RULES219_MODEL_CODE_SHAPE")
    try:
        tree = ast.parse("class Generated:\n" + method)
        old = ast.parse(source.decode("utf-8"))
    except (SyntaxError, ValueError, UnicodeError):
        raise SystemExit("RULES219_MODEL_CODE_SYNTAX") from None
    require(len(tree.body) == 1 and isinstance(tree.body[0], ast.ClassDef)
            and len(tree.body[0].body) == 1,
            "RULES219_MODEL_CODE_SCOPE")
    fn = tree.body[0].body[0]
    require(isinstance(fn, ast.FunctionDef)
            and re.fullmatch(r"test_[a-z0-9_]{3,80}", fn.name) is not None
            and fn.name not in {n.name for n in ast.walk(old)
                                if isinstance(n, ast.FunctionDef)}
            and len(fn.args.args) == 1 and fn.args.args[0].arg == "self"
            and fn.args.args[0].annotation is None
            and not fn.args.posonlyargs and fn.args.vararg is None
            and fn.args.kwarg is None and not fn.args.defaults
            and not fn.args.kwonlyargs and not fn.args.kw_defaults
            and not fn.decorator_list and fn.returns is None
            and not getattr(fn, "type_params", [])
            and 2 <= len(fn.body) <= 4,
            "RULES219_MODEL_TEST_SCOPE")
    assertions = set()
    for statement in fn.body:
        require(isinstance(statement, ast.Expr)
                and isinstance(statement.value, ast.Call),
                "RULES219_MODEL_STATEMENT_UNSAFE")
        call = statement.value
        require(not call.keywords and isinstance(call.func, ast.Attribute)
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "self"
                and call.func.attr in {"assertNotEqual", "assertIn", "assertNotIn"}
                and len(call.args) == 2,
                "RULES219_MODEL_CALL_UNSAFE")
        assertions.add(call.func.attr)
        for argument in call.args:
            if isinstance(argument, ast.Constant):
                require(type(argument.value) in (str, int)
                        and len(str(argument.value)) <= 100,
                        "RULES219_MODEL_LITERAL_UNSAFE")
            elif isinstance(argument, ast.Attribute):
                nested = argument.value
                require(argument.attr in {"returncode", "stderr", "stdout"}
                        and isinstance(nested, ast.Call)
                        and not nested.args and not nested.keywords
                        and isinstance(nested.func, ast.Attribute)
                        and isinstance(nested.func.value, ast.Name)
                        and nested.func.value.id == "self"
                        and nested.func.attr == "wrapper",
                        "RULES219_MODEL_NESTED_CALL_UNSAFE")
            else:
                raise SystemExit("RULES219_MODEL_EXPRESSION_UNSAFE")
    require("assertNotEqual" in assertions and "assertIn" in assertions,
            "RULES219_MODEL_ASSERTIONS_MISSING")
    require(source.count(END.encode()) == 1, "RULES219_INSERT_POINT_INVALID")
    amended = source.replace(END.encode(), ("\n" + method + END).encode())
    require(amended != source, "RULES219_MODEL_NO_MATERIAL_CHANGE")
    return amended


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise SystemExit("RULES219_OLLAMA_REDIRECT_FORBIDDEN")


def ollama(opener: urllib.request.OpenerDirector, path: str,
           payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
    request = urllib.request.Request(
        "http://127.0.0.1:11434" + path, data=data,
        headers={"Content-Type": "application/json"},
        method="GET" if data is None else "POST",
    )
    try:
        with opener.open(request, timeout=90) as response:
            require(response.geturl() == request.full_url
                    and response.status == 200, "RULES219_OLLAMA_UNTRUSTED_RESPONSE")
            raw = response.read(65537)
    except (OSError, urllib.error.URLError, TimeoutError, ValueError):
        raise SystemExit("RULES219_OLLAMA_UNAVAILABLE") from None
    require(len(raw) <= 65536, "RULES219_OLLAMA_TOO_LARGE")
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeError):
        raise SystemExit("RULES219_OLLAMA_BAD_JSON") from None
    require(type(body) is dict, "RULES219_OLLAMA_BAD_BODY")
    return body


def main() -> int:
    task, base, pinned_digest = identity()
    state = state_dir()
    require(not (state / "proposal.json").exists()
            and not (state / "proposal.json").is_symlink(),
            "RULES219_PROPOSAL_ALREADY_EXISTS")
    require(git("status", "--porcelain", "--untracked-files=all") == b"",
            "RULES219_SOURCE_NOT_CLEAN")
    source_path = ROOT / SOURCE
    require(source_path.is_file() and not source_path.is_symlink()
            and source_path.stat().st_size < 100000,
            "RULES219_SOURCE_UNTRUSTED")
    before = source_path.read_bytes()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    tags = ollama(opener, "/api/tags").get("models")
    require(type(tags) is list and any(
        type(item) is dict and item.get("name") == MODEL
        and item.get("digest") == pinned_digest
        and not item.get("remote_host") and not item.get("remote_model")
        for item in tags
    ), "RULES219_MODEL_DIGEST_MISMATCH")
    meta = ollama(opener, "/api/show", {"model": MODEL})
    require(not meta.get("remote_host") and not meta.get("remote_model")
            and type(meta.get("details")) is dict
            and meta["details"].get("format") == "gguf",
            "RULES219_MODEL_WEIGHTS_UNTRUSTED")
    response = ollama(opener, "/api/chat", {
        "model": MODEL, "stream": False, "keep_alive": "5m",
        "messages": [
            {"role": "system", "content": "Generate a single real Python unittest method, only JSON with key method. Never output commands."},
            {"role": "user", "content": (
                "Write ONE additional negative regression test for the existing "
                "ManifestBuilderStageTests class. It must verify that wrapper() "
                "without staged source fails (returncode nonzero) and stderr "
                "contains NO_SOURCE_STAGED. Output a string containing an indented "
                "'    def test_...' method with 2 to 4 single-line assertions "
                "only; each assertion is self.assertNotEqual or self.assertIn "
                "or self.assertNotIn, args are string/int constants or "
                "self.wrapper().returncode/stderr/stdout. No assignments, "
                "imports, filesystem calls, other methods, mocks or decorators. "
                "Do not repeat an existing test name."
            )},
        ],
        "format": {"type": "object", "properties": {"method": {"type": "string"}},
                   "required": ["method"], "additionalProperties": False},
        "options": {"temperature": 0, "seed": 7, "num_predict": 400},
    })
    require(response.get("done") is True and response.get("model") == MODEL
            and type(response.get("eval_count")) is int
            and response["eval_count"] > 0,
            "RULES219_MODEL_INFERENCE_UNPROVED")
    running = ollama(opener, "/api/ps").get("models")
    require(type(running) is list and any(
        type(x) is dict and x.get("name") == MODEL
        and x.get("digest") == pinned_digest
        and type(x.get("size")) is int and x["size"] > 0
        and not x.get("remote_host") and not x.get("remote_model")
        for x in running
    ), "RULES219_MODEL_PROCESS_UNPROVED")
    content = response.get("message")
    require(type(content) is dict and type(content.get("content")) is str,
            "RULES219_MODEL_REPLY_INVALID")
    try:
        result = json.loads(content["content"])
    except ValueError:
        raise SystemExit("RULES219_MODEL_REPLY_JSON_INVALID") from None
    require(type(result) is dict and set(result) == {"method"},
            "RULES219_MODEL_REPLY_SHAPE")
    after = candidate(before, result["method"])
    # Source is never changed by proposal. A trusted external reader must
    # independently bind this sidecar to the Gateway + local model process.
    require(source_path.read_bytes() == before
            and git("status", "--porcelain", "--untracked-files=all") == b"",
            "RULES219_PROPOSAL_MUTATED_SOURCE")
    proposal = {
        "task_id": task, "source_sha": base, "source_path": SOURCE,
        "model": MODEL, "digest": pinned_digest,
        "origin": "http://127.0.0.1:11434", "process_verified": True,
        "model_calls": 1,
        "before_sha256": hashlib.sha256(before).hexdigest(),
        "after_sha256": hashlib.sha256(after).hexdigest(),
        "model_reply_sha256": hashlib.sha256(content["content"].encode()).hexdigest(),
        "raw_reply": content["content"],
        "method": result["method"],
    }
    with (state / "proposal.json").open("xb") as output:
        output.write(json.dumps(proposal, sort_keys=True).encode())
        output.flush()
        os.fsync(output.fileno())
    print("RULES219_MODEL_PROPOSAL_RECORDED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
