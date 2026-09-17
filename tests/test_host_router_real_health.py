import unittest
from scripts.host_router import HostRouter, NodeHealth

DESKTOP = 'DESKTOP-PDQK954'
NOTERI = 'Noteri'

class RealHealthControlledE2E(unittest.TestCase):
    def test_real_health_then_controlled_failover_and_recovery(self):
        router = HostRouter(DESKTOP, NOTERI)
        real = [NodeHealth(DESKTOP, True), NodeHealth(NOTERI, True)]
        normal = router.select('e2e-real-primary', real)
        self.assertEqual(normal.node_id, DESKTOP)
        simulated = [NodeHealth(DESKTOP, False), NodeHealth(NOTERI, True)]
        failover = router.select('e2e-controlled-failover', simulated)
        self.assertEqual(failover.node_id, NOTERI)
        repeated = router.select('e2e-controlled-failover', real)
        self.assertEqual(repeated, failover)
        recovered = router.select('e2e-recovered-primary', real)
        self.assertEqual(recovered.node_id, DESKTOP)
        self.assertGreater(recovered.fencing_token, failover.fencing_token)

if __name__ == '__main__': unittest.main()
