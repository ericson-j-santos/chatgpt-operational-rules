# TODO Global assíncrono e orientado a eventos

Esta regra define o contrato operacional global para TODOs produzidos por chats, agentes, repositórios, integrações e demais soluções.

## Princípio

- TODO é **estado operacional**, não documentação.
- O `TODO Global` é a fonte canônica de estado.
- Planilhas, dashboards, e-mails, relatórios e sistemas específicos são projeções ou consumidores; não viram fonte de verdade por conveniência.
- O mecanismo é global e não pode depender de ReqSys ou de qualquer projeto específico.
- Regras específicas de projeto especializam esta regra, mas não substituem identidade, idempotência, evidência e rastreabilidade globais.

## Fluxo alvo

```text
produtor
  -> Event Gateway
  -> fila durável
  -> normalizador/consumidor
  -> upsert idempotente no TODO Global
  -> consumidores independentes (WIP, dashboard, sistemas, alertas)
```

A reconciliação periódica permanece ativa apenas como **rede de segurança** para reparar divergências, itens perdidos ou indisponibilidades temporárias. Ela não é o caminho primário quando um evento pode ser publicado no momento da mudança.

## Contrato `TodoEvent v1`

Todo evento deve possuir, no mínimo:

- `schema_version`;
- `event_id` único por emissão;
- `event_type`;
- `occurred_at` em UTC;
- `correlation_id` ponta a ponta;
- `idempotency_key` estável para o TODO lógico;
- `project`;
- dados normalizados do TODO.

A forma canônica do evento é definida por `schemas/todo-event-v1.schema.json`.

## Identidade e idempotência

- `event_id` identifica a emissão e impede processamento duplicado do mesmo evento.
- `idempotency_key` identifica o TODO lógico e deve convergir múltiplas emissões, chats ou agentes para o mesmo item.
- Preferir `SHA-256(projeto_normalizado + tipo + identificador_externo)`.
- Se não houver identificador externo, usar identificador lógico estável derivado da origem e da demanda.
- Reprocessar o mesmo evento não pode criar novo TODO.
- Processar evento novo com a mesma `idempotency_key` deve atualizar o TODO existente, preservando histórico e controles de concorrência.

## Semântica de entrega

- A fila opera com entrega **pelo menos uma vez**.
- O consumidor deve ser idempotente.
- A confirmação de aceitação do produtor significa apenas que o evento foi persistido na fila; não significa conclusão do TODO.
- Um produtor não deve esperar que todos os consumidores terminem para considerar a publicação aceita.

## Durabilidade, concorrência e backpressure

- A fila deve persistir eventos antes de confirmar aceitação.
- Consumidores reservam eventos por `lease`; lease expirado torna o evento novamente elegível.
- Reservas e mudanças de estado devem ser atômicas.
- Deve existir limite explícito de lote/concorrência para evitar consumo ilimitado.
- Se o destino estiver indisponível, a fila preserva o evento sem bloquear produtores saudáveis dentro dos limites definidos.
- Sob pressão, reduzir consumo ou rejeitar novas entradas de forma explícita; nunca perder silenciosamente eventos.

## Retentativas e DLQ/quarentena

- Falhas transitórias usam retentativa com backoff limitado.
- O número máximo de tentativas deve ser explícito.
- Ao atingir o limite, o evento vai para DLQ/quarentena com:
  - `event_id`;
  - `correlation_id`;
  - quantidade de tentativas;
  - erro sanitizado;
  - data/hora;
  - ação de reprocessamento.
- Não armazenar segredo, credencial ou conteúdo sensível bruto na DLQ.
- Reprocessamento da DLQ deve manter `event_id`/`idempotency_key` ou registrar a relação com o evento original.

## Estado e fail-closed

Estados canônicos do TODO:

- `PENDENTE`
- `EM ANDAMENTO`
- `BLOQUEADO`
- `CONCLUÍDO`
- `CANCELADO`

Nunca marcar `CONCLUÍDO` apenas por:

- HTTP 2xx;
- código de saída zero;
- CI verde;
- mensagem de sucesso;
- aceitação do evento pela fila;
- execução do consumidor sem leitura independente.

Conclusão exige critério objetivo e evidência atual da versão/execução correspondente. Quando faltar evidência, manter estado não terminal ou bloqueado.

## Consumidores

Consumidores devem ser independentes e substituíveis. Exemplos:

- adaptador do TODO Global;
- planilha WIP/WSJF;
- Dashboard WIP;
- ReqSys;
- Redmine;
- Power Platform;
- alertas.

Falha de um consumidor não deve impedir outros consumidores independentes de processar a mesma mudança quando a arquitetura de distribuição utilizada permitir isso.

### Consumidor de execução

Quando um TODO precisar originar trabalho técnico automatizado:

- usar uma `automation_action` tipada e allowlisted; nunca interpretar `next_action` como shell/comando;
- para `execution_lane.enqueue.v1`, usar `execution_request` estruturado com repositório, issue, request id e `base_sha`;
- URL, token, segredo, host e credencial da lane pertencem à configuração do consumidor e nunca ao evento;
- HTTP sem TLS só pode ser aceito em loopback; destinos remotos exigem HTTPS;
- aceitar uma task na lane conclui apenas a **continuação de despacho**; o TODO permanece não terminal até sua evidência de conclusão;
- replay deve reutilizar a identidade lógica downstream e não criar trabalho duplicado.

## Observabilidade

Registrar, sem dados sensíveis:

- `event_id`;
- `correlation_id`;
- `idempotency_key`;
- produtor;
- consumidor;
- estado da fila;
- tentativa;
- duração;
- resultado;
- motivo de DLQ quando houver.

A mesma `correlation_id` deve permitir reconstruir produtor -> fila -> consumidor -> TODO -> projeções.

## Reconciliação

Executar reconciliação periódica como controle compensatório:

1. ler TODOs não terminais;
2. comparar fonte canônica e projeções;
3. detectar ausentes, duplicados e divergências;
4. reparar somente com evidência;
5. nunca apagar o último snapshot válido por indisponibilidade temporária;
6. registrar divergência de forma fail-closed.

## Validação obrigatória

Aplicar `rules/e2e-validation.md`. Para este fluxo, o E2E mínimo deve provar:

1. evento válido é aceito e persistido;
2. consumidor processa e produz exatamente um TODO lógico;
3. replay do mesmo `event_id` não gera novo efeito;
4. novo `event_id` com a mesma `idempotency_key` atualiza o mesmo TODO;
5. falha transitória gera retentativa;
6. falha persistente atinge DLQ no limite configurado;
7. lease expirado recupera item sem perda;
8. uma leitura independente confirma o estado persistido;
9. controles negativos detectam evento inválido e falso sucesso.

## Critério de conclusão

A implementação só pode ser considerada validada quando o caminho real disponível comprovar:

`produtor -> fila durável -> consumidor -> TODO Global -> projeção -> replay sem duplicidade`

Se o adaptador externo do TODO Global não estiver disponível no ambiente de teste, o núcleo pode ser classificado como validado em isolamento, mas a integração externa permanece `PARCIAL` ou `PENDENTE`, nunca `VALIDADO`.
