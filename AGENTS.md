# AGENTS.md

Este repositório é a fonte canônica de regras operacionais do usuário.

Antes de executar trabalho técnico:
1. leia `README.md`;
2. identifique e leia as regras em `rules/` aplicáveis;
3. se houver regra do projeto em `projects/`, leia-a;
4. respeite a ordem de precedência definida no README;
5. para incrementos funcionais criados ou modificados, aplique `rules/e2e-validation.md`;
6. para terminal local/remoto, aplique `rules/terminal-execution.md`, `rules/command-gateway.md` e `rules/session-bootstrap.md`;
7. antes do primeiro comando local/remoto, execute `scripts/session_bootstrap.py` e exija `BOOTSTRAP_OK`;
8. depois do bootstrap, todo comando deve passar pelo Command Gateway; Remote Desktop Commander é apenas transporte;
9. nunca usar PowerShell, CMD, Bash, WSL, SSH ou terminal irrestrito como fallback para contornar bootstrap/gateway;
10. para alteração de risco 2, use o worktree reservado/materializado para a sessão;
11. valide evidências e controles contra falso positivo antes de declarar sucesso.

Se bootstrap, gateway, política, reserva ou validação falhar, interrompa a execução e reporte o bloqueio. Não contorne o controle.

Nunca registre segredos, tokens, credenciais ou dados confidenciais neste repositório.
