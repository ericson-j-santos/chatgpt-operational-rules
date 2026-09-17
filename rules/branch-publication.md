# Publicação governada de branch

## Objetivo
Permitir a publicação inicial de uma branch de trabalho sem liberar `git push` genérico, force-push, branch protegida ou atualização destrutiva de referência.

## Regra
O `git push` genérico continua bloqueado pelo Command Gateway. A única exceção de publicação local é `scripts/publish_branch_governed.py`, executado como ação de risco 2 dentro de uma sessão válida e de um worktree reservado/materializado.

A ação deve exigir simultaneamente:
- `expected-head` com SHA completo e igual ao HEAD local;
- working tree limpo;
- nome da branch local exatamente igual ao destino;
- destino fora da lista de branches protegidas;
- remoto `origin` já configurado pelo repositório;
- inexistência da branch remota, ou o mesmo SHA para repetição idempotente;
- ausência de `--force`, refspec curinga, exclusão ou atualização de branch remota existente;
- leitura independente por `ls-remote` depois da publicação.

Se a branch remota existir em SHA diferente, a ação deve falhar fechada. Atualização de branch já publicada permanece fora deste fluxo até existir política específica.

## Fluxo obrigatório
1. `session_launcher.py` / `session_preflight.py` retorna sessão válida.
2. Confirmar `target_path`, `session_id`, HEAD e árvore limpa.
3. Executar pelo `command_gateway.py` com risco 2 apenas o script versionado `publish_branch_governed.py`.
4. Confirmar o SHA remoto por leitura independente.
5. Usar API/plugin GitHub para abrir o PR.
6. Não fazer merge, force-push ou alteração de branch protegida sem autorização específica aplicável.

## Critério de conclusão
A publicação está concluída somente quando o SHA remoto coincide com `expected-head`, a repetição é idempotente e os testes negativos comprovam bloqueio para branch protegida, árvore suja e branch remota divergente.
