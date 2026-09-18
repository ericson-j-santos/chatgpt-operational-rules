import unittest

from services.gitlab_webhook_relay.service_main import FORWARDED_HEADERS, valid_target


class RelayContractTest(unittest.TestCase):
    def test_preserves_retry_idempotency_headers(self):
        self.assertIn("Webhook-Id", FORWARDED_HEADERS)
        self.assertIn("Idempotency-Key", FORWARDED_HEADERS)
        self.assertIn("X-Gitlab-Event-UUID", FORWARDED_HEADERS)

    def test_allows_only_dev_quick_tunnel_downstream(self):
        self.assertEqual(
            valid_target("https://example-test.trycloudflare.com/"),
            "https://example-test.trycloudflare.com",
        )
        with self.assertRaises(ValueError):
            valid_target("http://example-test.trycloudflare.com")
        with self.assertRaises(ValueError):
            valid_target("https://example.com")


if __name__ == "__main__":
    unittest.main()
