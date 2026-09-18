from __future__ import annotations

import hmac
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

MAX_BODY_BYTES = 65536
TARGET_LOCK = threading.Lock()
TARGET_BASE_URL = ""


def valid_target(value: str) -> str:
    value = value.rstrip("/")
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError("target must be https")
    if not parts.hostname.endswith(".trycloudflare.com"):
        raise ValueError("target host is not an approved DEV quick tunnel")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError("target must be an origin URL")
    return value


def set_target(value: str) -> str:
    global TARGET_BASE_URL
    normalized = valid_target(value)
    with TARGET_LOCK:
        TARGET_BASE_URL = normalized
    return normalized


def get_target() -> str:
    with TARGET_LOCK:
        return TARGET_BASE_URL


def bearer_ok(header: str | None) -> bool:
    expected = os.environ.get("RELAY_ADMIN_TOKEN", "")
    if not expected or not header or not header.startswith("Bearer "):
        return False
    return hmac.compare_digest(header[7:], expected)


def probe_target(target: str) -> bool:
    try:
        with urlopen(Request(target + "/readyz", headers={"Accept": "application/json"}), timeout=10) as response:
            data = json.loads(response.read(4096).decode("utf-8"))
            return response.status == 200 and data.get("status") == "ready"
    except (HTTPError, URLError, OSError, TimeoutError, json.JSONDecodeError):
        return False


class Handler(BaseHTTPRequestHandler):
    server_version = "ReqSysGitLabRelay/1.0"

    def log_message(self, fmt: str, *args: object) -> None:
        print(json.dumps({"remote": self.client_address[0], "path": self.path, "message": fmt % args}))

    def _json(self, status: int, payload: dict[str, object]) -> None:
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self, limit: int = MAX_BODY_BYTES) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("invalid content length")
        if length < 0 or length > limit:
            raise OverflowError("payload too large")
        return self.rfile.read(length)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._json(200, {"status": "ok"})
            return
        if self.path == "/readyz":
            target = get_target()
            ready = bool(target) and probe_target(target)
            self._json(200 if ready else 503, {"status": "ready" if ready else "not_ready", "target_set": bool(target)})
            return
        self._json(404, {"detail": "not found"})

    def do_POST(self) -> None:
        if self.path == "/admin/target":
            if not bearer_ok(self.headers.get("Authorization")):
                self._json(401, {"detail": "unauthorized"})
                return
            try:
                payload = json.loads(self._body(4096).decode("utf-8"))
                target = valid_target(str(payload.get("base_url", "")))
            except OverflowError:
                self._json(413, {"detail": "payload too large"})
                return
            except (ValueError, json.JSONDecodeError):
                self._json(422, {"detail": "invalid target"})
                return
            if not probe_target(target):
                self._json(422, {"detail": "target not ready"})
                return
            set_target(target)
            self._json(200, {"status": "updated", "base_url": target})
            return

        if self.path != "/v1/webhooks/gitlab":
            self._json(404, {"detail": "not found"})
            return

        target = get_target()
        if not target:
            self._json(503, {"detail": "downstream target unavailable"})
            return
        try:
            body = self._body()
        except OverflowError:
            self._json(413, {"detail": "payload too large"})
            return
        except ValueError:
            self._json(400, {"detail": "bad request"})
            return

        headers = {}
        for name in ("Content-Type", "X-Gitlab-Token", "X-Gitlab-Event", "X-Gitlab-Event-UUID"):
            value = self.headers.get(name)
            if value:
                headers[name] = value
        request = Request(target + "/v1/webhooks/gitlab", data=body, headers=headers, method="POST")
        try:
            with urlopen(request, timeout=20) as response:
                raw = response.read(MAX_BODY_BYTES)
                self.send_response(response.status)
                self.send_header("Content-Type", response.headers.get("Content-Type", "application/json"))
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        except HTTPError as exc:
            raw = exc.read(MAX_BODY_BYTES)
            self.send_response(exc.code)
            self.send_header("Content-Type", exc.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        except (URLError, OSError, TimeoutError):
            self._json(503, {"detail": "downstream unavailable"})


def main() -> int:
    port = int(os.environ.get("PORT", "10000"))
    if not os.environ.get("RELAY_ADMIN_TOKEN"):
        raise SystemExit("RELAY_ADMIN_TOKEN is required")
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(json.dumps({"event": "relay_started", "port": port}))
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
