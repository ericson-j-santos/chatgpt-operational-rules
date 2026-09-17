import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "todo_gateway_cloudflare_quick_tunnel_e2e.py"


class CloudflareQuickTunnelTests(unittest.TestCase):
    def test_quick_tunnel_is_pinned_temporary_and_cleaned_up(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('CLOUDFLARED_IMAGE = "cloudflare/cloudflared:2026.9.1"', text)
        self.assertIn('DOCKER_NETWORK = "todo-global-24x7_default"', text)
        self.assertIn('GATEWAY_ORIGIN = "http://gateway:8000"', text)
        self.assertIn("trycloudflare\\.com", text)
        self.assertIn('"temporary": True', text)
        self.assertIn('run("rm", "-f", container', text)
        self.assertIn('"public_route_active_after_test"] = False', text)

    def test_e2e_keeps_secret_out_of_output_and_proves_controls(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('values.get("GITLAB_WEBHOOK_TOKEN", "")', text)
        self.assertNotIn('print(token)', text)
        self.assertNotIn('"token": token', text)
        self.assertIn('denied == 401', text)
        self.assertIn('first_status == 202', text)
        self.assertIn('replay.get("duplicate") is True', text)
        self.assertIn("sql_event(event_id)", text)
        self.assertIn('"sql_readback": persisted', text)


if __name__ == "__main__":
    unittest.main()
