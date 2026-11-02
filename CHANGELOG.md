# Changelog

## Em preparação - 2026-10-30

- Formalizada a contingência gratuita de monitoramentos quando não é possível agendar no ChatGPT: reaproveitamento primeiro, GitHub Actions público com runner padrão em seguida, host governado por último e bloqueio sem prova de custo zero, com deduplicação, teste e readback (issue #191). Sem novo agendamento ou alteração automática de sistema.

## 1.6.19 - 2026-10-25

- Adicionado o contrato global para reparo real de agentes: patch persistido no CAS, validação independente, uma PR draft e readback/replay idempotente.
- Preservado integralmente o gate universal de 100% de controles aplicáveis da versão 1.6.18.

## 1.6.18 - 2026-10-25

- Critério universal de 100% dos gates obrigatórios aplicáveis, com matriz por SHA/ambiente e N/A justificado.
- Controles contra falso positivo e ressalva explícita de que validação completa não garante 100% de disponibilidade contínua.
- Teste de regressão para preservar o contrato em todas as frentes.

## 1.6.17 - 2026-10-24

- A análise de falhas passa a classificar cada sinal como `root_cause`, `control`, `cascade` ou `noise`; controles governados não entram no Pareto nem autorizam bypass.
- O Pareto considera somente causas ainda ativas, e reruns sem mudança material não contam como correção ou novo incidente.
- Produtores devem expor contexto sanitizado e a prevenção só é longitudinalmente comprovada após uma janela posterior sem reincidência.

## 1.6.16 - 2026-10-23

- Adicionada regra transversal de melhoria contínua orientada por logs e falhas: sanitização, fingerprint estável, separação de causa-raiz/cascata/ruído e priorização por Pareto.
- Correções passam a exigir prevenção verificável e confirmação de não reincidência na janela seguinte antes de avançar para a próxima causa.
- A análise não autoriza merge, deploy, mudança administrativa, gasto ou ação destrutiva; custo incremental permanece R$ 0,00 e a automação reutilizável do Observability Platform deve ser fixada por SHA integrado e validado.


## 1.6.15 - 2026-10-22

- A manutenção fast-forward de branches publicadas passa a selecionar exatamente uma capability revisada entre múltiplos registros, bloqueando ausência e ambiguidade sem liberar `git push` genérico.
- Adicionada capability de uso único para atualizar `engineering-worker-pool/codex/windows-native-no-docker-20261022` de `8740def7e832916452b13c52a1f4bec2f5712df7` para `cd150974a094dcda240987d8a2283c709a714d36`, vinculada ao worktree e helper versionado exatos.
- O helper continua recusando force-push, árvore suja, divergência remota, não ancestralidade e readback final incorreto; replays após uso falham fechados.

## 1.6.14 - 2026-10-20

- Formalizada a Regra nº 1 global: custo adicional máximo de R$ 0,00, com decisão fail-closed quando custo, franquia ou proteção contra overage não estiverem comprovados.
- Cartão cadastrado e autorizações técnicas genéricas deixam explicitamente de constituir autorização financeira; qualquer exceção paga exige autorização específica de produto, ação, valor máximo, moeda e escopo/período.
- GitHub Actions e demais produtos medidos passam a exigir classificação `COST_ZERO_CONFIRMED` antes da execução; custo desconhecido ou não zero bloqueia sem enfraquecer testes, evidências ou gates.
- O validador passa a exigir a presença da regra de governança de custo e as políticas do Command Gateway avançam apenas o identificador de versão, sem mudança de permissões ou allowlists.

## 1.6.13 - 2026-10-20

- A atualização persistente do Command Gateway passa a testar a substituição de cada arquivo gerenciado antes de escrever o novo bundle, bloqueando explicitamente alvo read-only e preservando o erro do sistema quando sharing mode, ACL, antivírus ou outra interferência não puderem ser distinguidos; no Windows, a ativação usa um clone irmão preparado por `CopyFileExW` e `MoveFileExW`, preservando e relendo OWNER, GROUP, DACL, atributos e streams alternativos sem expor o merge de ACL do `ReplaceFileW`.
- O updater serializa cada raiz de instalação, grava journal atômico e sincronizado antes da primeira mutação, assume sozinho a transação de upgrade, evita rollback duplicado, mantém um clone de recuperação verificado e imutável e só declara restauração após reconciliar conteúdo e metadados. A garantia cobre interrupção do processo; não declara recuperação transacional após perda abrupta de energia, pois os payloads e snapshots preexistentes ainda não possuem uma barreira de persistência completa no Windows.
- Replays reconciliam tentativas interrompidas e o mesmo commit já instalado é validado por readback sem reinstalação; testes cobrem falha transitória e permanente, N-ésimo arquivo, rollback falho e handle Windows sem `FILE_SHARE_DELETE`.

## 1.6.12 - 2026-10-20

- Formalizado contrato obrigatório de requisição para capabilities mutantes: alvo/ambiente, SHA/digest, correlação, idempotência, parâmetros fechados, confirmação, timeout, rollback e readback independente.
- Adicionado autorizador de ação Risk 3 específica, sem reativar modo amplo, com expiração curta e vínculo obrigatório a repositório limpo, worktree exato, HEAD exato e script versionado.
- A execução Risk 3 explícita passa a validar cwd/HEAD limpos quando a ação local contém esse vínculo, impedindo reaproveitamento da allowlist em outro checkout.
- CI passa a bloquear regressões do novo contrato e seus controles negativos.
- Definido o contrato compartilhado de artefatos duráveis S3/CAS, com criação condicional, integridade, readback, escopo por prefixo e retomada segura entre executores.
- Adiciona perfil explícito do Command Gateway para executores POSIX, limitado
  aos repositórios compartilhados declarados e ao namespace de worktrees.
- Valida plataforma, caminhos absolutos e variáveis obrigatórias antes da
  execução, vincula o ambiente POSIX a caminhos confiáveis versionados e
  congela caminhos físicos, recusa symlinks/junctions em raízes críticas e ancora
  o `cwd` do subprocesso por descritor no POSIX para impedir escape ou
  redirecionamento tardio da allowlist, da execução e do estado.
- Mantém diagnóstico read-only para caminhos não resolvidos sem transformar o
  diagnóstico em autorização e inclui controles positivos e negativos do perfil.
- Ancora o estado operacional POSIX com `dir_fd` e `O_NOFOLLOW`, rejeita folhas
  não regulares ou com hardlinks e grava reservas/snapshots por substituição
  atômica; documenta o limite do fallback Windows e a ACL exclusiva exigida.

## 1.6.11 - 2026-10-17

- Corrigido o bundle persistente do Command Gateway para instalar `runner_version_preflight.py`, dependência direta do Session Launcher.
- O instalador agora valida o fechamento transitivo dos imports locais de todos os entrypoints Python destinados a `bin/` e falha fechado quando um módulo local é omitido.
- Adicionados smoke tests do launcher instalado fora da árvore fonte e sem `PYTHONPATH`, controles negativos para módulo removido e import transitivo ausente, e teste de upgrade de bundle legado com backup e recibo `HOST_UPDATE_OK`.
- O CI bloqueante passa a executar também os testes do updater; a regra E2E formaliza reprodução, causa-raiz, regressões positiva/negativa, artefato instalado, migração e verificação operacional separada por SHA.
- A atualização persistente passa a criar snapshot e manifesto antes da primeira mutação, restaurar exatamente arquivos gerenciados e metadados em falhas intermediárias e remover somente arquivos novos comprovadamente escritos pela tentativa.

## 1.6.10 - 2026-09-28

- `session_launcher.py` passa a inserir explicitamente o próprio diretório em `sys.path` antes de importar módulos irmãos, mantendo o bootstrap governado funcional também sob Windows embeddable Python com `pythonXY._pth`.
- Adicionado teste isolado com `python -I` que reproduz ausência do diretório do script no caminho de imports e impede regressão do `ModuleNotFoundError: command_gateway`.
- A correção é transversal e elimina a necessidade de cada workflow consumidor editar o arquivo `._pth` do Python portátil.

## 1.6.9 - 2026-09-28

- `session_launcher.py` passa a aceitar o `GITHUB_WORKSPACE` corrente como fonte transitória somente em GitHub Actions, sem ampliar a allowlist de execução do Command Gateway.
- A origem dinâmica exige `GITHUB_ACTIONS=true`, `GITHUB_SHA` completo, `GITHUB_REPOSITORY` válido e remoto `origin` compatível com o repositório do workflow.
- A proteção existente permanece: `expected_head` + `sync_ref`, árvore rastreada limpa e materialização em base isolada antes do preflight.
- Adicionados controles negativos para workspace divergente, remoto divergente e SHA inválido.


## 1.6.8 - 2026-09-28

- Adicionado `scripts/runner_version_preflight.py` para inspecionar a versão real do GitHub Actions self-hosted runner via `Runner.Listener --version`.
- O preflight bloqueia registro/re-registro abaixo de `2.329.0` e usa a API oficial de depreciação quando autorizada, sem transformar HTTP 401/403/404 em falso diagnóstico de obsolescência.
- A release pública mais recente é apenas sinal informativo por causa do rollout progressivo; pickup do job continua sendo evidência independente de suporte de runtime naquele instante.
- `session_launcher.py` recebe `--require-runner-version-preflight`, mantendo a checagem dentro do bootstrap permitido antes do Command Gateway.
- Adicionados controles negativos para versão abaixo do mínimo, runtime expirado, deadline de registro vencida e indisponibilidade de permissão da API.


## 1.6.7 - 2026-09-24

- O Session Launcher passa a aceitar fontes transitórias de checkout explicitamente allowlisted por caminho exato em `session_source_roots`.
- Checkout de GitHub Actions fora da allowlist normal nunca vira diretório de execução: o bootstrap valida SHA/remoto, exige árvore rastreada limpa e materializa base isolada em `C:\\dev\\chatgpt-workers` antes do preflight.
- A exceção de fonte não relaxa `denied_segments`, não permite curingas e não altera a allowlist do Command Gateway para comandos pós-bootstrap.
- Adicionada origem canônica do runner Noteri para eliminar dependência de `C:\\dev\\reqsys-v2-enterprise-real` pré-existente sem recorrer a RDC ou shell irrestrito.

## 1.6.6 - 2026-09-22

- Adicionado watchdog de progresso material para chats, agentes, workers e automações, com limite padrão de 15 minutos.
- Heartbeat, renovação de lease e polling sem mudança deixam de reiniciar o relógio de progresso.
- Após estagnação, a decisão canônica passa a ser rerotear quando houver alternativa segura ou bloquear/liberar capacidade quando não houver.
- Adicionado executor reproduzível `scripts/progress_watchdog.py` e testes positivos/negativos para falso progresso, timeout, reroteamento, bloqueio e timestamps inválidos.

## 1.6.5 - 2026-09-20

- RDC: resultados técnicos deixam de mascarar falhas funcionais; novo guard semântico classifica `NO_CALLBACK`, efeitos vazios e evidência ausente como falha fechada.
- RDC: recuperação headless passa a usar circuit breaker persistente (3 falhas, cooldown exponencial 5–30 min, half-open) e só publica readiness após 10 s de transporte estável.
- RDC: reinícios do Scheduler ficam limitados a 3 com intervalo de 5 min, evitando retry storm em múltiplas camadas.
- O Session Bootstrap passa a reconciliar materialização interrompida quando o diretório já existe e o Git comprova que ele é um worktree registrado da mesma base, detached, limpo e no SHA reservado.
- Worktree existente não registrado, dirty ou em SHA divergente continua bloqueado em modo fail-closed.
- Testes unitários e E2E reproduzem interrupção entre `git worktree add` e persistência da reserva, incluindo controle negativo com worktree dirty.

## 1.6.4 - 2026-09-19
- O preflight dual-host passa a distinguir alcance do computador de conectividade do Remote Desktop Commander, evitando classificar ausência de heartbeat como PC desligado.
- Hosts alcançáveis com controller desconectado permanecem fail-closed para execução e recebem ação explícita de recuperação do controller.
- O supervisor RDC ganha modo headless governado com tarefa AtStartup + S4U, watchdog/restart e execução sem depender de login interativo.

## 1.6.3 - 2026-09-19
- O Command Gateway passa a aceitar `git_state_timeout_seconds` configurável, com faixa segura de 5 a 120 segundos.
- O perfil operacional usa 60 segundos para estado Git, evitando falsos bloqueios em hosts PC24x7 com I/O mais lento sem relaxar o fail-closed.
- `session_launcher` e `session_bootstrap` reutilizam o mesmo timeout de política para `git status` rastreado.

## 1.6.2 - 2026-09-16
- Generalizada a seleção de runtime para aplicar o princípio `PC24x7-first` antes de criar infraestrutura paga adicional quando o workload for compatível.
- Definidos critérios objetivos de elegibilidade: execução supervisionável, persistência e backup, HTTPS estável quando necessário, segredo fora do Git/chat, restart automático, isolamento/disponibilidade adequados e E2E verificável.
- ReqSys passa a preferir PC24x7 para workloads auxiliares compatíveis, especialmente em DEV, sem promover automaticamente HML/PROD nem remover gates de segurança, continuidade, SLA ou regulação.
- Render e outros provedores gerenciados permanecem disponíveis quando houver requisito objetivo não atendido pelo PC24x7 ou decisão explícita de contingência/arquitetura.

## 1.6.1 - 2026-09-13
- Host bootstrap em Windows passa a provisionar MinGit oficial quando `git` não estiver instalado.
- MinGit é fixado por versão, tamanho e SHA-256; o ZIP é validado antes da extração e não altera o `PATH` global.
- Extração do ZIP bloqueia path traversal, caminhos absolutos, drive prefixes, symlinks e limites excessivos de arquivos/tamanho.
- Bootstrap diferencia runtime instalado de `HOST_BOOTSTRAP_OK`; falhas finais gravam `HOST_BOOTSTRAP_BLOCKED` para evitar recibo enganoso de sucesso.
- Adicionados testes para Git existente, fallback MinGit, hash/tamanho inválidos e ZIP inseguro.

## 1.6.0 - 2026-09-12
- Adicionado Session Launcher como entrada única para criar sessão, validar SHA opcional e materializar o worktree via preflight.
- Launcher gera `session_id` seguro quando ausente e retorna `SESSION_LAUNCH_OK` com target, HEAD e snapshot SHA-256.
- Launcher é fail-closed para versão antiga, HEAD divergente e falhas do preflight; não executa fetch nem comandos arbitrários antes da sessão.
- Adicionados testes unitários/E2E e adoção operacional inicial no ReqSys.

## 1.5.2 - 2026-09-11
- Upgrade do host bootstrap faz fetch explícito do SHA aprovado antes do checkout em repositório de validação já existente.
- Saída JSON das CLIs usa escapes ASCII para funcionar em consoles Windows CP1252 sem perder a evidência Unicode.
- Adicionados testes de upgrade N→N+1 e execução do Gateway sob CP1252 estrito.

## 1.5.1 - 2026-09-11
- Bootstrap registra somente diretórios criados e validados como `safe.directory`.
- Corrigido preflight pós-instalação em hosts Windows com proprietário administrativo.
- Adicionados testes isolados para comprovar o registro exato sem wildcard.

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
