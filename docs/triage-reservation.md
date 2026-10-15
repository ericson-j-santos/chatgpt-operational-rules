# Reserva compartilhada da triagem — issue #137 / PR #139

## Escopo e fonte de estado

A reserva usa o PostgreSQL **já utilizado pelo TODO Gateway**, na tabela aditiva
`todo_bus.triage_reservations`. Não cria scheduler, fila de execução, serviço
permanente ou substituto do TODO Global. A tabela guarda exclusividade e
checkpoint nominal; o TODO Global continua canônico e sua projeção externa
permanece pendente até confirmação independente no Notion.

O escopo é derivado da chave existente do TODO + operação fixa
`portfolio.triage.v1`. A chave original é preservada, inclusive para TODOs legados;
não criar outro TODO apenas para converter uma chave antiga em SHA-256.

## Ativação controlada

As rotas permanecem desligadas por padrão. O ASGI só as registra com
`TODO_TRIAGE_MODE=dev` e `TODO_TRIAGE_ENVIRONMENT=DEV`. Qualquer outro modo é
recusado. A migração aditiva `initialize_schema` é explícita, deve ocorrer na
sessão governada do ambiente DEV autorizado e nunca é executada implicitamente
por uma requisição HTTP. Sem tabela disponível, a operação falha com 503.

Os endpoints usam a autenticação existente do gateway e limite de corpo de
64 KiB. Não há comandos livres, URLs de destino ou credenciais no contrato.

## Contrato

- `GET /v1/triage/snapshot?todo_key=...`: consulta checkpoint, versão, nomes únicos
  e pendentes. Escopo ainda não inicializado retorna versão zero.
- `POST /v1/triage/claim`: exige `todo_key`, `claim_id`, `correlation_id`,
  `expected_version`, `source_version`, `inventory` e `known`; opcionais
  `lease_seconds` (1..300) e `limit` (1..100, padrão 10).
- `POST /v1/triage/complete`: exige a identidade da reserva, `fence`,
  `expected_version`, `source_version`, `commit_id` e registros contendo apenas
  `repository`, `head_sha` completo e `branch`.
- `POST /v1/triage/release`: exige `todo_key`, `claim_id`, `fence` e
  `expected_version`. Uma reserva antiga não pode liberar a reserva substituta.

Inventário e baseline `known` devem vir de leitura confiável e independente de
GitHub/Notion, não de uma soma de lotes nem de texto livre do modelo. O serviço
valida a estrutura, **não autentica por si só a origem dos snapshots fornecidos
pelo produtor já autenticado**. Mudança do snapshot de origem bloqueia o escopo
até reconciliação explícita; este incremento não implementa rebase automático.

## Exclusividade e persistência

Cada alteração obtém lock de linha PostgreSQL e usa o relógio do banco depois
da aquisição. A versão do checkpoint é conferida na mesma transação; a escrita
usa compare-and-swap dessa versão. A seleção é a diferença entre inventário,
baseline conhecida e registros já persistidos. Identidades `owner/repo` são
normalizadas e aliases duplicados são rejeitados.

Cada nova reserva recebe um número monotônico `fence`. Após expiração, um novo
pedido pode adquirir o escopo, mas o executor antigo não pode gravar nem liberar
esta nova reserva. Replay da reserva não renova prazo. Replay de um commit com
conteúdo idêntico só lê o recibo existente, sem nova contagem; conteúdo diferente
com o mesmo ID é conflito. O histórico é limitado a 1000 reservas/recibos por
escopo e falha fechado ao atingir esse limite, sem descarte silencioso.

O checkpoint é atômico **dentro do PostgreSQL**. Ele não é uma transação distribuída
com Notion e não impede um escritor externo de ignorar o gateway. Por isso,
`CHECKPOINT_PERSISTED` sempre retorna `todo_completed=false` e
`external_projection=PENDING`. Não aumentar o total do TODO-25 nem encerrar #137
sem produtor integrado, publicação externa e leitura independente no mesmo SHA.

## Validação

`tests/test_triage_reservation.py` usa HTTP real e PostgreSQL real descartável no
CI. Cobre disputa simultânea de dois clientes pelos mesmos dez nomes, commit
concorrente e replay, expiração/fencing, liberação, conflitos de versão, dados
fora do escopo, falha real de persistência com rollback, autenticação e leitura
SQL independente. Os dados do portfólio são fixtures explícitas; não são dez
repositórios efetivamente triados do usuário.

`scripts/triage_reservation_e2e.py` cria Session Launcher/Command Gateway reais e
worktree exclusivo no SHA exato antes de executar a suíte. Falha de bootstrap,
banco ou cleanup bloqueia o teste; não há fallback nem testes ignorados por falta
do banco. O banco/container é descartado ao fim do job e não migra PC24x7 para
Docker. A evidência do job não comprova Notion live, múltiplos hosts físicos,
implantação permanente ou uma tarefa real de produto pelo worker.

## Próxima ligação externa

Reutilizar o produtor autenticado existente para obter snapshots, adquirir a
reserva, ler metadados reais dos candidatos, persistir o checkpoint e publicar
uma atualização idempotente no mesmo TODO. Provar rejeição de escritor atrasado,
replay, readback integral no Notion e retomada após falha entre PostgreSQL e
projeção externa. Sem esta ligação, o resultado permanece parcial.
