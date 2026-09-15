-- TODO Global async event bus — PostgreSQL reference
-- Idempotent migration for isolated DEV/staging branches.
-- No secrets, credentials or environment-specific identifiers.

CREATE SCHEMA IF NOT EXISTS todo_bus;

CREATE TABLE IF NOT EXISTS todo_bus.queue_events (
    event_id text PRIMARY KEY,
    schema_version text NOT NULL DEFAULT '1.0'
        CHECK (schema_version = '1.0'),
    event_type text NOT NULL
        CHECK (event_type IN (
            'todo.created',
            'todo.updated',
            'todo.status.changed',
            'todo.evidence.updated',
            'todo.reconcile.requested'
        )),
    idempotency_key text NOT NULL
        CHECK (idempotency_key ~ '^[0-9a-f]{64}$'),
    correlation_id text NOT NULL
        CHECK (length(correlation_id) BETWEEN 8 AND 128),
    project text NOT NULL
        CHECK (length(trim(project)) > 0),
    producer text,
    payload jsonb NOT NULL,
    state text NOT NULL DEFAULT 'PENDING'
        CHECK (state IN ('PENDING','PROCESSING','PROCESSED','DLQ')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    lease_until timestamptz,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (
        (state = 'PROCESSING' AND lease_until IS NOT NULL)
        OR state <> 'PROCESSING'
    )
);

CREATE INDEX IF NOT EXISTS ix_todo_queue_ready
    ON todo_bus.queue_events (state, available_at, created_at);

CREATE INDEX IF NOT EXISTS ix_todo_queue_lease
    ON todo_bus.queue_events (lease_until)
    WHERE state = 'PROCESSING';

CREATE INDEX IF NOT EXISTS ix_todo_queue_idempotency
    ON todo_bus.queue_events (idempotency_key, created_at DESC);

CREATE TABLE IF NOT EXISTS todo_bus.queue_event_history (
    history_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id text NOT NULL,
    from_state text,
    to_state text NOT NULL,
    attempts integer NOT NULL,
    correlation_id text NOT NULL,
    detail text,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS ix_todo_queue_history_event
    ON todo_bus.queue_event_history (event_id, history_id);

CREATE OR REPLACE FUNCTION todo_bus.audit_queue_state()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        INSERT INTO todo_bus.queue_event_history(
            event_id, from_state, to_state, attempts, correlation_id, detail
        )
        VALUES (
            NEW.event_id, NULL, NEW.state, NEW.attempts, NEW.correlation_id, NULL
        );
    ELSIF NEW.state IS DISTINCT FROM OLD.state
       OR NEW.attempts IS DISTINCT FROM OLD.attempts THEN
        INSERT INTO todo_bus.queue_event_history(
            event_id, from_state, to_state, attempts, correlation_id, detail
        )
        VALUES (
            NEW.event_id,
            OLD.state,
            NEW.state,
            NEW.attempts,
            NEW.correlation_id,
            NEW.last_error
        );
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS trg_todo_queue_audit ON todo_bus.queue_events;

CREATE TRIGGER trg_todo_queue_audit
AFTER INSERT OR UPDATE OF state, attempts
ON todo_bus.queue_events
FOR EACH ROW
EXECUTE FUNCTION todo_bus.audit_queue_state();

CREATE OR REPLACE FUNCTION todo_bus.reserve_events(
    p_limit integer DEFAULT 10,
    p_lease_seconds integer DEFAULT 60
)
RETURNS SETOF todo_bus.queue_events
LANGUAGE plpgsql
AS $$
BEGIN
    IF p_limit < 1 OR p_limit > 100 THEN
        RAISE EXCEPTION 'p_limit out of range';
    END IF;
    IF p_lease_seconds < 1 OR p_lease_seconds > 3600 THEN
        RAISE EXCEPTION 'p_lease_seconds out of range';
    END IF;

    RETURN QUERY
    WITH eligible AS (
        SELECT q.event_id
        FROM todo_bus.queue_events q
        WHERE
            (q.state = 'PENDING' AND q.available_at <= clock_timestamp())
            OR
            (q.state = 'PROCESSING' AND q.lease_until <= clock_timestamp())
        ORDER BY q.created_at, q.event_id
        FOR UPDATE SKIP LOCKED
        LIMIT p_limit
    )
    UPDATE todo_bus.queue_events q
    SET
        state = 'PROCESSING',
        attempts = q.attempts + 1,
        lease_until = clock_timestamp() + make_interval(secs => p_lease_seconds),
        updated_at = clock_timestamp()
    FROM eligible e
    WHERE q.event_id = e.event_id
    RETURNING q.*;
END
$$;

CREATE OR REPLACE FUNCTION todo_bus.ack_event(p_event_id text)
RETURNS boolean
LANGUAGE plpgsql
AS $$
DECLARE
    changed integer;
BEGIN
    UPDATE todo_bus.queue_events
    SET
        state = 'PROCESSED',
        lease_until = NULL,
        last_error = NULL,
        updated_at = clock_timestamp()
    WHERE event_id = p_event_id
      AND state = 'PROCESSING';

    GET DIAGNOSTICS changed = ROW_COUNT;
    RETURN changed = 1;
END
$$;

CREATE OR REPLACE FUNCTION todo_bus.fail_event(
    p_event_id text,
    p_error text,
    p_max_attempts integer DEFAULT 3,
    p_backoff_seconds integer DEFAULT 0
)
RETURNS text
LANGUAGE plpgsql
AS $$
DECLARE
    current_attempts integer;
    target_state text;
BEGIN
    SELECT attempts
    INTO current_attempts
    FROM todo_bus.queue_events
    WHERE event_id = p_event_id
      AND state = 'PROCESSING'
    FOR UPDATE;

    IF current_attempts IS NULL THEN
        RAISE EXCEPTION 'event not reserved: %', p_event_id;
    END IF;
    IF p_max_attempts < 1 OR p_backoff_seconds < 0 THEN
        RAISE EXCEPTION 'invalid retry parameters';
    END IF;

    target_state := CASE
        WHEN current_attempts >= p_max_attempts THEN 'DLQ'
        ELSE 'PENDING'
    END;

    UPDATE todo_bus.queue_events
    SET
        state = target_state,
        available_at = CASE
            WHEN target_state = 'DLQ' THEN clock_timestamp()
            ELSE clock_timestamp() + make_interval(secs => p_backoff_seconds)
        END,
        lease_until = NULL,
        last_error = left(
            replace(coalesce(p_error, ''), E'\n', ' '),
            1000
        ),
        updated_at = clock_timestamp()
    WHERE event_id = p_event_id;

    RETURN target_state;
END
$$;
