# Roteamento governado entre hosts

## Objetivo
Usar múltiplos computadores como workers independentes sem compartilhar working tree e sem roteamento para host degradado.

## Preflight obrigatório
Antes de distribuir trabalho entre dois ou mais hosts, coletar evidência atual para cada candidato:
- controller online;
- autenticação válida;
- SHA de regras igual ao SHA canônico exigido;
- `SESSION_LAUNCH_OK`;
- `state_validated=true`;
- Command Gateway funcional;
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

Um host com controlador offline permanece inelegível para execução, mesmo quando `host_reachable=true`. A diferença é operacional: primeiro recuperar o controlador, em vez de declarar o PC desligado.

Quando o controlador estiver offline:
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

O perfil ausente é interpretado como `NORMAL` para compatibilidade. Perfil desconhecido deve falhar fechado.

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
