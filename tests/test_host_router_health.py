from __future__ import annotations

import unittest

from scripts.host_router import DynamicNodeHealth, HostRouter, NodeHealth, RouteDecision

DESKTOP = "DESKTOP-PDQK954"
NOTERI = "Noteri"


class FakeHealthStore:
    def __init__(self, snapshots):
        self.snapshots = list(snapshots)
        self.calls = 0

    def snapshot(self, primary, secondary, ttl_seconds, primary_stability_seconds):
        index = min(self.calls, len(self.snapshots) - 1)
        self.calls += 1
        return self.snapshots[index]


class FakeDecisionStore:
    def __init__(self):
        self.rows = {}
        self.epoch = 0

    def get(self, correlation_id):
        return self.rows.get(correlation_id)

    def put_if_absent(self, decision):
        if decision.correlation_id not in self.rows:
            self.epoch += 1
            self.rows[decision.correlation_id] = RouteDecision(
                decision.correlation_id, decision.node_id, decision.reason, self.epoch
            )
        return self.rows[decision.correlation_id]


class DynamicHealthTest(unittest.TestCase):
    def test_primary_unstable_routes_secondary(self):
        source = DynamicNodeHealth(
            FakeHealthStore([[NodeHealth(DESKTOP, False), NodeHealth(NOTERI, True)]]),
            DESKTOP, NOTERI, 20, 15,
        )
        decision = HostRouter(DESKTOP, NOTERI, FakeDecisionStore()).select("corr-a", source)
        self.assertEqual(decision.node_id, NOTERI)
        self.assertEqual(decision.reason, "primary_unavailable_failover")

    def test_primary_stable_routes_primary(self):
        source = DynamicNodeHealth(
            FakeHealthStore([[NodeHealth(DESKTOP, True), NodeHealth(NOTERI, True)]]),
            DESKTOP, NOTERI, 20, 15,
        )
        decision = HostRouter(DESKTOP, NOTERI, FakeDecisionStore()).select("corr-b", source)
        self.assertEqual(decision.node_id, DESKTOP)
        self.assertEqual(decision.reason, "primary_healthy")

    def test_new_snapshot_is_read_for_new_correlation(self):
        store = FakeHealthStore([
            [NodeHealth(DESKTOP, False), NodeHealth(NOTERI, True)],
            [NodeHealth(DESKTOP, True), NodeHealth(NOTERI, True)],
        ])
        source = DynamicNodeHealth(store, DESKTOP, NOTERI, 20, 15)
        router = HostRouter(DESKTOP, NOTERI, FakeDecisionStore())
        self.assertEqual(router.select("corr-c1", source).node_id, NOTERI)
        self.assertEqual(router.select("corr-c2", source).node_id, DESKTOP)
        self.assertEqual(store.calls, 2)

    def test_persisted_failover_does_not_move_after_primary_returns(self):
        decisions = FakeDecisionStore()
        first_source = DynamicNodeHealth(
            FakeHealthStore([[NodeHealth(DESKTOP, False), NodeHealth(NOTERI, True)]]),
            DESKTOP, NOTERI, 20, 15,
        )
        first = HostRouter(DESKTOP, NOTERI, decisions).select("corr-fixed", first_source)
        second_source = DynamicNodeHealth(
            FakeHealthStore([[NodeHealth(DESKTOP, True), NodeHealth(NOTERI, True)]]),
            DESKTOP, NOTERI, 20, 15,
        )
        second = HostRouter(DESKTOP, NOTERI, decisions).select("corr-fixed", second_source)
        self.assertEqual(first, second)
        self.assertEqual(second.node_id, NOTERI)


if __name__ == "__main__":
    unittest.main()
