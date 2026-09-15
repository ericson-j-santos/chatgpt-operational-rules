import unittest
from unittest.mock import patch

from scripts import render_todo_gateway_bootstrap as bootstrap


class RenderTodoGatewayBootstrapTests(unittest.TestCase):
    def test_selects_internal_connection_string(self):
        payload = {
            "internalConnectionString": "postgresql://internal.example/db",
            "externalConnectionString": "postgresql://external.example/db",
        }
        self.assertEqual(
            bootstrap.select_internal_connection_string(payload),
            "postgresql://internal.example/db",
        )

    def test_rejects_payload_without_postgres_url(self):
        with self.assertRaisesRegex(RuntimeError, "no PostgreSQL URL"):
            bootstrap.select_internal_connection_string({"host": "example"})

    def test_rejects_ambiguous_internal_urls(self):
        payload = {
            "internalUrl": "postgresql://internal-a/db",
            "privateUrl": "postgresql://internal-b/db",
        }
        with self.assertRaisesRegex(RuntimeError, "unambiguous internal PostgreSQL URL"):
            bootstrap.select_internal_connection_string(payload)

    def test_require_rejects_empty_value(self):
        with patch.dict(bootstrap.os.environ, {"RENDER_API_KEY": ""}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "RENDER_API_KEY"):
                bootstrap._require("RENDER_API_KEY")

    def test_set_env_var_does_not_log_secret(self):
        with patch.object(bootstrap, "_request_json") as request:
            bootstrap.set_env_var("api-key", "srv-1", "NOTION_TOKEN", "very-secret")
        request.assert_called_once_with(
            "PUT",
            "/services/srv-1/env-vars/NOTION_TOKEN",
            "api-key",
            {"value": "very-secret"},
        )


if __name__ == "__main__":
    unittest.main()
