import unittest
from scripts.host_router import RouteDecision

class FakePersistentStore:
    def __init__(self): self.rows={}; self.epoch=0
    def put_if_absent(self,d):
        if d.correlation_id not in self.rows:
            self.epoch += 1
            self.rows[d.correlation_id]=RouteDecision(d.correlation_id,d.node_id,d.reason,self.epoch)
        return self.rows[d.correlation_id]
    def get(self,c): return self.rows.get(c)

class StoreContractTest(unittest.TestCase):
    def test_repeat_survives_router_restart(self):
        store=FakePersistentStore()
        first=store.put_if_absent(RouteDecision('corr','Noteri','primary_unavailable_failover',0))
        second=store.put_if_absent(RouteDecision('corr','DESKTOP-PDQK954','primary_healthy',0))
        self.assertEqual(first,second)
        self.assertEqual(second.node_id,'Noteri')
    def test_fencing_increases_for_new_correlation(self):
        store=FakePersistentStore()
        a=store.put_if_absent(RouteDecision('a','Noteri','failover',0))
        b=store.put_if_absent(RouteDecision('b','DESKTOP-PDQK954','primary_healthy',0))
        self.assertGreater(b.fencing_token,a.fencing_token)
