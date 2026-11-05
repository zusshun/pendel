"""Single-market (non-sibling) instances also throttle repeated confirmed-zero stop sweeps."""
import unittest
from dataclasses import replace

import test_m3_wiring as wiring
from pendle.maker_transport import build_maker


class M28SweepThrottleTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = wiring.M3WiringTests.asyncSetUp
    asyncTearDown = wiring.M3WiringTests.asyncTearDown
    drive = wiring.M3WiringTests.drive
    ready = wiring.M3WiringTests.ready
    wall = wiring.M3WiringTests.wall
    publish = wiring.M3WiringTests.publish

    async def test_default_single_market_settings_wire_five_second_throttle(self):
        from pendle.maker_config import MakerSettings
        self.assertEqual(MakerSettings().stop_sweep_interval_s, 5.0)
        settings = replace(self.settings, stop_sweep_interval_s=5.0)
        self.assertEqual(settings.sibling_markets, ())
        bot = build_maker(settings, env={
            'ONDO_API_KEY': '测试-key', 'ONDO_API_SECRET': '测试-secret',
            'TELEGRAM_BOT_TOKEN': '测试-token', 'TELEGRAM_CHAT_ID': '测试-chat'},
            transport=self.bot.api.transport, http=None, connect=None, files=self.files,
            rules=self.bot.rules, monotonic=self.clock.monotonic, clock=self.wall, sleep=self.clock.sleep)
        self.assertEqual(bot.manager.stop_sweep_interval_s, 5.0)

    async def test_repeated_stops_within_interval_send_one_sweep(self):
        await self.ready()
        self.bot.manager.stop_sweep_interval_s = 5.0
        self.assertTrue(await self.drive(self.bot.stop_orders('首次停止')))
        deletes = sum(r.method == 'DELETE' for r in self.requests)
        for _ in range(5):
            self.assertTrue(await self.drive(self.bot.stop_orders('重复停止')))
        self.assertEqual(sum(r.method == 'DELETE' for r in self.requests), deletes)
        self.clock.advance(5.1)
        self.assertTrue(await self.drive(self.bot.stop_orders('间隔后停止')))
        self.assertEqual(sum(r.method == 'DELETE' for r in self.requests), deletes + 1)


if __name__ == '__main__':
    unittest.main()


class M28StopPriorityTests(unittest.TestCase):
    def test_stop_verification_get_outranks_routine_reads(self):
        from pendle.ondo_api import _Pending
        def item(method, path, stop, seq, body=''):
            return _Pending(method, path, body, None, stop, None, seq)
        cancel_all = item('DELETE', '/v1/perps/orders?market=IBM-USD.P', True, 3)
        verify = item('GET', '/v1/perps/orders?market=IBM-USD.P&status=open', True, 2)
        routine = item('GET', '/v1/perps/positions', False, 1)
        new_order = item('POST', '/v1/perps/orders', False, 0, body='{"reduceOnly":false}')
        ranked = sorted((routine, verify, cancel_all, new_order), key=lambda e: (e.priority, e.sequence))
        self.assertEqual(ranked, [cancel_all, verify, new_order, routine])
