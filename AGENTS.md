# AGENTS.md

Este repositório é a fonte canônica de regras operacionais do usuário.

Antes de executar trabalho técnico:
1. leia `README.md`;
2. identifique e leia as regras em `rules/` aplicáveis;
3. se houver regra do projeto em `projects/`, leia-a;
4. respeite a ordem de precedência definida no README;
5. para incrementos funcionais criados ou modificados, aplique `rules/e2e-validation.md`;
6. para TODOs operacionais, aplique `rules/todo-global.md`; quando o caminho assíncrono existir, publique o evento e não trate aceitação na fila como conclusão;
7. para terminal local/remoto, aplique `rules/terminal-execution.md`, `rules/command-gateway.md` e `rules/session-bootstrap.md`;
8. antes do primeiro comando local/remoto, execute `scripts/session_preflight.py` e exija `BOOTSTRAP_OK` com `state_validated=true`;
9. depois do bootstrap, todo comando deve passar pelo Command Gateway; Remote Desktop Commander é apenas transporte;
10. nunca usar PowerShell, CMD, Bash, WSL, SSH ou terminal irrestrito como fallback para contornar bootstrap/gateway;
11. para alteração de risco 2, use o worktree reservado/materializado para a sessão;
12. aplique `rules/progress-watchdog.md`: liveness não é progresso; heartbeat, lease e polling repetido não reiniciam o relógio; após o limite, reroteie ou bloqueie explicitamente.
13. valide evidências e controles contra falso positivo antes de declarar sucesso.

Exceção única para host novo sem Gateway instalado: usar `scripts/install_command_gateway_host.py` com SHA completo aprovado e hash próprio esperado; exigir `HOST_BOOTSTRAP_OK` e então passar imediatamente ao `session_preflight.py`. Não usar essa exceção como terminal genérico.

Se bootstrap, gateway, política, reserva ou validação falhar, interrompa a execução e reporte o bloqueio. Não contorne o controle.

Nunca registre segredos, tokens, credenciais ou dados confidenciais neste repositório.
