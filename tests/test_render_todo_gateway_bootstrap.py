import unittest
from unittest.mock import call, patch

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

    def test_main_allows_missing_notion_token(self):
        env = {
            "RENDER_API_KEY": "api-key",
            "RENDER_SERVICE_ID": "srv-1",
            "RENDER_POSTGRES_ID": "pg-1",
            "TODO_GATEWAY_URL": "https://gateway.example",
            "RENDER_DEPLOY_TIMEOUT_SECONDS": "1",
        }
        with (
            patch.dict(bootstrap.os.environ, env, clear=True),
            patch.object(
                bootstrap,
                "_request_json",
                side_effect=[
                    {"internalConnectionString": "postgresql://internal.example/db"},
                    {"id": "dep-1"},
                ],
            ),
            patch.object(bootstrap, "set_env_var") as set_env,
            patch.object(bootstrap, "_poll_deploy", return_value="live"),
            patch.object(bootstrap, "_check_readyz") as check_readyz,
        ):
            self.assertEqual(bootstrap.main(), 0)

        set_env.assert_called_once_with(
            "api-key",
            "srv-1",
            "DATABASE_URL",
            "postgresql://internal.example/db",
        )
        check_readyz.assert_called_once_with("https://gateway.example")

    def test_main_configures_notion_only_when_token_exists(self):
        env = {
            "RENDER_API_KEY": "api-key",
            "RENDER_SERVICE_ID": "srv-1",
            "RENDER_POSTGRES_ID": "pg-1",
            "NOTION_TOKEN": "notion-secret",
            "NOTION_DATA_SOURCE_ID": "ds-1",
            "TODO_GATEWAY_URL": "https://gateway.example",
            "RENDER_DEPLOY_TIMEOUT_SECONDS": "1",
        }
        with (
            patch.dict(bootstrap.os.environ, env, clear=True),
            patch.object(
                bootstrap,
                "_request_json",
                side_effect=[
                    {"internalConnectionString": "postgresql://internal.example/db"},
                    {"id": "dep-1"},
                ],
            ),
            patch.object(bootstrap, "set_env_var") as set_env,
            patch.object(bootstrap, "_poll_deploy", return_value="live"),
            patch.object(bootstrap, "_check_readyz"),
        ):
            self.assertEqual(bootstrap.main(), 0)

        self.assertEqual(
            set_env.call_args_list,
            [
                call("api-key", "srv-1", "DATABASE_URL", "postgresql://internal.example/db"),
                call("api-key", "srv-1", "NOTION_TOKEN", "notion-secret"),
                call("api-key", "srv-1", "NOTION_DATA_SOURCE_ID", "ds-1"),
            ],
        )

    def test_main_rejects_notion_token_without_data_source(self):
        env = {
            "RENDER_API_KEY": "api-key",
            "RENDER_SERVICE_ID": "srv-1",
            "RENDER_POSTGRES_ID": "pg-1",
            "NOTION_TOKEN": "notion-secret",
            "TODO_GATEWAY_URL": "https://gateway.example",
        }
        with patch.dict(bootstrap.os.environ, env, clear=True):
            with self.assertRaisesRegex(RuntimeError, "NOTION_DATA_SOURCE_ID"):
                bootstrap.main()


if __name__ == "__main__":
    unittest.main()
