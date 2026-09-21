from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.execution_lane_client import (
    ExecutionLaneClient,
    ExecutionLaneError,
    validate_base_url,
    validate_execution_request,
)


def request_payload() -> dict:
    return {
        "repository": "ericson-j-santos/example",
        "issue_number": 90,
        "request_id": "todo-execution-90",
        "base_sha": "a" * 40,
        "priority": 10,
        "max_attempts": 3,
    }


class FakeResponse:
    def __init__(self, payload: dict, status: int = 201) -> None:
        self.payload = payload
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def getcode(self) -> int:
        return self.status

    def read(self, _limit: int) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class ExecutionLaneClientTests(unittest.TestCase):
    def test_http_is_allowed_only_on_loopback(self) -> None:
        self.assertEqual(validate_base_url("http://127.0.0.1:8097/"), "http://127.0.0.1:8097")
        self.assertEqual(validate_base_url("https://worker.example:443"), "https://worker.example:443")
        with self.assertRaisesRegex(ValueError, "loopback"):
            validate_base_url("http://worker.example:8097")
        with self.assertRaises(ValueError):
            validate_base_url("file:///tmp/socket")

    def test_execution_request_is_strict_and_normalized(self) -> None:
        payload = request_payload()
        normalized = validate_execution_request(payload)
        self.assertEqual(normalized["base_sha"], "a" * 40)
        payload["arbitrary_command"] = "rm -rf /"
        with self.assertRaisesRegex(ValueError, "não permitidos"):
            validate_execution_request(payload)

    def test_enqueue_reads_token_from_file_and_checks_independent_response(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            token_file = Path(tmp) / "token"
            token_file.write_text("super-secret-token\n", encoding="utf-8")
            payload = request_payload()
            response = {
                "created": True,
                "task": {**payload, "task_id": "task-1", "state": "queued"},
            }
            observed = {}

            def fake_urlopen(request, timeout):
                observed["url"] = request.full_url
                observed["authorization"] = request.get_header("Authorization")
                observed["body"] = request.data.decode("utf-8")
                observed["timeout"] = timeout
                return FakeResponse(response)

            client = ExecutionLaneClient("http://127.0.0.1:8097", token_file)
            with patch("scripts.execution_lane_client.urlopen", side_effect=fake_urlopen):
                result = client.enqueue(payload)

            self.assertTrue(result["created"])
            self.assertEqual(observed["url"], "http://127.0.0.1:8097/v1/tasks")
            self.assertEqual(observed["authorization"], "Bearer super-secret-token")
            self.assertNotIn("super-secret-token", observed["body"])

    def test_enqueue_rejects_mismatched_response(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            token_file = Path(tmp) / "token"
            token_file.write_text("token-value", encoding="utf-8")
            payload = request_payload()
            response = {
                "created": True,
                "task": {**payload, "repository": "other/repo"},
            }
            client = ExecutionLaneClient("http://localhost:8097", token_file)
            with patch(
                "scripts.execution_lane_client.urlopen",
                return_value=FakeResponse(response),
            ):
                with self.assertRaisesRegex(ExecutionLaneError, "mismatch:repository"):
                    client.enqueue(payload)


if __name__ == "__main__":
    unittest.main()
