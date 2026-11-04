"""M20: reconciliation semantics for real empty REST records and existing synchronization barriers."""

import asyncio
import json
import logging
import unittest
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path

import test_maker_e2e as e2e
from pendle.maker import MarketRules
from pendle.maker_config import MakerSettings
from pendle.maker_transport import build_maker
from pendle.ondo_api import HttpResponse
from test_maker import MARKET
from test_ondo_api import FakeClock, settle


SAMPLES = json.loads((Path(__file__).parent / 'fixtures/live_samples/'
                      'ws_private_samples.json').read_text())


class M20PositionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock, self.files, self.alerts = FakeClock(), e2e.Files(), e2e.Alerts()
        self.positions, self.requests, self.position_times = [], [], []
        self.during_positions = None

        async def network(request):
            self.requests.append(request)
            path = request.path_query.partition('?')[0]
            if path == '/v1/perps/positions':
                self.position_times.append(self.clock.monotonic())
                if self.during_positions is not None:
                    self.during_positions()
                result = self.positions
            elif path == '/v1/perps/balance':
                result = SAMPLES['balance_update']['data']
            elif request.method == 'DELETE':
                result = {}
            else:
                result = []
            return HttpResponse(200, {'success': True, 'result': result})

        self.bot = build_maker(
            replace(MakerSettings(), mode='sandbox', quiet_s=3, rate_per_second=1000),
            env={'ONDO_API_KEY': '测试-key', 'ONDO_API_SECRET': '测试-secret'},
            transport=network, alerter=self.alerts, files=self.files,
            rules=MarketRules(D('.01'), D('.01')), logger=logging.getLogger(self.id()),
            monotonic=self.clock.monotonic, clock=self.clock.time, sleep=self.clock.sleep)
        await self.bot.startup()
        self.bot.state.deadman_ack()

    async def drive(self, job):
        task = asyncio.ensure_future(job)
        for _ in range(1000):
            await settle()
            if task.done():
                return await task
            self.clock.advance(.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.fail('内存模拟超过预算')

    async def asyncTearDown(self):
        await self.drive(self.bot.shutdown())

    def other(self, quantity='2'):
        return {'market': 'OTHER-USD.P', 'direction': 'long', 'netQuantity': quantity,
                'markPrice': '100', 'unrealizedPnl': '0'}

    def assert_neutral(self):
        self.assertEqual(self.bot.state.positions[MARKET], 0)
        self.assertIn(MARKET, self.bot.state.position_details)
        position = self.bot.state.position_details[MARKET]
        self.assertEqual(position['direction'], 'neutral')
        self.assertEqual(D(position['quantity']), 0)
        self.assertEqual(D(position['unrealized_pnl']), 0)
        self.assertNotIn('time_ms', position)

    async def test_rest_empty_positions_reconciles_as_neutral_zero(self):
        self.assertTrue(await self.drive(self.bot.reconcile_once()))
        self.assert_neutral()
        self.assertEqual(self.bot.state.account_positions, [])
        self.assertEqual(self.bot.state.market_mode(MARKET), 'RUNNING')
        self.assertGreaterEqual(self.position_times[0], 3)
        self.assertFalse(self.files.exists(self.bot.settings.halt_path))

    async def test_only_other_zero_position_reconciles_target_as_neutral_zero(self):
        self.positions = [self.other('0')]
        self.assertTrue(await self.drive(self.bot.reconcile_once()))
        self.assert_neutral()
        self.assertEqual(len(self.bot.state.account_positions), 1)
        self.assertEqual(self.bot.state.account_positions[0]['market'], 'OTHER-USD.P')

    async def test_only_other_nonzero_position_still_halts_with_target_zero(self):
        self.positions = [self.other()]
        self.assertFalse(await self.drive(self.bot.reconcile_once()))
        self.assert_neutral()
        reason = self.files.rows[str(self.bot.settings.halt_path)]['reason']
        self.assertIn('其他市场持仓', reason)
        self.assertNotIn('STOP_FAILED', reason)
        self.assertFalse(any(request.method == 'POST' for request in self.requests))

    async def test_missing_position_rebases_nonzero_then_requires_quiet_confirmation(self):
        self.bot.state.apply_untimed_position_snapshot(MARKET, '2')
        self.assertFalse(await self.drive(self.bot.manager.reconcile(MARKET)))
        self.assertEqual(self.bot.state.positions[MARKET], 0)
        self.assertEqual(self.bot.state.market_mode(MARKET), 'SYNCING')
        self.assertTrue(await self.drive(self.bot.manager.reconcile(MARKET)))
        self.assert_neutral()
        self.assertGreaterEqual(self.position_times[-1] - self.position_times[0], 3)

    async def test_empty_positions_during_fill_cannot_release_sync_barrier(self):
        self.during_positions = lambda: self.bot.state.apply_fill(
            'concurrent-fill', MARKET, 'buy', '1', 100)
        self.assertFalse(await self.drive(self.bot.manager.reconcile(MARKET)))
        self.assertEqual(self.bot.state.positions[MARKET], 1)
        self.assertEqual(self.bot.state.market_mode(MARKET), 'SYNCING')
        self.assertNotIn(MARKET, self.bot.state.position_details)

    async def test_duplicate_target_positions_still_rejected(self):
        position = {'market': MARKET, 'direction': 'neutral', 'netQuantity': '0',
                    'unrealizedPnl': '0'}
        self.positions = [position, dict(position)]
        self.assertFalse(await self.drive(self.bot.manager.reconcile(MARKET)))
        self.assertEqual(self.bot.state.market_mode(MARKET), 'SYNCING')

    async def test_rest_empty_open_orders_is_empty_and_confirms_stop(self):
        self.assertEqual(await self.drive(self.bot.api.get_open_orders(MARKET)), [])
        self.assertTrue(await self.drive(self.bot.stop_orders('M20 空挂单核实')))
        self.assertFalse(self.bot.state.active_orders())
        self.assertFalse(self.files.exists(self.bot.settings.halt_path))

    async def test_rest_empty_fills_has_no_accounting_effect(self):
        self.assertEqual(await self.drive(self.bot.api.get_fills(MARKET)), [])
        self.assertEqual(self.bot.state.fills, {})
        self.assertEqual(self.bot.state.realized_pnl, 0)
        self.assertEqual(self.bot.state.fees, 0)


if __name__ == '__main__':
    unittest.main()
