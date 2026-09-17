from __future__ import annotations

import unittest
from fastapi.testclient import TestClient
from services.todo_gateway.gateway import create_app
from services.todo_gateway.gitlab_webhook import authorized, normalize


class Queue:
    def __init__(self):
        self.ids = set()
        self.fail = False

    def enqueue(self, event):
        if self.fail:
            raise RuntimeError("db unavailable")
        inserted = event.event_id not in self.ids
        self.ids.add(event.event_id)
        return inserted


def payload():
    return {
        "object_kind": "push",
        "after": "abc123",
        "ref": "refs/heads/main",
        "project": {
            "id": 84366761,
            "path_with_namespace": "ericson-j-santos/reqsys-v2-enterprise-real",
        },
    }


class GitLabWebhookTests(unittest.TestCase):
    def setUp(self):
        self.queue = Queue()
        self.client = TestClient(
            create_app(
                self.queue,
                "gateway-secret",
                "hook-secret",
                "ericson-j-santos/reqsys-v2-enterprise-real",
            )
        )
        self.headers = {
            "X-Gitlab-Token": "hook-secret",
            "X-Gitlab-Event": "Push Hook",
            "X-Gitlab-Event-UUID": "uuid-1",
        }

    def test_constant_time_auth_contract(self):
        self.assertTrue(authorized("secret", "secret"))
        self.assertFalse(authorized("secret", "wrong"))
        self.assertFalse(authorized("", "secret"))

    def test_normalizes_supported_event_with_contract_safe_ids(self):
        event = normalize(
            "Push Hook",
            "uuid-1",
            payload(),
            "ericson-j-santos/reqsys-v2-enterprise-real",
        )
        self.assertTrue(event.event_id.startswith("evt-gitlab-"))
        self.assertTrue(event.correlation_id.startswith("corr-gitlab-"))
        self.assertEqual(event.producer, "local-agent")
        self.assertEqual(event.todo["external_id"], "gitlab:uuid-1")

    def test_rejects_wrong_project_and_unsupported_event(self):
        with self.assertRaises(PermissionError):
            normalize("Push Hook", "uuid-1", payload(), "other/project")
        with self.assertRaises(ValueError):
            normalize("Pipeline Hook", "uuid-1", payload())

    def test_requires_stable_identifier(self):
        body = {"object_kind": "push", "project": {"path_with_namespace": "p/r"}}
        with self.assertRaises(ValueError):
            normalize("Push Hook", None, body)

    def test_endpoint_auth_accept_and_replay(self):
        self.assertEqual(self.client.post("/v1/webhooks/gitlab", json=payload()).status_code, 401)
        first = self.client.post("/v1/webhooks/gitlab", json=payload(), headers=self.headers)
        replay = self.client.post("/v1/webhooks/gitlab", json=payload(), headers=self.headers)
        self.assertEqual(first.status_code, 202)
        self.assertFalse(first.json()["duplicate"])
        self.assertEqual(replay.status_code, 202)
        self.assertTrue(replay.json()["duplicate"])
        self.assertEqual(first.json()["event_id"], replay.json()["event_id"])

    def test_endpoint_rejects_invalid_payload_event_and_project(self):
        malformed = self.client.post("/v1/webhooks/gitlab", content=b"not-json", headers=self.headers)
        self.assertEqual(malformed.status_code, 422)
        headers = dict(self.headers)
        headers["X-Gitlab-Event"] = "Pipeline Hook"
        unsupported = self.client.post("/v1/webhooks/gitlab", json=payload(), headers=headers)
        self.assertEqual(unsupported.status_code, 422)
        wrong_project = payload()
        wrong_project["project"]["path_with_namespace"] = "other/project"
        forbidden = self.client.post("/v1/webhooks/gitlab", json=wrong_project, headers=self.headers)
        self.assertEqual(forbidden.status_code, 403)

    def test_endpoint_fails_closed_when_queue_unavailable(self):
        self.queue.fail = True
        response = self.client.post("/v1/webhooks/gitlab", json=payload(), headers=self.headers)
        self.assertEqual(response.status_code, 503)


if __name__ == "__main__":
    unittest.main()
