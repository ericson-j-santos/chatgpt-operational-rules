-- PC24x7 runtime node health for autonomous Desktop/Noteri routing.
-- Idempotent DEV-safe migration. No credentials or environment secrets.

CREATE TABLE IF NOT EXISTS todo_bus.runtime_node_heartbeat (
    node_id text PRIMARY KEY,
    heartbeat_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    healthy_since timestamptz NOT NULL DEFAULT clock_timestamp(),
    capabilities jsonb NOT NULL DEFAULT '[]'::jsonb,
    last_worker_id text,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE OR REPLACE FUNCTION todo_bus.touch_runtime_node_heartbeat(
    p_node_id text,
    p_worker_id text,
    p_capabilities jsonb DEFAULT '[]'::jsonb,
    p_ttl_seconds integer DEFAULT 20
)
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    IF length(trim(coalesce(p_node_id, ''))) = 0 THEN
        RAISE EXCEPTION 'p_node_id required';
    END IF;
    IF p_ttl_seconds < 5 OR p_ttl_seconds > 300 THEN
        RAISE EXCEPTION 'p_ttl_seconds out of range';
    END IF;

    INSERT INTO todo_bus.runtime_node_heartbeat(
        node_id, heartbeat_at, healthy_since, capabilities, last_worker_id, updated_at
    )
    VALUES (
        p_node_id,
        clock_timestamp(),
        clock_timestamp(),
        coalesce(p_capabilities, '[]'::jsonb),
        nullif(trim(coalesce(p_worker_id, '')), ''),
        clock_timestamp()
    )
    ON CONFLICT (node_id) DO UPDATE
    SET
        healthy_since = CASE
            WHEN todo_bus.runtime_node_heartbeat.heartbeat_at
                 < clock_timestamp() - make_interval(secs => p_ttl_seconds)
            THEN clock_timestamp()
            ELSE todo_bus.runtime_node_heartbeat.healthy_since
        END,
        heartbeat_at = clock_timestamp(),
        capabilities = EXCLUDED.capabilities,
        last_worker_id = EXCLUDED.last_worker_id,
        updated_at = clock_timestamp();
END
$$;

CREATE OR REPLACE FUNCTION todo_bus.get_runtime_node_health(
    p_primary_node text,
    p_secondary_node text,
    p_ttl_seconds integer DEFAULT 20,
    p_primary_stability_seconds integer DEFAULT 15
)
RETURNS TABLE(
    node_id text,
    healthy boolean,
    heartbeat_age_seconds integer,
    healthy_age_seconds integer,
    capabilities jsonb
)
LANGUAGE sql
AS $$
    WITH requested(node_id, priority) AS (
        VALUES (p_primary_node, 1), (p_secondary_node, 2)
    )
    SELECT
        r.node_id,
        CASE
            WHEN h.node_id IS NULL THEN false
            WHEN h.heartbeat_at < clock_timestamp() - make_interval(secs => p_ttl_seconds) THEN false
            WHEN r.node_id = p_primary_node
                 AND h.healthy_since > clock_timestamp() - make_interval(secs => p_primary_stability_seconds)
            THEN false
            ELSE true
        END AS healthy,
        CASE WHEN h.node_id IS NULL THEN 2147483647
             ELSE greatest(0, extract(epoch from clock_timestamp() - h.heartbeat_at)::integer)
        END AS heartbeat_age_seconds,
        CASE WHEN h.node_id IS NULL THEN 0
             ELSE greatest(0, extract(epoch from clock_timestamp() - h.healthy_since)::integer)
        END AS healthy_age_seconds,
        coalesce(h.capabilities, '[]'::jsonb)
    FROM requested r
    LEFT JOIN todo_bus.runtime_node_heartbeat h ON h.node_id = r.node_id
    ORDER BY r.priority;
$$;
