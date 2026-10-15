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
5. performs an independent readback of every supplied property, including evidence and next action;
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

## Safe partial updates and replay

Omitted optional fields are preserved. An explicitly supplied null/empty optional
text or URL clears that field; null clears optional selections. Required state and
identity fields are always checked. An identical persisted payload returns after
a read-only comparison instead of issuing another PATCH. Duplicate, incomplete,
archived or mismatched page results fail closed. Verification compares semantic
values, not Notion-generated IDs, formatting or colors, and never logs field values.

A producer updating an existing page may supply
`todo.expected_page_last_edited_at` from its trusted Notion snapshot. A stale version
blocks the write and requires a new snapshot; it must not be blindly retried.
This precondition is **not atomic server-side compare-and-swap**. It does not
replace the existing shared PostgreSQL idempotency lock, reserve a task for a chat,
or protect against writers that bypass that lock. Do not treat this increment as
completion of the cross-agent portfolio reservation tracked in issue #137.

The integrity suite is part of the existing gate, with positive, negative,
field-by-field false-success, replay, stale-snapshot and preservation controls.
Its MockTransport cases simulate Notion; they do not prove live Notion integration.
Deployment and end-to-end validation against the authorized real destination
remain separate gates, tied to the tested SHA.
