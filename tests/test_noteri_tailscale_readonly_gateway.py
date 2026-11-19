"""PASS-001: governança Tailscale estritamente read-only no Noteri."""
import json
import unittest
from pathlib import Path
from unittest import mock
import subprocess

from scripts import command_gateway as cg

EXE = r"C:\Program Files\Tailscale\tailscale.exe"
DIGEST = "e10279fa2f34996b93525484f14984e8a1afa5ccf713d234f7db9aa33873b007"
COMMANDS = [
    [EXE, "status", "--json"],
    [EXE, "serve", "status", "--json"],
    [EXE, "serve", "get-config", "--all"],
]


def policy():
    return {
        "version": 1,
        "platform": "windows",
        "allowed_executables": ["python", "git"],
        "blocked_executables": ["tailscale", "cmd", "powershell"],
        "noteri_tailscale_readonly_diagnostic": True,
        "noteri_tailscale_binary_sha256": DIGEST,
    }


class TailscaleNoteriGate(unittest.TestCase):
    def setUp(self):
        self.stack = unittest.mock.ExitStack() if hasattr(unittest.mock, "ExitStack") else None
        self.patches = [
            mock.patch.object(cg, "wsl_host_context", return_value=("nt", "Noteri")),
            mock.patch.object(Path, "is_symlink", return_value=False),
            mock.patch.object(Path, "resolve", return_value=Path(EXE)),
            mock.patch.object(cg, "sha256_file", return_value=DIGEST),
        ]
        for patch in self.patches:
            patch.start()
        self.addCleanup(lambda: [p.stop() for p in reversed(self.patches)])

    def test_exatamente_tres_queries_sao_admitidas(self):
        for cmd in COMMANDS:
            with self.subTest(cmd=cmd[1:]):
                cg.validate_command(cmd, 1, policy())
        self.assertEqual(
            cg.tailscale_validate_request(COMMANDS[2], 1, policy()), "SERVE_ALL")

    def test_qualquer_mutacao_ou_atalho_e_recusado(self):
        candidates = [
            [EXE, "up"], [EXE, "down"], [EXE, "logout"], [EXE, "login"],
            [EXE, "serve", "--bg", "--tcp=443", "tcp://127.0.0.1:8765"],
            [EXE, "serve", "reset"], [EXE, "funnel", "443", "on"],
            [EXE, "status", "--json", "--peers=false"],
            [EXE, "serve", "status", "--json", "--all"],
            [EXE, "serve", "get-config", "--all", "--output=somewhere"],
            ["tailscale.exe", "status", "--json"],
            [r"C:\dev\chatgpt-workers\tailscale.exe", "status", "--json"],
            [r"\\server\share\tailscale.exe", "status", "--json"],
            [EXE.upper(), "STATUS", "--json"],
        ]
        for cmd in candidates:
            with self.subTest(cmd=cmd):
                with self.assertRaises(cg.GatewayError):
                    cg.validate_command(cmd, 1, policy())
        for command in COMMANDS:
            for risk in (2, 3):
                with self.assertRaises(cg.GatewayError):
                    cg.validate_command(command, risk, policy())

    def test_policy_explicitamente_opt_in_e_hash_pinned(self):
        for field, invalid in [
            ("noteri_tailscale_readonly_diagnostic", False),
            ("noteri_tailscale_readonly_diagnostic", None),
            ("noteri_tailscale_binary_sha256", "0" * 64),
            ("noteri_tailscale_binary_sha256", ""),
        ]:
            with self.subTest(field=field, invalid=invalid):
                p = policy()
                p[field] = invalid
                with self.assertRaises(cg.GatewayError):
                    cg.validate_command(COMMANDS[0], 1, p)
        for missing in ("noteri_tailscale_readonly_diagnostic", "noteri_tailscale_binary_sha256"):
            p = policy()
            p.pop(missing)
            with self.assertRaises(cg.GatewayError):
                cg.validate_command(COMMANDS[0], 1, p)

    def test_outro_host_plataforma_e_arquivo_alterado_bloqueiam(self):
        for platform, host in (("nt", "OtherPC"), ("posix", "Noteri")):
            with mock.patch.object(cg, "wsl_host_context", return_value=(platform, host)):
                with self.assertRaises(cg.GatewayError):
                    cg.validate_command(COMMANDS[0], 1, policy())
        with mock.patch.object(cg, "sha256_file", return_value="f" * 64):
            with self.assertRaises(cg.GatewayError):
                cg.validate_command(COMMANDS[0], 1, policy())
        with mock.patch.object(Path, "is_symlink", return_value=True):
            with self.assertRaises(cg.GatewayError):
                cg.validate_command(COMMANDS[0], 1, policy())

    def test_cargas_status_nao_expõem_identidades_e_tokens(self):
        bruto = json.dumps({
            "BackendState": "Running",
            "Self": {"Online": True, "DNSName": "noteri.minharede.ts.net.",
                     "UserID": 1, "PublicKey": "tskey-auth-SIGILO"},
            "Peer": {"someid": {"HostName": "NOME_PRIVADO", "TailscaleIPs": ["100.100.100.1"]}},
            "AuthURL": "",
        }).encode()
        result = cg.tailscale_sanitize_output("STATUS", 0, bruto, b"")
        texto = json.dumps(result)
        self.assertTrue(result["backend_running"])
        self.assertTrue(result["device_online"])
        self.assertTrue(result["dns_ts_net_present"])
        for secreto in ("NOME_PRIVADO", "tskey-auth", "100.100.100.1", "minharede"):
            self.assertNotIn(secreto, texto)
        self.assertFalse(result["deploy_autorizado"])
        self.assertFalse(result["custo_zero_validado"])

    def test_serve_conflitos_e_funnel_sem_metadata_sensivel(self):
        cfg = {"TCP": {"443": {"TCPForward": "127.0.0.1:8765", "Note": "SEGREDO"}},
               "AllowFunnel": {"noteri.minharede.ts.net:8443": True}}
        result = cg.tailscale_sanitize_output("SERVE_STATUS", 0, json.dumps(cfg).encode(), b"")
        self.assertTrue(result["conflict_tcp443_or_web443"])
        self.assertTrue(result["funnel_active_or_unrecognized"])
        self.assertFalse(result["serve_sem_conflito_comprovado"])
        self.assertNotIn("SEGREDO", json.dumps(result))
        clean = cg.tailscale_sanitize_output("SERVE_STATUS", 0, b'{"TCP":{},"Web":{},"AllowFunnel":{}}', b"")
        self.assertTrue(clean["serve_sem_conflito_comprovado"])
        self.assertFalse(clean["deploy_autorizado"])

    def test_get_config_all_nao_afirma_autorizacao(self):
        result = cg.tailscale_sanitize_output("SERVE_ALL", 0, b'{"Services":{"svc:xxx":{"Secret":"SENHA"}}}', b"")
        self.assertTrue(result["schema_unknown_or_unverified_services"])
        self.assertFalse(result["serve_sem_conflito_comprovado"])
        self.assertNotIn("SENHA", json.dumps(result))

    def test_erros_duplicatas_e_estouro_sao_fechados(self):
        invalids = [
            (0, b'{"BackendState":"Running","BackendState":"Stopped"}', b""),
            (0, b"<html>error</html>", b""),
            (0, b"{" + b"x" * (cg.TAILSCALE_MAX_BYTES + 1), b""),
            (0, b'{"BackendState":"Running"}', b"warning PRIVADO"),
            (1, b'{"BackendState":"Running"}', b""),
        ]
        for rc, out, err in invalids:
            with self.subTest(rc=rc, size=len(out)):
                with self.assertRaises(cg.TailscaleProbeBlocked):
                    cg.tailscale_sanitize_output("STATUS", rc, out, err)

    def test_runner_usa_exato_argv_sem_stdout_bruto(self):
        def fake_run(argv, **kwargs):
            self.assertEqual(argv, COMMANDS[0])
            self.assertFalse(kwargs["shell"])
            self.assertEqual(kwargs["timeout"], 12)
            return subprocess.CompletedProcess(argv, 0, b'{"BackendState":"Running","Self":{"Online":true}}', b"")
        result = cg.tailscale_run_fixed(
            COMMANDS[0], 1, policy(), cwd=r"C:\dev\chatgpt-workers\wt", timeout=60,
            runner=fake_run)
        self.assertEqual(result.returncode, 0)
        self.assertTrue(json.loads(result.stdout)["backend_running"])

    def test_runner_erro_nao_vaza_stderr(self):
        result = cg.tailscale_run_fixed(
            COMMANDS[1], 1, policy(), cwd=r"C:\dev\chatgpt-workers\wt", timeout=10,
            runner=lambda argv, **kw: subprocess.CompletedProcess(
                argv, 1, b"", b"token: SIGILO"))
        self.assertEqual(result.returncode, 23)
        self.assertNotIn("SIGILO", result.stdout)


if __name__ == "__main__":
    unittest.main()
