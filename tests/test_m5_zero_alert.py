"""M5 evidence of zero open orders covers final alerts and waits for other markets while preserving the three-attempt retry budget."""

import unittest

from pendle.maker_orders import AccountState, OrderManager, OrderStatus
from test_maker import MARKET
from test_maker_orders import API, Clock, row


OTHER = 'OTHER-USD.P'


class ZeroAPI(API):
    def __init__(self, markets):
        super().__init__()
        self.remote = {market: {} for market in markets}
        self.snapshot_hook = None

    async def cancel_all(self, market, *, stop=False):
        await super().cancel_all(market, stop=stop)
        self.remote[market].clear()
        return {}

    async def get_open_orders(self, market, *, stop=False):
        self.calls.append(('open', market, stop))
        result = list(self.remote[market].values())
        if self.snapshot_hook is not None:
            await self.snapshot_hook(market)
        return result


class M5ZeroAlertTests(unittest.IsolatedAsyncioTestCase):
    def make_manager(self, markets=(MARKET,)):
        self.clock, self.state = Clock(), AccountState(markets)
        self.api, self.alerts, self.failures = ZeroAPI(markets), [], []
        self.alert_hook = None

        async def alert(reason):
            self.alerts.append(reason)
            if self.alert_hook is not None:
                await self.alert_hook()

        async def stop_failed(reason):
            self.failures.append(reason)

        self.manager = OrderManager(self.state, self.api, risk_check=lambda *args: None,
            monotonic=self.clock, sleep=self.clock.sleep, alert=alert, stop_failed=stop_failed)

    def new_order(self, cid, market=MARKET):
        event = dict(row(cid, at=100+len(self.state.orders)), market=market)
        self.api.remote[market][cid] = event
        self.state.apply_order_event(event)

    def cancels(self, market=MARKET):
        return sum(call[0] == 'cancel_all' and call[1] == market for call in self.api.calls)

    async def test_same_market_order_during_zero_alert_restarts_zero_before_success(self):
        self.make_manager()

        async def inject():
            if len(self.alerts) == 1:
                self.new_order('late-alert')

        self.alert_hook = inject
        self.assertTrue(await self.manager.stop('告警期间迟到订单'))
        self.assertEqual(self.api.remote[MARKET], {})
        self.assertFalse(self.state.active_orders(MARKET))
        self.assertEqual(self.state.orders['late-alert'].status, OrderStatus.CANCELED)
        self.assertEqual(self.cancels(), 2)
        self.assertEqual(self.failures, [])

    async def test_balance_during_zero_alert_keeps_zero_evidence(self):
        self.make_manager()

        async def inject():
            self.state.apply_balance_snapshot(dict(equity='1000', available='900',
                totalFundingPayments='0.01', time_ms=len(self.alerts)), source='ws')

        self.alert_hook = inject
        self.assertTrue(await self.manager.stop('告警期间余额推送'))
        self.assertEqual(self.cancels(), 1)
        self.assertEqual(len(self.alerts), 1)
        self.assertEqual(self.failures, [])

    async def test_position_event_during_zero_alert_restarts_market_evidence(self):
        self.make_manager()

        async def inject():
            if len(self.alerts) == 1:
                self.state.note_position_event(MARKET)

        self.alert_hook = inject
        self.assertTrue(await self.manager.stop('告警期间持仓推送'))
        self.assertEqual(self.cancels(), 2)
        self.assertEqual(self.failures, [])

    async def test_repeated_alert_races_use_at_most_three_market_cancel_attempts(self):
        self.make_manager()

        async def inject():
            self.new_order('late-alert-'+str(len(self.alerts)))

        self.alert_hook = inject
        self.assertFalse(await self.manager.stop('持续迟到订单'))
        self.assertEqual(self.cancels(), 3)
        self.assertEqual(len(self.alerts), 3)
        self.assertEqual(len(self.failures), 1)
        self.assertTrue(self.state.active_orders(MARKET))

    async def test_later_market_snapshot_invalidates_earlier_market_zero_evidence(self):
        self.make_manager((MARKET, OTHER))
        injected = False

        async def inject(market):
            nonlocal injected
            if market == OTHER and not injected:
                injected = True
                self.new_order('late-first-market')

        self.api.snapshot_hook = inject
        self.assertTrue(await self.manager.stop('其他市场等待期间第一市场迟到订单'))
        self.assertEqual(self.api.remote[MARKET], {})
        self.assertFalse(self.state.active_orders())
        self.assertEqual(self.cancels(MARKET), 2)
        self.assertEqual(self.cancels(OTHER), 1)
        self.assertEqual(self.failures, [])


if __name__ == '__main__':
    unittest.main()
