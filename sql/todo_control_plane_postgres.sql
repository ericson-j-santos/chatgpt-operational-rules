-- TODO Global control plane — PostgreSQL continuation queue.
-- Idempotent migration. No secrets or environment-specific identifiers.

CREATE SCHEMA IF NOT EXISTS todo_bus;

CREATE TABLE IF NOT EXISTS todo_bus.continuation_requests (
    request_id text PRIMARY KEY,
    idempotency_key text NOT NULL
        CHECK (idempotency_key ~ '^[0-9a-f]{64}$'),
    basis_event_id text NOT NULL,
    correlation_id text NOT NULL
        CHECK (length(correlation_id) BETWEEN 8 AND 128),
    project text NOT NULL
        CHECK (length(trim(project)) > 0),
    todo_payload jsonb NOT NULL,
    state text NOT NULL DEFAULT 'PENDING'
        CHECK (state IN ('PENDING','PROCESSING','HUMAN_GATE','COMPLETED','DLQ')),
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

CREATE UNIQUE INDEX IF NOT EXISTS ux_continuation_active_basis
    ON todo_bus.continuation_requests (idempotency_key, basis_event_id)
    WHERE state IN ('PENDING','PROCESSING','HUMAN_GATE');

CREATE INDEX IF NOT EXISTS ix_continuation_ready
    ON todo_bus.continuation_requests (state, available_at, created_at);

CREATE INDEX IF NOT EXISTS ix_continuation_lease
    ON todo_bus.continuation_requests (lease_until)
    WHERE state = 'PROCESSING';

CREATE TABLE IF NOT EXISTS todo_bus.continuation_history (
    history_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    request_id text NOT NULL,
    from_state text,
    to_state text NOT NULL,
    attempts integer NOT NULL,
    correlation_id text NOT NULL,
    detail text,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS ix_continuation_history_request
    ON todo_bus.continuation_history (request_id, history_id);

CREATE OR REPLACE FUNCTION todo_bus.audit_continuation_state()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        INSERT INTO todo_bus.continuation_history(
            request_id, from_state, to_state, attempts, correlation_id, detail
        )
        VALUES (
            NEW.request_id, NULL, NEW.state, NEW.attempts, NEW.correlation_id, NULL
        );
    ELSIF NEW.state IS DISTINCT FROM OLD.state
       OR NEW.attempts IS DISTINCT FROM OLD.attempts THEN
        INSERT INTO todo_bus.continuation_history(
            request_id, from_state, to_state, attempts, correlation_id, detail
        )
        VALUES (
            NEW.request_id,
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

DROP TRIGGER IF EXISTS trg_continuation_audit
    ON todo_bus.continuation_requests;

CREATE TRIGGER trg_continuation_audit
AFTER INSERT OR UPDATE OF state, attempts
ON todo_bus.continuation_requests
FOR EACH ROW
EXECUTE FUNCTION todo_bus.audit_continuation_state();

CREATE OR REPLACE FUNCTION todo_bus.reserve_continuations(
    p_limit integer DEFAULT 10,
    p_lease_seconds integer DEFAULT 120
)
RETURNS SETOF todo_bus.continuation_requests
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
        SELECT c.request_id
        FROM todo_bus.continuation_requests c
        WHERE
            (c.state = 'PENDING' AND c.available_at <= clock_timestamp())
            OR
            (c.state = 'PROCESSING' AND c.lease_until <= clock_timestamp())
        ORDER BY c.created_at, c.request_id
        FOR UPDATE SKIP LOCKED
        LIMIT p_limit
    )
    UPDATE todo_bus.continuation_requests c
    SET
        state = 'PROCESSING',
        attempts = c.attempts + 1,
        lease_until = clock_timestamp() + make_interval(secs => p_lease_seconds),
        updated_at = clock_timestamp()
    FROM eligible e
    WHERE c.request_id = e.request_id
    RETURNING c.*;
END
$$;

CREATE OR REPLACE FUNCTION todo_bus.complete_continuation(
    p_request_id text
)
RETURNS boolean
LANGUAGE plpgsql
AS $$
DECLARE
    changed integer;
BEGIN
    UPDATE todo_bus.continuation_requests
    SET
        state = 'COMPLETED',
        lease_until = NULL,
        last_error = NULL,
        updated_at = clock_timestamp()
    WHERE request_id = p_request_id
      AND state = 'PROCESSING';

    GET DIAGNOSTICS changed = ROW_COUNT;
    RETURN changed = 1;
END
$$;

CREATE OR REPLACE FUNCTION todo_bus.gate_continuation(
    p_request_id text,
    p_detail text
)
RETURNS boolean
LANGUAGE plpgsql
AS $$
DECLARE
    changed integer;
BEGIN
    UPDATE todo_bus.continuation_requests
    SET
        state = 'HUMAN_GATE',
        lease_until = NULL,
        last_error = left(replace(coalesce(p_detail, ''), E'\n', ' '), 1000),
        updated_at = clock_timestamp()
    WHERE request_id = p_request_id
      AND state = 'PROCESSING';

    GET DIAGNOSTICS changed = ROW_COUNT;
    RETURN changed = 1;
END
$$;

CREATE OR REPLACE FUNCTION todo_bus.fail_continuation(
    p_request_id text,
    p_error text,
    p_max_attempts integer DEFAULT 3,
    p_backoff_seconds integer DEFAULT 30
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
    FROM todo_bus.continuation_requests
    WHERE request_id = p_request_id
      AND state = 'PROCESSING'
    FOR UPDATE;

    IF current_attempts IS NULL THEN
        RAISE EXCEPTION 'continuation not reserved: %', p_request_id;
    END IF;
    IF p_max_attempts < 1 OR p_backoff_seconds < 0 THEN
        RAISE EXCEPTION 'invalid retry parameters';
    END IF;

    target_state := CASE
        WHEN current_attempts >= p_max_attempts THEN 'DLQ'
        ELSE 'PENDING'
    END;

    UPDATE todo_bus.continuation_requests
    SET
        state = target_state,
        available_at = CASE
            WHEN target_state = 'DLQ' THEN clock_timestamp()
            ELSE clock_timestamp() + make_interval(secs => p_backoff_seconds)
        END,
        lease_until = NULL,
        last_error = left(replace(coalesce(p_error, ''), E'\n', ' '), 1000),
        updated_at = clock_timestamp()
    WHERE request_id = p_request_id;

    RETURN target_state;
END
$$;


CREATE TABLE IF NOT EXISTS todo_bus.worker_heartbeat (
    worker_id text PRIMARY KEY,
    heartbeat_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    last_counts jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE OR REPLACE FUNCTION todo_bus.touch_worker_heartbeat(p_worker_id text, p_counts jsonb)
RETURNS void LANGUAGE sql AS $$
    INSERT INTO todo_bus.worker_heartbeat(worker_id, heartbeat_at, last_counts)
    VALUES (p_worker_id, clock_timestamp(), coalesce(p_counts, '{}'::jsonb))
    ON CONFLICT (worker_id) DO UPDATE SET heartbeat_at=EXCLUDED.heartbeat_at, last_counts=EXCLUDED.last_counts;
$$;
