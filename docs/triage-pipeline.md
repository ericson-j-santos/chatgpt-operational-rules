# Produtor, reserva e projeção do TODO

Este incremento conecta o núcleo de reserva à fila e ao consumidor existentes.
Não instala serviços, não muda permissões e não torna o Worker Pool um executor
genérico. A classificação do portfólio e o E2E de cada produto continuam separados
de um checkpoint técnico de branch/HEAD.

## Fluxo e identidade

A rota autenticada `POST /v1/triage/publish` recebe `todo_key`, `request_id`,
`correlation_id`, `expected_version`, `source_version`, `inventory` e `known`.
`limit` é opcional (máximo 10); `lease_seconds` é opcional (máximo 300).
As listas são o inventário/baseline nominal já verificado pelo produtor de triagem,
não são deduzidas de contadores ou nomes de lotes. Alterar essa baseline exige
reconciliação explícita; este módulo não é um classificador de READMEs.

O serviço verifica a página canônica, data source, chave original, status e versão,
adquire uma reserva, lê as referências GitHub candidatas e repete as leituras antes
de gravar. Mudança de origem, escopo ou versão impede a publicação.
Solicitação e lease têm identidades diferentes: o request permanece estável;
uma tentativa liberada/expirada pode adquirir outro lease sem criar outro evento.

Checkpoint e inserção na `todo_bus.queue_events` compartilham UMA conexão e
transação PostgreSQL. O adaptador de conexão emprestada não faz commit interno.
Uma falha no enqueue reverte também o checkpoint. Não existe segunda fila.

A chave SHA-256 do envelope da fila não substitui uma chave textual preexistente
no Notion. O campo tipado `triage_projection` vincula a chave original, página,
request e evidência. O consumidor nunca cria uma página para essa operação.

## Efeito permitido e preservação

O consumidor atualiza SOMENTE `Evidência`, acrescentando o checkpoint às partes
anteriores, preservando links, anotações e histórico. Não muda status, responsáveis,
bloqueios, próxima ação, título ou identidade. Texto e formatação são comparados
na leitura independente. Rich text de tipos não suportados falha antes da escrita;
não é convertido silenciosamente. São respeitados os limites do contrato e do
Notion; não existe truncamento automático de histórico.

Após enqueue, a resposta é QUEUED, nunca CONCLUÍDO. O worker existente continua
responsável por reserva, lock, retry limitado, DLQ e ack. Resposta perdida depois
de PATCH é tratada com leitura do efeito antes de outra escrita. `Retry-After`
é repassado ao backoff da fila, sem um loop adicional.

Repetir o MESMO request encontra o evento persistido. Quando o worker o tiver
confirmado, o produtor lê o Notion novamente e somente então retorna
`readback_confirmed=true` e libera a próxima projeção. A mera coluna PROCESSED
não comprova o efeito externo. `todo_completed` permanece false.
Uma falha após gravar no Notion, mas antes do ack, deve retomar o mesmo evento.

## Habilitação controlada

O padrão continua desligado. Para publicação no DEV explicitamente autorizado,
o web e o worker devem usar a MESMA versão deste incremento. Atualizar o worker
antes de habilitar o produtor evita um consumidor antigo interpretar o envelope.
Não inferir prontidão da configuração: verificar ambos os processos e seu SHA.

Além da configuração normal do gateway/banco/Notion, são necessárias as referências:
`TODO_TRIAGE_MODE=dev`, `TODO_TRIAGE_ENVIRONMENT=DEV`,
`TODO_TRIAGE_PUBLISH_MODE=dev`, `TODO_TRIAGE_PAGE_ID`, `TODO_TRIAGE_OWNER` e
`PORTFOLIO_GITHUB_TOKEN_FILE`. O arquivo protegido usa permissões mínimas.
Valores de token, página e data source não pertencem ao repositório público.
Configuração incompleta bloqueia antes de consultar as fontes.

No modo de publicação as antigas rotas de mutação direta da reserva não são
instaladas; a entrada é o produtor tipado. A migração aditiva da reserva e as
migrações existentes da fila permanecem explícitas, fora de uma requisição HTTP.

Reversão: desabilitar novas publicações, drenar ou colocar em quarentena eventos
pendentes mantendo suas identidades e então reverter os processos juntos.
Não rebaixar o worker com eventos `triage_projection` ainda pendentes.

## Limites e critérios de aceite

Não há transação distribuída com o Notion nem compare-and-swap atômico externo.
O guard de versão detecta snapshots antigos, mas não protege um escritor que
ignore o gateway durante a janela entre GET e PATCH. Todos os produtores
operacionais precisam usar o caminho governado; escrita externa concorrente
exige reconciliação, nunca overwrite cego.

O CI usa HTTP, PostgreSQL e o worker reais com GitHub/Notion simulados e dados
sintéticos. Prova atomicidade local, protocolo do consumidor, replay, recuperação,
preservação e rejeições; NÃO prova credencial, permissão, disponibilidade ou efeito
no Notion real/PC24x7. Aceite externo exige um canário autorizado no mesmo SHA,
leitura por conexão independente, repetição sem página/patch duplicado e confirmação
de que estado de negócio e histórico foram preservados.

O consumidor confere a ligação entre envelope, fila e recibo de reserva no mesmo banco antes de qualquer acesso ao Notion. Um envelope inserido fora da transação autorizada falha fechado, mesmo que declare o nome do produtor.
