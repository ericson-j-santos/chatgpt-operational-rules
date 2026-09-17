import unittest
from scripts.host_router import HostRouter, NodeHealth

class HostRouterTest(unittest.TestCase):
    def setUp(self):
        self.router = HostRouter('DESKTOP-PDQK954', 'Noteri')

    def test_primary_healthy_routes_desktop(self):
        d = self.router.select('c1', [NodeHealth('DESKTOP-PDQK954', True), NodeHealth('Noteri', True)])
        self.assertEqual(d.node_id, 'DESKTOP-PDQK954')
        self.assertEqual(d.reason, 'primary_healthy')

    def test_primary_unhealthy_routes_noteri(self):
        d = self.router.select('c2', [NodeHealth('DESKTOP-PDQK954', False), NodeHealth('Noteri', True)])
        self.assertEqual(d.node_id, 'Noteri')
        self.assertEqual(d.reason, 'primary_unavailable_failover')

    def test_repeat_correlation_does_not_reassign(self):
        first = self.router.select('same', [NodeHealth('DESKTOP-PDQK954', False), NodeHealth('Noteri', True)])
        second = self.router.select('same', [NodeHealth('DESKTOP-PDQK954', True), NodeHealth('Noteri', True)])
        self.assertEqual(first, second)

    def test_new_correlation_fails_back_to_recovered_primary(self):
        self.router.select('old', [NodeHealth('DESKTOP-PDQK954', False), NodeHealth('Noteri', True)])
        d = self.router.select('new', [NodeHealth('DESKTOP-PDQK954', True), NodeHealth('Noteri', True)])
        self.assertEqual(d.node_id, 'DESKTOP-PDQK954')
        self.assertGreater(d.fencing_token, 1)

if __name__ == '__main__':
    unittest.main()
