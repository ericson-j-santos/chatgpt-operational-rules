from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Protocol


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


class DecisionStore(Protocol):
    def get(self, correlation_id: str) -> RouteDecision | None: ...
    def put_if_absent(self, decision: RouteDecision) -> RouteDecision: ...


class HostRouter:
    def __init__(self, primary: str, secondary: str, decision_store: DecisionStore | None = None) -> None:
        self.primary, self.secondary = primary, secondary
        self.decision_store = decision_store
        self._seen: dict[str, RouteDecision] = {}
        self._epoch = 0

    def select(self, correlation_id: str, nodes: Iterable[NodeHealth], capability: str = '') -> RouteDecision:
        cached = self._seen.get(correlation_id)
        if cached is not None:
            return cached

        if self.decision_store is not None:
            persisted = self.decision_store.get(correlation_id)
            if persisted is not None:
                self._seen[correlation_id] = persisted
                return persisted

        available = {
            n.node_id: n for n in nodes
            if n.healthy and (not capability or capability in n.capabilities)
        }
        chosen = self.primary if self.primary in available else self.secondary if self.secondary in available else None
        reason = 'primary_healthy' if chosen == self.primary else (
            'primary_unavailable_failover' if chosen else 'no_healthy_capable_node'
        )

        if chosen is None or self.decision_store is None:
            self._epoch += 1
            decision = RouteDecision(correlation_id, chosen, reason, self._epoch)
        else:
            proposed = RouteDecision(correlation_id, chosen, reason, 0)
            decision = self.decision_store.put_if_absent(proposed)

        self._seen[correlation_id] = decision
        return decision


class PostgresDecisionStore:
    def __init__(self, dsn: str) -> None:
        if not dsn:
            raise ValueError('DATABASE_URL is required')
        self.dsn = dsn

    def _connect(self):
        import psycopg
        return psycopg.connect(self.dsn)

    @staticmethod
    def _decision(correlation_id: str, row) -> RouteDecision | None:
        if not row:
            return None
        node_id = None if row[0] is None else str(row[0])
        return RouteDecision(correlation_id, node_id, str(row[1]), int(row[2]))

    def get(self, correlation_id: str) -> RouteDecision | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                'SELECT node_id, reason, fencing_token FROM todo_bus.host_route_decisions WHERE correlation_id=%s',
                (correlation_id,),
            )
            row = cur.fetchone()
        return self._decision(correlation_id, row)

    def put_if_absent(self, decision: RouteDecision) -> RouteDecision:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                'SELECT node_id, reason, fencing_token FROM todo_bus.claim_host_route(%s,%s,%s)',
                (decision.correlation_id, decision.node_id, decision.reason),
            )
            row = cur.fetchone()
        persisted = self._decision(decision.correlation_id, row)
        if persisted is None:
            raise RuntimeError('host route claim returned no decision')
        return persisted
