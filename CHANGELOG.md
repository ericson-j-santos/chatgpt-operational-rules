# Changelog

## 1.5.0 - 2026-09-11
- Adicionado bootstrap inicial de host novo, restrito ao repositório canônico e SHA completo aprovado.
- Instalador valida SHA-256 do próprio script, `MANIFEST.json`, tamanho e hash dos quatro arquivos de runtime antes da instalação.
- Instalação gera backup quando aplicável, recibo auditável e cria namespace `C:\\dev\\chatgpt-workers`.
- Bootstrap cria repositório de validação no commit exato aprovado para permitir o primeiro `session_preflight.py` sem terminal genérico.
- Adicionados testes unitários e E2E específicos do bootstrap de host.

## 1.4.1 - 2026-09-11
- Fingerprint Git passa a incorporar conteúdo efetivo dos arquivos rastreados modificados, além de índice, status e não rastreados.
- Corrigida decodificação de subprocessos para UTF-8 com substituição segura, evitando falha CP1252 no Windows.
- Bootstrap detecta caminhos rastreados que diferem apenas por maiúsculas/minúsculas e bloqueia materialização com diagnóstico explícito.
- Adicionados testes unitários e E2E para mutação de conteúdo já dirty, UTF-8 e colisões de casing.

## 1.4.0 - 2026-09-10
- Adicionado `C:\\dev\\chatgpt-workers\\*` à allowlist do Command Gateway para múltiplas frentes isoladas.
- Definido o namespace de workers como destino preferencial para OCR, Portal Portabilidade e futuros projetos autorizados.
- Mantidos bloqueados o perfil do usuário, o volume `D:` e clones existentes fora da allowlist.
- Proibida a adoção automática de clones com alterações locais preexistentes; workers devem partir de referência canônica limpa.
- Adicionados testes de política para comprovar que o namespace de workers é permitido sem liberar os clones do Portal no perfil do usuário ou no `D:`.
- Preservada a lógica `git_untracked_excludes` introduzida na versão 1.3.1.
- Adicionado `session_preflight.py` para captura automática e estável de host, regras, branch, SHA, digest Git, reserva e worktree.
- Snapshot de preflight passa a ter SHA-256 próprio e é pré-condição do gateway quando habilitado.
- Auditoria positiva do gateway passa a carregar `session_id`; risco 2 continua restrito ao worktree materializado da sessão.
- Tornado obrigatório o bootstrap de sessão antes do primeiro comando local/remoto de cada chat/agente.
- Proibido fallback direto para PowerShell, CMD, Bash, WSL, SSH ou terminal irrestrito quando bootstrap/gateway bloquear ou estiver indisponível.
- Adicionada reserva idempotente de worktree por `session_id`, persistida fora do working tree.
- Adicionada materialização opcional com `git worktree add --detach` no SHA capturado no bootstrap.
- Alterações de risco 2 passam a exigir worktree reservado/materializado da sessão por regra operacional.
- Adicionados testes unitários e E2E para idempotência, conflito de sessão, identificador inválido, política desabilitada e worktree isolado.
- Adicionado gerador determinístico de `MANIFEST.json` para reduzir divergência de hash/tamanho durante evoluções das regras.

## 1.3.1 - 2026-09-10
- Corrigida inspeção Git do Command Gateway para separar estado rastreado e não rastreado.
- Adicionado `git_untracked_excludes` para áreas temporárias explicitamente excluídas, sem reduzir a enumeração dos demais não rastreados.
- Mantido bloqueio quando qualquer comando usado na inspeção Git produz aviso/erro em `stderr`.
- Adicionado teste que prova que alterações não rastreadas fora das exclusões continuam detectáveis.

## 1.3.0 - 2026-09-10
- Adicionado Command Gateway local para execução governada de comandos em máquinas autorizadas.
- Adicionada allowlist explícita para ReqSys e worktrees em `C:\\dev`, com bloqueio de perfil do usuário e caminhos sensíveis.
- Adicionado lock exclusivo por repositório, `correlation_id`, log JSONL mascarado e validação de HEAD/estado Git antes e depois.
- Operações de risco 3 são bloqueadas pelo gateway; risco 2 exige árvore limpa por padrão e não permite mudança de HEAD sem autorização explícita.
- Adicionados testes unitários e E2E com casos negativos para diretório bloqueado, lock concorrente, referência sensível, HEAD divergente e mutação indevida em risco 1.
- Gate do GitHub Actions atualizado para executar validação de regras, testes do gateway e E2E contra falso positivo.

## 1.2.0 - 2026-09-10
- Tornada obrigatória a validação ponta a ponta para incrementos funcionais criados ou modificados.
- Adicionados controles explícitos contra falso positivo: pré-condição, marcador único, caso positivo, caso negativo/controle, leitura independente e vínculo com branch/SHA/ambiente.
- Adicionado teste do próprio teste quando seguro e viável para comprovar que o mecanismo detecta falhas.
- Proibido declarar conclusão funcional quando a validação E2E aplicável estiver bloqueada ou baseada em evidência residual/ambígua.
- Adicionado gate automático do repositório com validação de manifesto e autotestes negativos do próprio validador.
- Padronizados fins de linha em LF para manter hashes determinísticos entre Windows e Linux.

## 1.1.0 - 2026-09-10
- Adicionada política de execução governada de terminal.
- Definidos níveis 🟢/🟡/🔴 para observação, alteração reversível e operações críticas.
- Adicionadas proteções para segredos, privilégios, comandos destrutivos e execução remota.
- Definidas evidências mínimas e critérios de conclusão para comandos executados por chats e agentes.

## 1.0.0 - 2026-09-09
- Criação da fonte canônica de regras operacionais.
- Política de armazenamento de artefatos no Google Drive.
- Regras de evidência, GitHub/CI, segurança, relatórios e engenharia.
- Regra de bootstrap para novos chats/agentes.
- Regras específicas iniciais para ReqSys, ocr_evidencia e BACEN.
- Proteção contra publicação de identificadores privados em repositório público.
