# Watchdog de progresso material

## Objetivo

Impedir que chats, agentes, workers ou automações permaneçam aparentando execução enquanto apenas renovam lease, enviam heartbeat, repetem consultas de estado ou aguardam indefinidamente uma dependência externa.

## Princípio

Tempo de processo vivo não é progresso. A métrica canônica é o tempo desde a última **evidência material de avanço**.

Por padrão, uma execução é considerada estagnada após **300 segundos (5 minutos)** sem progresso material. Regra específica de projeto pode reduzir ou ajustar esse limite quando houver justificativa objetiva.

## O que conta como progresso material

Conta como progresso quando existe mudança verificável que aproxima a tarefa do critério de conclusão, por exemplo:

- novo commit/SHA produzido pelo incremento atual;
- transição real de estado da tarefa para a próxima etapa;
- teste, validação ou evidência nova vinculada ao SHA/execução atual;
- artefato novo ou atualização material do artefato esperado;
- efeito externo comprovado por leitura independente;
- remoção comprovada de um bloqueio;
- aquisição de uma dependência que permite iniciar a próxima etapa.

## O que NÃO conta como progresso

Nunca atualizar `last_material_progress_at` somente por:

- heartbeat;
- renovação de lease;
- polling/status GET sem mudança;
- repetição do mesmo log;
- mensagem de "continue", "ainda executando" ou equivalente;
- job continuar `queued`, `pending` ou `running` sem nova evidência;
- repetição de retry sem efeito novo;
- atualização cosmética ou de timestamp;
- sucesso técnico do transporte sem efeito funcional.

Esses eventos podem atualizar liveness/observabilidade, mas não reiniciam o relógio de estagnação.

## Decisão obrigatória

Para toda tarefa não terminal:

1. calcular `no_progress_seconds = now - last_material_progress_at`;
2. se menor que o limite, continuar;
3. se atingir o limite e existir rota alternativa segura/autorizada, retornar `switch_route`;
4. se atingir o limite e não existir rota alternativa, retornar `block`;
5. registrar `task_id`, `correlation_id`, `last_material_progress_at`, duração, motivo e decisão;
6. liberar capacidade do worker quando a tarefa for bloqueada;
7. nunca manter a tarefa indefinidamente como "em execução" apenas porque o processo está vivo.

## Dependência externa

Quando a tarefa estiver aguardando runner, CI, aprovação, controlador, quota, serviço externo ou outro recurso:

- a espera pode ser observada até o limite;
- após o limite, a execução ativa deve ser encerrada como bloqueada/aguardando ou roteada para alternativa;
- se houver mecanismo de condição futura, usar automação/evento para retomar;
- não ocupar worker/chat ativo apenas para polling contínuo.

## Integração com leases

Lease protege exclusividade; não prova progresso.

- renovar lease pode impedir claim concorrente;
- renovar lease **não** altera `last_material_progress_at`;
- task com lease válido ainda pode estar estagnada;
- recuperação de lease expirado e watchdog de progresso são controles complementares.

## Executor canônico

Usar `scripts/progress_watchdog.py` para decisões reproduzíveis.

Entradas mínimas:

- `task_id`;
- `correlation_id`;
- `state`;
- `last_material_progress_at`;
- opcionalmente `last_observation_at`;
- opcionalmente `stall_after_seconds`;
- `alternative_route_available`.

Saídas:

- `stalled`;
- `no_progress_seconds`;
- `decision`: `continue`, `switch_route`, `block` ou `terminal`;
- `reason_code`.

## Validação

Aplicar `rules/e2e-validation.md`.

O controle mínimo deve provar:

1. atividade abaixo do limite continua;
2. heartbeat/observação recente não mascara progresso material antigo;
3. estagnação com rota alternativa produz `switch_route`;
4. estagnação sem rota alternativa produz `block`;
5. estado terminal não é reaberto;
6. timestamps inválidos falham fechado.

## Critério de conclusão

Uma execução não pode permanecer indefinidamente em estado ativo sem nova evidência material. Ao exceder o limite, deve existir decisão explícita e auditável de reroteamento ou bloqueio.
