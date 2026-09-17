from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

@dataclass(frozen=True)
class NodeHealth:
    node_id: str
    healthy: bool
    capabilities: frozenset[str] = frozenset()

@dataclass(frozen=True)
class RouteDecision:
    correlation_id: str
    node_id: str | None
    reason: str
    fencing_token: int

class HostRouter:
    def __init__(self, primary: str, secondary: str) -> None:
        self.primary, self.secondary = primary, secondary
        self._seen: dict[str, RouteDecision] = {}
        self._epoch = 0

    def select(self, correlation_id: str, nodes: Iterable[NodeHealth], capability: str = '') -> RouteDecision:
        if correlation_id in self._seen:
            return self._seen[correlation_id]
        available = {n.node_id: n for n in nodes if n.healthy and (not capability or capability in n.capabilities)}
        chosen = self.primary if self.primary in available else self.secondary if self.secondary in available else None
        self._epoch += 1
        reason = 'primary_healthy' if chosen == self.primary else ('primary_unavailable_failover' if chosen else 'no_healthy_capable_node')
        decision = RouteDecision(correlation_id, chosen, reason, self._epoch)
        self._seen[correlation_id] = decision
        return decision


class PostgresDecisionStore:
    def __init__(self, dsn: str) -> None:
        if not dsn: raise ValueError('DATABASE_URL is required')
        self.dsn = dsn

    def _connect(self):
        import psycopg
        return psycopg.connect(self.dsn)

    def get(self, correlation_id: str):
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute('SELECT node_id, reason, fencing_token FROM todo_bus.host_route_decisions WHERE correlation_id=%s', (correlation_id,))
            row = cur.fetchone()
        return RouteDecision(correlation_id, str(row[0]), str(row[1]), int(row[2])) if row else None

    def put_if_absent(self, decision: RouteDecision):
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute('SELECT node_id, reason, fencing_token FROM todo_bus.claim_host_route(%s,%s,%s)', (decision.correlation_id, decision.node_id, decision.reason))
            row = cur.fetchone()
        return RouteDecision(decision.correlation_id, str(row[0]), str(row[1]), int(row[2]))
