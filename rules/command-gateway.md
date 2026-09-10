# Command Gateway local

Esta regra define o mecanismo operacional para executar comandos governados em máquinas autorizadas.

## Estado alvo

O gateway deve mediar comandos locais sempre que possível, aplicando:
- allowlist explícita de diretórios;
- bloqueio de caminhos e nomes sensíveis;
- execução sem shell intermediário;
- classificação de risco;
- lock exclusivo por repositório;
- `correlation_id`;
- validação do estado Git antes e depois;
- log estruturado com mascaramento;
- `timeout`;
- controles negativos contra falso positivo;
- bootstrap obrigatório de sessão e reserva de worktree conforme `rules/session-bootstrap.md`.

## Uso exclusivo e sem fallback

- Antes do primeiro comando local/remoto, executar `scripts/session_preflight.py` e exigir `BOOTSTRAP_OK` com `state_validated=true`.
- Remote Desktop Commander é somente transporte para chamar preflight/gateway.
- Depois do bootstrap, todos os comandos locais/remotos devem passar pelo Command Gateway.
- Não usar PowerShell, CMD, Bash, WSL, SSH ou terminal irrestrito como fallback quando gateway/bootstrap bloquear ou estiver indisponível.
- Falha de bootstrap, política, reserva, lock ou validação interrompe a execução.
- Plugins/APIs específicas continuam preferíveis para operações de sistemas externos.

## Escopo inicial

O perfil padrão autoriza somente:
- `C:\dev\reqsys-v2-enterprise-real`;
- worktrees que correspondam a `C:\dev\wt-*`.

O perfil padrão não autoriza o diretório de usuário nem outros volumes/projetos. Novos diretórios exigem alteração explícita da política e validação.

## Bloqueios

O gateway deve bloquear:
- risco 3; operações críticas continuam exigindo ação governada e autorização humana fora do gateway automático;
- diretórios fora da allowlist;
- execução dentro de `.ssh`, `.azure`, `.aws`, `.gnupg`, `.kube` ou equivalentes configurados;
- argumentos que referenciem `.env`, chaves privadas, credenciais ou nomes sensíveis configurados;
- shells e ferramentas administrativas bloqueadas pela política;
- composição por `&&`, `||`, `;`, pipes ou redirecionamentos;
- código inline (`python -c`, `node -e`) no perfil operacional padrão;
- comandos destrutivos conhecidos, inclusive `git push`, `git reset --hard`, `git clean -f`, `docker system prune` e equivalentes.

## Bootstrap e worktree por sessão

- `require_session_bootstrap=true` deve permanecer ativo no perfil operacional.
- `worktree_root` e `worktree_prefix` definem onde a sessão pode reservar o worktree.
- O bootstrap captura o SHA da base e persiste uma reserva exclusiva fora do working tree.
- Materialização usa `git worktree add --detach` no SHA reservado por meio do próprio gateway.
- A materialização é permitida somente quando não existem alterações rastreadas na base.
- Arquivos não rastreados da base não são copiados para o worktree e não autorizam sobrescrita.
- O worktree deve terminar limpo e no SHA reservado.
- Alterações de risco 2 devem ocorrer no worktree da sessão, nunca diretamente na árvore base.

## Risco 1

Para uma operação declarada como risco 1:
1. capturar branch, HEAD e digest do estado Git;
2. adquirir lock exclusivo;
3. confirmar novamente o estado após o lock;
4. executar o comando com `shell=False`;
5. capturar novamente o estado;
6. falhar se HEAD ou estado Git tiver mudado.

Um comando que retorna código zero, mas altera o repositório, é falso positivo e deve ser rejeitado.

## Risco 2

Para risco 2:
- a árvore deve estar limpa por padrão;
- o HEAD esperado pode ser informado e deve coincidir antes da execução;
- mudanças no working tree são permitidas e registradas;
- mudança de HEAD é bloqueada por padrão;
- `--allow-head-change` só pode ser usado quando a solicitação atual autorizar explicitamente a criação de commit correspondente;
- após a ação, testes e E2E aplicáveis continuam obrigatórios.

## Concorrência

O lock fica fora do repositório, no diretório operacional local configurado. Todos os chats/agentes que operem pelo ecossistema devem respeitar esse lock.

Além do lock, o gateway compara o estado imediatamente antes e depois da aquisição. Mudança concorrente nesse intervalo aborta a execução.

Mudanças feitas por processos que ignorem o gateway não podem ser atribuídas com certeza ao executor. Se isso ocorrer, registrar bloqueio e não sobrescrever trabalho existente.

## Exclusões de estado não rastreado

- `git_untracked_excludes` pode excluir somente áreas temporárias/descartáveis da enumeração de não rastreados.
- Os padrões são relativos ao repositório e não reduzem a verificação de arquivos rastreados.
- Demais arquivos não rastreados continuam enumerados individualmente e participam do digest de estado.
- Aviso ou erro em qualquer comando usado para compor o estado Git continua bloqueando a execução.

## Evidência

Cada execução deve produzir evento JSONL contendo, sem segredos:
- `correlation_id`;
- instante UTC;
- diretório;
- risco;
- executável e argumentos mascarados;
- branch/HEAD antes e depois;
- digest do estado Git;
- código de saída;
- classificação final.

O bootstrap adiciona `session_id`, SHA base, worktree reservado e estado `reserved|materialized`.

## Critério de conclusão

O gateway está operacional somente quando:
- política é carregada;
- bootstrap produz `BOOTSTRAP_OK`;
- reserva por sessão é idempotente e conflito é bloqueado;
- worktree materializado está limpo e no SHA reservado;
- diretórios permitidos e bloqueados são testados;
- lock concorrente é testado;
- risco 1 detecta mutação indevida;
- divergência de HEAD é testada;
- referência sensível é bloqueada;
- o fluxo positivo executa com código zero;
- os testes negativos comprovam que o gate falha quando deve;
- o estado final do repositório real é revalidado.

## Preflight automático

Antes do primeiro `inspect/run`, executar `scripts/session_preflight.py`. O preflight captura host, versão das regras, branch, SHA, digest/contagem do estado Git, reserva e worktree, grava snapshot com SHA-256 e retorna `BOOTSTRAP_OK` somente após revalidar estado estável. Quando `require_preflight_snapshot=true`, o gateway deve bloquear sessão sem snapshot íntegro.
