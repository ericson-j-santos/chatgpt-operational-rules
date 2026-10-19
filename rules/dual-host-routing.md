# Roteamento governado entre hosts

## Objetivo
Usar múltiplos computadores como workers independentes sem compartilhar working tree e sem roteamento para host degradado.

## Preflight obrigatório
Antes de distribuir trabalho entre dois ou mais hosts, coletar evidência atual para cada candidato:
- transporte selecionado e funcionalmente comprovado (RDC ou GitHub Actions);
- autenticação válida;
- SHA de regras igual ao SHA canônico exigido;
- `SESSION_LAUNCH_OK`;
- `state_validated=true`;
- Command Gateway funcional;
- no transporte RDC, `controller_online=true` e `controller_semantic_ok=true`, derivados de evidência funcional normalizada; no GitHub Actions, o contrato independente abaixo;
- worktree limpo;
- worktree solicitado não reservado por outra sessão.

A decisão deve ser fail-closed: qualquer requisito obrigatório ausente torna o host inelegível para aquela tarefa.

## Disponibilidade em camadas

`controller_online=false` significa somente que o canal do Remote Desktop Commander não está conectado. Esse sinal, isoladamente, **não prova que o computador está desligado ou sem rede**.

O preflight deve distinguir:
- `controller_online`: heartbeat/conectividade do controlador;
- `host_reachable`: evidência independente de que o Windows/host respondeu;
- `reachability_signals`: sinais positivos independentes, por exemplo RPC/serviço governado/probe de rede;
- `availability_state`: classificação consolidada sem relaxar os gates do worker.

Estados esperados:
- `controller_online`: host e controlador disponíveis;
- `host_reachable_controller_offline`: computador alcançável, mas controlador desconectado;
- `controller_offline_host_unknown`: controlador desconectado e sem evidência independente suficiente;
- `host_unreachable`: probe independente comprovou indisponibilidade do host.

No transporte RDC, um host com controlador offline permanece inelegível, mesmo quando `host_reachable=true`. Uma rota GitHub Actions comprovada pode ser selecionada independentemente do RDC; não recuperar RDC apenas para executar por essa rota.

## Resultado funcional e circuit breaker

Para Remote Desktop Commander, `controller_online=true` não basta para liberar o host. O preflight exige `controller_semantic_ok=true`, obtido por `scripts/rdc_semantic_result.py` ou evidência equivalente que comprove o efeito esperado.

Resultados como `NO_CALLBACK`, `matched=[]`, `controls=[]`, `SESSION_LAUNCH_BLOCKED`, ausência de prova funcional ou erro semântico devem tornar o host inelegível. Um `exit code 0` externo não pode sobrescrever essa classificação.

A recuperação do controlador deve usar circuit breaker persistente: após falhas consecutivas, abrir o circuito, suprimir novas tentativas durante o cooldown e permitir somente uma sonda `half_open`. O circuito só fecha depois de transporte estável comprovado; retries concorrentes por interface gráfica são proibidos enquanto o circuito estiver aberto.

Quando o transporte selecionado for RDC e o controlador estiver offline:
1. coletar evidência independente de alcance antes de concluir que o host está offline;
2. se o host responder, tentar somente recuperação governada do controlador/watchdog;
3. revalidar heartbeat, autenticação, sessão e Gateway;
4. reboot, power-on ou mudança administrativa continuam operações separadas e não podem ser disparadas apenas pela ausência de heartbeat do controlador.

## Seleção
Executar `scripts/dual_host_preflight.py` com a evidência coletada.
O roteador considera capacidade declarada e quantidade de tarefas ativas.
Uma preferência de host pode desempatar, mas nunca pode superar um bloqueio de segurança.

## Perfis operacionais

Cada host físico pode declarar um perfil operacional:

- `NORMAL`: aceita novas tarefas de desenvolvimento, build, teste, E2E e agentes.
- `ESTUDO`: permanece elegível para controle e monitoramento, mas não recebe novas tarefas de desenvolvimento.

No contrato legado RDC, o perfil ausente é interpretado como `NORMAL` para compatibilidade. No contrato GitHub Actions, o perfil deve ser explícito e coincidir com o recibo. Perfil desconhecido deve falhar fechado. `hml` e `homologation` não recebem novas tarefas em `ESTUDO`.

No `ESTUDO`, o worker de continuação deve concluir somente o trabalho já reservado e, a partir do ciclo seguinte, parar de reservar novos itens. Como a fila é durável e compartilhada, itens ainda `PENDING` permanecem disponíveis para outro worker em `NORMAL`, priorizando o Desktop quando elegível.

A mudança de perfil deve ser persistida de forma atômica, auditável por `correlation_id` e reavaliada a cada ciclo do worker. O retorno a `NORMAL` reabre a capacidade sem recriar a fila ou duplicar itens.

## Concorrência
Cada tarefa deve ter `session_id`, `correlation_id`, branch/SHA e worktree próprios.
Não permitir duas sessões gravarem no mesmo working tree do mesmo host.
Hosts distintos podem usar caminhos textualmente iguais porque os sistemas de arquivos são independentes, desde que cada host mantenha reserva exclusiva local.

## Evidência
Persistir ou registrar o JSON de entrada e o JSON de saída do preflight.
O resultado deve identificar `selected_host`, `secondary_host`, hosts bloqueados e respectivos motivos.
Para controlador offline, registrar também `host_reachable`, `availability_state` e `recovery_action`, evitando converter ausência de heartbeat em afirmação sobre energia/rede do computador.

## Versão do controller
Diferença de versão do Remote Desktop Commander gera aviso quando os demais requisitos estão íntegros.
Ela só se torna bloqueadora se houver incompatibilidade funcional comprovada ou regra específica mais restritiva.

## Transporte GitHub Actions — contrato opcional v1

O campo `transport` do candidato aceita `rdc` (padrão legado) ou
`github_actions`. Valor desconhecido não aciona fallback e bloqueia o candidato.
Esta mudança permite usar Noteri, Desktop ou outro host previamente autorizado,
mas **não instala SQL Server, não registra runner, não altera labels, não faz
dispatch e não transfere bancos nem tarefas em execução**.

Para `github_actions`, o produtor confiável informa `execution_repository`,
`expected_source_sha` (SHA completo do qualificador), `runner_name`, `profile` e
`transport_evidence`. Não inventar esses campos com base no nome do computador.

`transport_evidence` exige:

- `schema_version="1"`, `source="github_api_and_gateway_receipt"`, `repository`,
  `observed_at`, `independent_readback_verified=true`, `artifact_sha256` e
  `readback_sha256` de 64 dígitos hexadecimais;
- `job` obtido por leitura independente da API GitHub, com `id`, `run_id`,
  `run_attempt`, `runner_id` positivos, `runner_name`, `head_sha`, `html_url`,
  `labels` contendo `self-hosted`, conclusão `completed/success` e etapa
  `Verify governed host qualification` efetivamente `completed/success`;
- `receipt` lido e verificado fora da implementação sob teste, vinculando `host`,
  `repository`, `source_sha`, `rules_sha`, `run_id`, `job_id`, `run_attempt`,
  `runner_id`, `runner_name`, `correlation_id` e `profile` aos valores esperados;
- nesse recibo: `generated_at`, `session_result="SESSION_LAUNCH_OK"`, `session_id`,
  `snapshot_sha256`, `state_validated=true`, `gateway_ok=true`, `auth_valid=true`
  e `independent_effect_verified=true`.

Observação da API, conclusão do job e geração do recibo têm validade máxima de
300 segundos, exigem fuso e não podem estar no futuro. Reconsultar a API não
renova a idade do job nem do recibo. Jobs cancelados, sem atribuição de runner,
sem etapas ou com qualificação skipped não provam prontidão.

**Fronteira de confiança:** o seletor valida consistência e vinculação, não
autentica JSON nem busca/verifica os bytes dos artefatos por conta própria.
O produtor deve autenticar a fonte, verificar hashes contra bytes reais, observar
o efeito independentemente e só então marcar o readback como verificado. Não
alimentar esse contrato com JSON livre de usuário, logs autodeclarados ou
fixtures. Os hashes e flags isolados não são prova de autenticidade.

Os gates comuns de autenticação, SHA de regras, sessão, estado, Gateway,
limpeza e reserva de worktree continuam obrigatórios. Nenhum resultado usa
GitHub Actions como disfarce para executar RDC bloqueado por cota/circuit breaker.
A elegibilidade da rota não autoriza instalação, segredo, elevação, HML/PROD,
reboot, merge ou publicação. O executor deve revalidar a sessão e a reserva antes
de agir; o seletor não implementa lock/lease, cancelamento ou migração em voo.

## Capacidades e HML técnica portátil

`required_capabilities` é um mapa limitado de pares texto/texto, por exemplo
`sqlserver.major: "17"` e `sqlserver.runtime: "native"`. No GitHub Actions as
capacidades vêm do recibo verificado, nunca de labels nem de uma segunda
declaração livre no candidato. A classificação não presume SQL Server
instalado porque Python, CI ou o runner responderam.

A preferência pelo Desktop não supera inelegibilidade. `NORMAL/ESTUDO`,
segregação de worktrees, SHA, identidade, disponibilidade das dependências e
regras de dados permanecem aplicáveis em todos os candidatos.

A saída inclui `decision_only=true` e `hml_verified=false`, inclusive quando
`ready=true`. Qualificação de transporte não comprova motor SQL, TLS, grants,
backup/restore, benchmark nem ciclo de negócio. Essas evidências pertencem ao
mesmo ambiente/SHA de cada teste; não chamar E2E sintético de HML física.

A CLI substitui decisões por escrita atômica. Entrada inválida grava decisão
bloqueada, sem deixar um `ready=true` residual. Falha ao persistir retorna erro;
consumidores devem validar retorno, correlação, fonte e frescor, não apenas
reabrir um arquivo que pode pertencer a uma tentativa anterior.

## Aceite de ativação nos hosts

Este contrato não ativa automaticamente o produtor de recibos nos workflows
existentes. Antes de selecionar um host real, integrar ao canal governado sua
etapa de qualificação, validar Session Launcher/Gateway e produzir o recibo
independente. Manter o candidato bloqueado enquanto isso não estiver comprovado.

Para Noteri/HML técnica, continuar no TODO-37 existente: inventário nativo atual,
perfil NORMAL, capacidade disponível, SQL/driver/configuração protegida e E2E
real. Um job antigo sem runner comprova somente ausência de pickup naquela
execução; não permite afirmar que o computador está desligado agora.

Referência oficial do contrato de job:
https://docs.github.com/en/rest/actions/workflow-jobs
