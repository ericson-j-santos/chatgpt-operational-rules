import unittest
from scripts.host_router import HostRouter, NodeHealth, RouteDecision

DESKTOP = 'DESKTOP-PDQK954'
NOTERI = 'Noteri'


class FakePersistentStore:
    def __init__(self):
        self.rows = {}
        self.epoch = 0

    def put_if_absent(self, decision):
        if decision.correlation_id not in self.rows:
            self.epoch += 1
            self.rows[decision.correlation_id] = RouteDecision(
                decision.correlation_id, decision.node_id, decision.reason, self.epoch
            )
        return self.rows[decision.correlation_id]

    def get(self, correlation_id):
        return self.rows.get(correlation_id)


class StoreContractTest(unittest.TestCase):
    def test_router_restart_reuses_persisted_failover(self):
        store = FakePersistentStore()
        first_router = HostRouter(DESKTOP, NOTERI, store)
        first = first_router.select(
            'corr-restart', [NodeHealth(DESKTOP, False), NodeHealth(NOTERI, True)]
        )
        restarted_router = HostRouter(DESKTOP, NOTERI, store)
        second = restarted_router.select(
            'corr-restart', [NodeHealth(DESKTOP, True), NodeHealth(NOTERI, True)]
        )
        self.assertEqual(first, second)
        self.assertEqual(second.node_id, NOTERI)

    def test_atomic_store_winner_is_returned(self):
        store = FakePersistentStore()
        first = HostRouter(DESKTOP, NOTERI, store).select(
            'corr-race', [NodeHealth(DESKTOP, False), NodeHealth(NOTERI, True)]
        )
        second = HostRouter(DESKTOP, NOTERI, store).select(
            'corr-race', [NodeHealth(DESKTOP, True), NodeHealth(NOTERI, True)]
        )
        self.assertEqual(first, second)
        self.assertEqual(len(store.rows), 1)

    def test_fencing_increases_for_new_correlation(self):
        store = FakePersistentStore()
        router = HostRouter(DESKTOP, NOTERI, store)
        a = router.select('a', [NodeHealth(DESKTOP, False), NodeHealth(NOTERI, True)])
        b = router.select('b', [NodeHealth(DESKTOP, True), NodeHealth(NOTERI, True)])
        self.assertGreater(b.fencing_token, a.fencing_token)

    def test_no_healthy_node_is_explicit_and_not_persisted(self):
        store = FakePersistentStore()
        decision = HostRouter(DESKTOP, NOTERI, store).select(
            'blocked', [NodeHealth(DESKTOP, False), NodeHealth(NOTERI, False)]
        )
        self.assertIsNone(decision.node_id)
        self.assertEqual(decision.reason, 'no_healthy_capable_node')
        self.assertNotIn('blocked', store.rows)
