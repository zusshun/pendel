"""Open positions stream valuation pushes every ~80 ms; reconciliation must still settle (production 2026-11-07)."""
import unittest

import test_maker_orders as orders
from pendle.maker_orders import AccountState, OrderManager


class ValuationPushAPI(orders.API):
    """Deliver a changed private balance push in the middle of every REST snapshot read."""

    def __init__(self, state):
        super().__init__()
        self.state, self.tick = state, 0

    def push(self):
        self.tick += 1
        self.state.apply_balance_snapshot({'equity': str(1000 + self.tick), 'available': '900',
                                           'unrealized_pnl': str(self.tick)}, source='ws')

    async def get_open_orders(self, market, *, stop=False):
        self.push()
        return await super().get_open_orders(market, stop=stop)

    async def get_positions(self):
        self.push()
        return [{'market': 'IBM-USD.P', 'quantity': '0', 'time_ms': 10}]

    async def get_balances(self):
        self.push()
        return {'equity': '1', 'available': '1'}


class M31ReconcileValuationTests(unittest.IsolatedAsyncioTestCase):
    async def test_balance_valuation_pushes_during_reads_do_not_block_reconcile(self):
        clock = orders.Clock()
        state = AccountState(['IBM-USD.P'])
        api = ValuationPushAPI(state)
        manager = OrderManager(state, api, risk_check=lambda order, state: None,
                               monotonic=clock, sleep=clock.sleep, request_timeout_s=2)
        state.deadman_ack()
        revision = state.revision
        self.assertTrue(await manager.reconcile('IBM-USD.P'))
        self.assertEqual(state.revision >= revision, True)
        self.assertEqual(api.tick, 3)
        # The newer private push wins over the untimed REST balance read during the push.
        self.assertEqual(state.balances['equity'], '1003')


if __name__ == '__main__':
    unittest.main()
