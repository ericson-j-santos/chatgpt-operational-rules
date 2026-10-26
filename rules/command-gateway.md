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

- Antes do primeiro comando local/remoto, executar `scripts/session_preflight.py` e exigir `BOOTSTRAP_OK` com `state_validated=true`; o preflight chama o bootstrap interno de reserva/materialização.
- Remote Desktop Commander é somente transporte para chamar bootstrap/gateway.
- Depois do bootstrap, todos os comandos locais/remotos devem passar pelo Command Gateway.
- Não usar PowerShell, CMD, Bash, WSL, SSH ou terminal irrestrito como fallback quando gateway/bootstrap bloquear ou estiver indisponível.
- Falha de bootstrap, política, reserva, lock ou validação interrompe a execução.
- Plugins/APIs específicas continuam preferíveis para operações de sistemas externos.

## Escopo operacional

O perfil padrão autoriza somente:
- `C:\dev\reqsys-v2-enterprise-real`;
- worktrees legados que correspondam a `C:\dev\wt-*`;
- repositórios/worktrees isolados sob `C:\dev\chatgpt-workers\*`.

O namespace `C:\dev\chatgpt-workers\*` é o local preferencial para múltiplos chats/agentes. Cada frente deve usar diretório e branch próprios; não compartilhar o mesmo working tree entre workers.

Em executores POSIX, usar `config/command-gateway.policy.posix.json`. O perfil
exige `REQSYS_WORKSPACE_ROOT` e `REQSYS_GATEWAY_STATE_DIR` definidos como
caminhos absolutos. O perfil atual vincula esses valores, respectivamente, a
`/workspace` e `/workspace/.reqsys-command-gateway-posix` por meio de
`required_path_environment`; o chamador não pode redirecioná-los. Somente os
repositórios compartilhados enumerados na política e o namespace
`${REQSYS_WORKSPACE_ROOT}/.chatgpt-workers/*` são autorizados. Ele não autoriza
`${REQSYS_WORKSPACE_ROOT}` inteiro. Um layout diferente exige outro perfil
versionado e homologado. O perfil Windows continua em
`config/command-gateway.policy.json`; uma política declarada para outra
plataforma deve falhar antes da execução.

Variáveis de caminho não resolvidas e caminhos relativos devem falhar fechados.
Raízes de política e o diretório de estado devem ser canonizados uma vez e não
podem conter symlink ou junction em componentes existentes. O diretório de
execução deve ser resolvido para o alvo físico antes da validação. Em POSIX, o
subprocesso deve receber um descritor herdado que permaneça ancorado nesse mesmo
diretório; nas demais plataformas, recebe o caminho canônico. Assim, um link dentro de uma raiz
permitida ou redirecionado depois do carregamento não amplia a autoridade.
O diagnóstico read-only pode relatar uma variável ausente como não resolvida,
mas isso não concede autorização de execução.

Para OCR, quando o código estiver no ReqSys, usar um worker isolado do repositório ReqSys.

### Escopos distintos de Portal Portabilidade

Há duas implementações **não intercambiáveis**. Não presumir que uma Issue,
TODO ou teste possa ser executado no outro repositório:

- **Portal Portabilidade 2025 (JWT)**: `ericson-j-santos/portal-portabilidade-2025`,
  que usa login próprio/JWT. Este é o repositório canônico do produto 2025
  previamente especificado por esta regra.
- **Portal Portabilidade MSAL (Entra ID)**: `ericson-j-santos/portal-portabilidade`,
  que contém o fluxo de autenticação Microsoft Entra/MSAL. Ele é alvo distinto,
  não alias, clone nem branch do produto JWT.

Para selecionar um executor, exigir que o TODO canônico e a Issue GitHub aberta
apontem para a **mesma** combinação explícita de variante,
`repository_full_name`, número de Issue, branch padrão e HEAD SHA completo,
lidos de forma independente e atualizados imediatamente antes do despacho.
Quando o pedido disser apenas "Portal Portabilidade", não identificar variante
ou misturar JWT/MSAL, **falhar fechado** e manter o TODO **BLOQUEADO** até
reconciliação da identidade (issue de governança #98). Não inferir migração,
não renomear repositório e não deslocar tarefas entre as duas implementações.

Ambos os projetos somente podem usar worktrees isolados sob
`C:\\dev\\chatgpt-workers\\*`, respeitando bootstrap, locks, Gateway e os
demais controles deste documento; a documentação não amplia allowlists,
permissões administrativas ou autorização de produção.

O perfil padrão não autoriza clones existentes no diretório de usuário, o volume `D:` nem outros diretórios fora da allowlist. Um novo projeto só entra no gateway ao ser colocado no namespace de workers ou por alteração explícita desta política.

Clones com alterações locais preexistentes não devem ser adotados como workspace automático. Preservar o trabalho existente e criar um worker limpo a partir da referência canônica.

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

## Gate específico Noteri: consultar WSL em execução (sem iniciar Ubuntu)

A operação `wsl.exe --list --running --quiet` é a única exceção de leitura
revisada para o Noteri. A Microsoft documenta que `--running` lista apenas
as distribuições **já em execução**; a operação não executa comandos Linux.
Esse comportamento precisa ser confirmado no host antes de atribuir prontidão.

- A chave de política `noteri_wsl_running_inventory` deve ser booleano **true**
  em um perfil explicitamente admitido para o Noteri; o padrão é ausente/false.
  A política Windows global continua com `wsl` em `blocked_executables`.
- Exigir `os.name=nt`, hostname exato `Noteri`, política `platform=windows`,
  sessão existente, Gateway `risk=1`, lock, HEAD SHA e cwd isolado.
- O executável deve ser exatamente o `wsl.exe` do diretório Win32 System32,
  resolvido pela API do Windows, não `PATH`, um alias nem um caminho fornecido
  por texto externo. Apenas os argumentos `--list --running --quiet` nessa ordem
  são permitidos. `--exec`, `--distribution`, `--install`, `--shutdown`,
  shell e qualquer opção adicional permanecem negados.
- A resposta é analisada com limite de bytes/entradas, emitindo apenas número
  de distribuições e indicador de **candidato** Ubuntu já executando; nunca
  nomes arbitrários, stderr bruto, credenciais ou caminhos privados.
- `ubuntu_running_candidate=true` não autoriza executar `wsl.exe --exec`.
  A distribuição pode parar após a leitura (TOCTOU); o acesso POSIX separado
  precisa de transporte e Session Launcher/Gateway **dentro** do guest,
  previamente homologados. O helper jamais testa systemd/psql por conta própria.
- Implementação: rotinas internas em `scripts/command_gateway.py`, sem
  dependência de módulo auxiliar no pacote instalado. Testes positivos, negativos, SHA/lock,
  replay e Windows: `tests/test_noteri_wsl_running_status.py`.

Não ativar o perfil na máquina nem iniciar distribuições sem gate operacional
específico. Nenhum Docker, Neon, serviço, CAS ou permissão nova é exigido.

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

### Integridade do diretório operacional

Em POSIX, toda criação, abertura, substituição e remoção sob o diretório operacional usa componentes relativos a descritores de diretóio ancorados, com `O_NOFOLLOW`. Arquivos precisam ser regulares, pertencer à identidade efetiva e possuir um único hardlink antes de leitura ou escrita. Reservas e snapshots são persistidos por arquivo temporário aleatório e exclusivo, sincronização e substituição atômica no mesmo diretório.

No Windows, a biblioteca padrão usada pelo gateway não oferece operações equivalentes relativas a `dir_fd`. O fallback rejeita symlinks e junctions estáveis antes e depois da abertura e valida a identidade do arquivo aberto, mas não promete resistência a uma troca concorrente feita por outro processo sob a mesma identidade. Portanto, o diretório operacional Windows deve ter ACL exclusiva para a identidade técnica do gateway, e processos não confiáveis não podem compartilhar essa identidade. Quando isso não puder ser garantido, a execução deve falhar de forma controlada ou usar uma implementação nativa baseada em handles Win32.

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
