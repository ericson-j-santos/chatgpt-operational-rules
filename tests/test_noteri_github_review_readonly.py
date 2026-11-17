"""Testes isolados, sem rede/host/segredos e sem aplicar manutenção."""
import sys
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import noteri_github_review_readonly as m

NOW = datetime(2026, 11, 16, 21, 0, 0, tzinfo=timezone.utc)
SHA = "a" * 40
RID = 12345
ENV_ID = 6789


def amostra():
    run = {"id": RID, "repository": {"full_name": m.REPOSITORIO},
           "actor": {"id": m.USUARIO_PROPRIETARIO_ID},
           "event": "workflow_dispatch", "path": m.WORKFLOW,
           "head_branch": "main", "head_sha": SHA, "run_attempt": 1,
           "status": "completed", "conclusion": "success",
           "created_at": (NOW - timedelta(minutes=5)).isoformat()}
    reviews = [{"state": "approved", "user": {"id": m.USUARIO_PROPRIETARIO_ID},
                "environments": [{"id": ENV_ID, "name": m.AMBIENTE}]}]
    env = {"id": ENV_ID, "name": m.AMBIENTE, "can_admins_bypass": False,
           "protection_rules": [{"type": "required_reviewers", "reviewers": [
               {"type": "User", "reviewer": {"id": m.USUARIO_PROPRIETARIO_ID}}]}]}
    return run, reviews, env


def validar(run=None, reviews=None, env=None):
    a, b, c = amostra()
    return m.validar_evidencia(execucao=a if run is None else run,
                              revisoes=b if reviews is None else reviews,
                              ambiente=c if env is None else env,
                              run_id=RID, sha_workflow_instalado=SHA, agora=NOW)


class ReviewReadOnlyTests(unittest.TestCase):
    def test_prova_externa_valida_nunca_concede_execucao(self):
        d = validar()
        self.assertEqual(d["result"], "REVIEW_PROVENANCE_VERIFIED")
        self.assertTrue(d["review_provenance_verified"])
        self.assertFalse(d["execution_authorized"])
        self.assertFalse(d["state_changed"])

    def test_revisao_concluida_success_comprova_sem_autorizar_manutencao(self):
        run, _, _ = amostra()
        run["status"] = "completed"
        run["conclusion"] = "success"
        result = validar(run=run)
        self.assertEqual(result["result"], "REVIEW_PROVENANCE_VERIFIED")
        self.assertIs(result["execution_authorized"], False)

    def test_revisao_em_andamento_nao_comprova_resultado_final(self):
        run, _, _ = amostra()
        run["status"] = "in_progress"
        run["conclusion"] = None
        self.assertEqual(validar(run=run)["result"], "BLOCKED")

    def test_revisao_concluida_com_erro_bloqueia(self):
        run, _, _ = amostra()
        run["status"] = "completed"
        run["conclusion"] = "failure"
        self.assertEqual(validar(run=run)["result"], "BLOCKED")

    def test_sem_approval_bloqueia(self):
        self.assertEqual(validar(reviews=[])["reason"], "aprovacao_unica_ausente")

    def test_texto_nao_autentica_proprietario(self):
        _, reviews, _ = amostra()
        reviews[0]["user"] = {"id": 123}
        reviews[0]["comment"] = "AUTORIZADO Noteri pelo proprietário"
        self.assertEqual(validar(reviews=reviews)["reason"], "aprovacao_unica_ausente")

    def test_aprovacao_de_ambiente_errado_bloqueia(self):
        _, reviews, _ = amostra()
        reviews[0]["environments"] = [{"id": ENV_ID + 1, "name": m.AMBIENTE}]
        self.assertEqual(validar(reviews=reviews)["result"], "BLOCKED")

    def test_run_forjado_sha_errado_bloqueia(self):
        run, _, _ = amostra()
        run["head_sha"] = "b" * 40
        self.assertEqual(validar(run=run)["reason"], "identidade_da_execucao_divergente")

    def test_run_reexecutado_nao_reaproveita_review(self):
        run, _, _ = amostra()
        run["run_attempt"] = 2
        self.assertEqual(validar(run=run)["result"], "BLOCKED")

    def test_approval_rejeitada_posteriormente_bloqueia(self):
        _, reviews, _ = amostra()
        reviews.append({"state": "rejected", "user": {"id": m.USUARIO_PROPRIETARIO_ID},
                        "environments": [{"id": ENV_ID, "name": m.AMBIENTE}]})
        self.assertEqual(validar(reviews=reviews)["reason"], "historico_rejeitado")

    def test_fora_janela_bloqueia(self):
        run, _, _ = amostra()
        run["created_at"] = (NOW - timedelta(hours=1)).isoformat()
        self.assertEqual(validar(run=run)["reason"], "execucao_fora_da_janela")

    def test_reviewers_nao_configurados_bloqueia(self):
        _, _, env = amostra()
        env["protection_rules"] = []
        self.assertEqual(validar(env=env)["reason"], "proprietario_nao_e_revisor_exigido")

    def test_bypass_administrador_bloqueia(self):
        _, _, env = amostra()
        env["can_admins_bypass"] = True
        self.assertEqual(validar(env=env)["reason"], "ambiente_sem_protecao_estrita")

    def test_protecao_ausente_bloqueia(self):
        _, _, env = amostra()
        env.pop("can_admins_bypass")
        self.assertEqual(validar(env=env)["result"], "BLOCKED")

    def test_job_de_outro_workflow_bloqueia(self):
        run, _, _ = amostra()
        run["path"] = ".github/workflows/outro.yml"
        self.assertEqual(validar(run=run)["result"], "BLOCKED")

    def test_ator_errado_bloqueia(self):
        run, _, _ = amostra()
        run["actor"]["id"] = 22
        self.assertEqual(validar(run=run)["result"], "BLOCKED")

    def test_sem_run_id_valido_nao_faz_chamada_api(self):
        calls = []
        with self.assertRaisesRegex(m.RecusaDeAutorizacao, "run_id_invalido"):
            m.consultar_evidencia_github(0, leitor=lambda u: calls.append(u))
        self.assertEqual(calls, [])

    def test_api_apenas_tres_endpoints_fixos(self):
        urls = []
        def leitor(url):
            urls.append(url)
            return {}
        m.consultar_evidencia_github(RID, leitor=leitor)
        self.assertEqual(len(urls), 3)
        self.assertTrue(all(u.startswith("https://api.github.com/repos/"+m.REPOSITORIO+"/") for u in urls))
        self.assertTrue(urls[1].endswith("/actions/runs/12345/approvals"))

    def test_dois_approvals_mesmo_job_bloqueia_ambiguidade(self):
        _, reviews, _ = amostra()
        reviews.append(deepcopy(reviews[0]))
        self.assertEqual(validar(reviews=reviews)["reason"], "aprovacao_unica_ausente")


if __name__ == "__main__":
    unittest.main()
