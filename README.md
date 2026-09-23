# ChatGPT Operational Rules

Versão: 1.6.6

Fonte canônica de regras operacionais para trabalhos executados com ChatGPT e agentes conectados aos projetos do usuário.

## Ordem de precedência
1. Instrução explícita do usuário no chat atual.
2. Regra específica do projeto.
3. Regra operacional global deste repositório.
4. Convenção padrão da ferramenta/modelo.

## Fluxo obrigatório
1. Identificar fonte e estado atual.
2. Executar as ações disponíveis.
3. Validar evidências e consistência.
4. Tratar bloqueios e alternativas seguras.
5. Retornar estado evidenciado, riscos, bloqueios e próximo passo.
6. Para artefatos, aplicar `rules/artifact-storage.md`.
7. Para execução de comandos locais ou remotos, aplicar `rules/terminal-execution.md`.
8. Para todo incremento funcional criado ou modificado, aplicar `rules/e2e-validation.md` e não declarar conclusão sem validação ponta a ponta aplicável e controles contra falso positivo.
9. Para TODOs operacionais, aplicar `rules/todo-global.md`: publicar mudanças pelo caminho assíncrono quando disponível, preservar `event_id`, `correlation_id` e `idempotency_key`, manter o TODO Global como fonte canônica e usar reconciliação periódica apenas como rede de segurança.
10. Para comandos executados na máquina autorizada, aplicar `rules/command-gateway.md`.
11. Antes do primeiro comando local/remoto de cada chat/agente, aplicar `rules/session-bootstrap.md`; preferir `scripts/session_launcher.py`, que materializa o preflight e exige `SESSION_LAUNCH_OK` com `state_validated=true`.
12. Para múltiplos chats/agentes, usar workspaces isolados sob `C:\\dev\\chatgpt-workers\\*`; nunca compartilhar o mesmo working tree entre workers.
13. Após o bootstrap, não usar terminal direto como fallback; Remote Desktop Commander é apenas transporte para preflight/gateway.
14. Para alterações de risco 2, materializar e usar o worktree reservado da sessão.
15. Para seleção de infraestrutura/runtime, aplicar `rules/runtime-routing.md`: avaliar e reaproveitar o `PC24x7 Desktop` antes de criar runtime pago adicional quando o workload for compatível; regras específicas de projeto/ambiente e requisitos objetivos de SLA, segurança, continuidade ou regulação podem determinar outro runtime. No TODO Global / AI Control Plane o PC24x7 permanece canônico, Render é somente contingência manual e Fly.io não é rota desse projeto.
16. Durante desenvolvimento até o estado **padrão ouro**, Risk 3 pode usar o modo temporário descrito em `docs/OWNER_RISK3_EXCEPTION.md`: somente `local/DEV`, janela máxima de 30 dias, script Python versionado, worktree limpo e auditoria obrigatória. A exigência normal da allowlist deve ser reativada antes de HML/STG/PROD e essa reativação faz parte do critério de conclusão.
17. Quando houver dois ou mais hosts disponíveis para desenvolvimento, aplicar `rules/dual-host-routing.md` e executar `scripts/dual_host_preflight.py` antes de distribuir a tarefa; cada worker deve manter sessão, correlação e worktree exclusivos, com decisão fail-closed diante de host degradado.
18. Para seleção de ferramenta/executor, aplicar `rules/tool-routing.md` e `scripts/tool_router.py`: plugin/API e web nativa ficam no caminho normal; TinyFish é contingência com saldo positivo; Remote Desktop Commander é reservado para ações que dependem da máquina física, especialmente quando a cota restante estiver em modo de reserva.
19. Para Remote Desktop Commander, sucesso técnico da chamada nunca substitui sucesso funcional: normalizar o resultado com `scripts/rdc_semantic_result.py`, exigir `controller_semantic_ok=true` no roteamento e usar recuperação fail-fast com circuit breaker persistente; `NO_CALLBACK`, efeitos vazios/não comprovados e `SESSION_LAUNCH_BLOCKED` devem falhar fechado e suprimir retries até nova sonda governada.
20. Para chats, agentes, workers e automações, aplicar `rules/progress-watchdog.md`: heartbeat, renovação de lease e polling sem mudança não contam como progresso; após 300 segundos (5 minutos) sem evidência material, rerotear quando houver alternativa segura ou bloquear/liberar capacidade quando não houver. Usar `scripts/progress_watchdog.py` para decisão reproduzível.

Host novo sem Gateway: executar exclusivamente `scripts/install_command_gateway_host.py` com SHA completo aprovado e SHA-256 esperado do próprio instalador. Se Git não estiver instalado em Windows, o próprio bootstrap pode provisionar a distribuição oficial MinGit fixada por versão, tamanho e SHA-256, sem alterar o `PATH` global. Após `HOST_BOOTSTRAP_OK`, seguir imediatamente para `scripts/session_preflight.py`; não usar o bootstrap como terminal genérico.

## Projetos conhecidos
- ReqSys Produto — prioridade principal P0 enquanto não houver regressão sistêmica do plano de controle.
- Engineering Control Plane — melhoria contínua; pode reassumir P0 temporário somente diante de gargalo sistêmico comprovado que afete múltiplos repositórios.
- Runtime Platform / PC24x7 — infraestrutura física/runtime sob demanda por bloqueio real; PC24x7-first, fail-closed e sem absorver lógica de produto ou CI transversal.
- ocr_evidencia
- BACEN

## Política de artefatos
Google Drive é o armazenamento primário para arquivos gerados.
GitHub é usado para código, testes, CI/CD, infraestrutura como código e documentação versionada.

## Segurança
Este repositório pode ser público. Não registrar nele segredos, tokens, credenciais, identificadores privados de armazenamento, conteúdo confidencial ou dados pessoais desnecessários.

O modo temporário Risk 3 de desenvolvimento **não** libera HML/STG/PROD, operações destrutivas, reboot/shutdown, billing, administração ampla de RBAC ou passagem explícita de segredos. Ele remove apenas o atrito de cadastrar cada `action_id` durante o ciclo local/DEV controlado.
