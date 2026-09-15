# TODO Global Event Gateway + Worker

Bootstrap service for the universal asynchronous TODO flow.

## Processes

- Gateway: `uvicorn services.todo_gateway.asgi:app --host 0.0.0.0 --port ${PORT:-8000}`
- Worker: `python -m services.todo_gateway.worker`

## Required environment

- `DATABASE_URL`: PostgreSQL connection string for an isolated TODO bus database/branch.
- `TODO_GATEWAY_TOKEN`: bearer token used by producers to publish events.
- `NOTION_TOKEN`: Notion internal/public integration token with query/insert/update access to TODO Global.
- `NOTION_DATA_SOURCE_ID`: Notion data source UUID for TODO Global.

Do not commit values. Configure them only in the authorized secret store of the target environment.

## Contract

`POST /v1/events` receives `TodoEvent v1`. HTTP 202 means **persisted/accepted by the queue**, not completed.

The gateway additionally rejects operationally invalid terminal states:

- `CONCLUÍDO` requires `completion_criteria` and `evidence`;
- `BLOQUEADO` requires `blocker` and `next_action`.

The worker:

1. reserves events with lease;
2. obtains a PostgreSQL advisory lock for the logical `idempotency_key`;
3. performs idempotent Notion lookup by `Chave de idempotência`;
4. creates or updates exactly one canonical TODO;
5. performs an independent readback of key, correlation id and status;
6. acknowledges only after readback succeeds;
7. retries transient failures;
8. sends permanent failures / exhausted transient failures to DLQ.

The daily reconciliation remains a safety net and is not the primary event path.

## Security

- fail closed when required environment is absent;
- bearer token comparison uses constant-time comparison;
- payload limit: 64 KiB;
- no credentials in logs or queue error text by design;
- Notion errors are reduced to status class instead of response bodies;
- production/deployment and secret configuration require explicit human authorization.
