# ChatGPT Operational Rules

Versão: 1.4.0

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
9. Para comandos executados na máquina autorizada, aplicar `rules/command-gateway.md`.
10. Antes do primeiro comando local/remoto de cada chat/agente, aplicar `rules/session-bootstrap.md`, executar `scripts/session_preflight.py` e exigir `BOOTSTRAP_OK` com `state_validated=true`.
11. Após o bootstrap, não usar terminal direto como fallback; Remote Desktop Commander é apenas transporte para bootstrap/gateway.
12. Para alterações de risco 2, materializar e usar o worktree reservado da sessão.

## Projetos conhecidos
- ReqSys
- ocr_evidencia
- BACEN

## Política de artefatos
Google Drive é o armazenamento primário para arquivos gerados.
GitHub é usado para código, testes, CI/CD, infraestrutura como código e documentação versionada.

## Segurança
Este repositório pode ser público. Não registrar nele segredos, tokens, credenciais, identificadores privados de armazenamento, conteúdo confidencial ou dados pessoais desnecessários.
