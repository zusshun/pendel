"""Exercise sibling-mode sweep throttling without changing legacy stop regressions."""

import asyncio
import inspect
import unittest
from decimal import Decimal as D

import test_m27_resume as m27
from pendle.maker_orders import AccountState, Order, OrderManager, OrderStatus
from test_maker_orders import API, Clock, row


MARKET = 'IBM-USD.P'


class SiblingStopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock, self.api = Clock(), API()
        self.state = AccountState([MARKET])
        self.manager = OrderManager(self.state, self.api, risk_check=lambda *args: None,
                                    monotonic=self.clock, sleep=self.clock.sleep,
                                    request_timeout_s=2, stop_sweep_interval_s=5)
        self.state.deadman_ack()
        self.assertTrue(await self.manager.reconcile(MARKET))
        self.api.calls.clear()

    def sweeps(self):
        return sum(call[0] == 'cancel_all' for call in self.api.calls)

    async def test_first_stop_sweeps_and_repeated_confirmed_stop_is_throttled(self):
        self.assertTrue(await self.manager.stop('首次停止'))
        self.assertEqual(self.api.calls, [('cancel_all', MARKET, True), ('open', MARKET, True)])
        stop_task = self.manager._stop_task
        self.assertTrue(await self.manager.stop('重复停止'))
        self.assertEqual(self.sweeps(), 1)
        self.assertIs(self.manager._stop_task, stop_task)

    async def test_interval_expiry_sweeps_again(self):
        self.assertTrue(await self.manager.stop('首次停止'))
        self.clock.now += 4.99
        self.assertTrue(await self.manager.stop('间隔内停止'))
        self.assertEqual(self.sweeps(), 1)
        self.clock.now += .01
        self.assertTrue(await self.manager.stop('间隔到期停止'))
        self.assertEqual(self.sweeps(), 2)

    async def test_exit_force_sweeps_immediately(self):
        self.assertIn('force', inspect.signature(self.manager.stop).parameters)
        self.assertTrue(await self.manager.stop('普通停止'))
        self.assertTrue(await self.manager.stop('退出停止', force=True))
        self.assertEqual(self.sweeps(), 2)

    async def test_new_active_order_sweeps_immediately_even_without_event_revision(self):
        self.assertTrue(await self.manager.stop('首次停止'))
        self.state.orders['new'] = Order('new', MARKET, 'buy', D(2), D(100),
                                          status=OrderStatus.OPEN)
        self.assertTrue(await self.manager.stop('新挂单必须清扫'))
        self.assertEqual(self.sweeps(), 2)
        self.assertFalse(self.state.active_orders())

    async def test_late_acceptance_sweeps_immediately(self):
        order = self.manager.create_intent(MARKET, 'buy', '2', '100')
        await self.manager.submit(order)
        self.assertTrue(await self.manager.stop('首次停止'))
        await self.manager.handle_order_event(row(order.client_order_id, at=100))
        self.assertTrue(await self.manager.stop('延迟受理必须清扫'))
        self.assertEqual(self.sweeps(), 2)
        self.assertFalse(self.state.active_orders())

    async def test_relevant_position_event_invalidates_confirmed_zero(self):
        self.assertTrue(await self.manager.stop('首次停止'))
        self.state.note_position_event(MARKET)
        self.assertTrue(await self.manager.stop('新持仓事件后停止'))
        self.assertEqual(self.sweeps(), 2)

    async def test_balance_only_event_does_not_invalidate_market_zero(self):
        self.assertTrue(await self.manager.stop('首次停止'))
        self.state.apply_balance_snapshot({'equity': '1001', 'available': '901', 'time_ms': 20})
        self.assertTrue(await self.manager.stop('只有余额变化'))
        self.assertEqual(self.sweeps(), 1)

    async def test_inflight_transport_write_prevents_cached_confirmation(self):
        self.assertTrue(await self.manager.stop('首次停止'))
        self.api.in_flight_writes = 1
        self.assertFalse(await self.manager.stop('底层仍有写请求'))
        self.assertEqual(self.sweeps(), 4)
        self.api.in_flight_writes = 0
        self.assertTrue(await self.manager.stop('重新核实'))
        self.assertEqual(self.sweeps(), 5)

    async def test_pending_write_prevents_cached_confirmation(self):
        self.assertTrue(await self.manager.stop('首次停止'))
        writing = asyncio.get_running_loop().create_future()
        self.manager.pending['pending'] = writing
        try:
            self.assertFalse(await self.manager.stop('仍有在途写请求'))
            self.assertEqual(self.sweeps(), 4)
        finally:
            writing.set_result(None)
            self.manager.pending.clear()
            await asyncio.sleep(0)

    async def test_failed_stop_never_creates_cached_confirmation(self):
        self.api.open_rows = [row('remote')]
        self.assertFalse(await self.manager.stop('首次未核零'))
        self.assertFalse(await self.manager.stop('再次未核零'))
        self.assertEqual(self.sweeps(), 6)

    async def test_resume_clears_cached_stop_and_requires_reconciliation(self):
        self.assertTrue(await self.manager.stop('首次停止'))
        self.assertTrue(await self.manager.stop('已确认无需重扫'))
        self.assertTrue(self.manager.prepare_resume())
        self.assertEqual(self.state.market_mode(MARKET), 'SYNCING')
        self.assertTrue(await self.manager.reconcile(MARKET))
        self.assertTrue(await self.manager.stop('恢复后首次停止'))
        self.assertEqual(self.sweeps(), 2)

    async def test_default_interval_keeps_every_legacy_stop_sweep(self):
        manager = OrderManager(self.state, self.api, risk_check=lambda *args: None,
                               monotonic=self.clock, sleep=self.clock.sleep)
        self.assertTrue(await manager.stop('旧模式首次停止'))
        self.assertTrue(await manager.stop('旧模式重复停止'))
        self.assertEqual(self.sweeps(), 2)

    def test_constructor_accepts_interval_and_rejects_invalid_values(self):
        self.assertIn('stop_sweep_interval_s', inspect.signature(OrderManager).parameters)
        manager = OrderManager(self.state, self.api, risk_check=lambda *args: None,
                               stop_sweep_interval_s=5)
        self.assertEqual(manager.stop_sweep_interval_s, 5)
        for invalid in (-1, float('inf'), float('nan'), True):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                OrderManager(self.state, self.api, risk_check=lambda *args: None,
                             stop_sweep_interval_s=invalid)


class SiblingM27ResumeTests(m27.M27ResumeTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.bot.manager.stop_sweep_interval_s = 5

    async def start_drain_sweep(self):
        # Exercise M27 while a new periodic sweep is due, rather than a reused zero proof.
        self.clock.advance(5)
        self.publish()
        await super().start_drain_sweep()
