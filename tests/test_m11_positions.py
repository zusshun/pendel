"""M11 重复私有推送与核零回归；仅使用真实样本和内存传输。"""

import json
import unittest
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import test_m3_wiring as wiring
from pendle.maker_orders import AccountState, OrdersBlocked
from pendle.ondo_api import normalize_balance
from test_maker import MARKET
from test_maker_orders import row as order_row


SAMPLES = json.loads((Path(__file__).parent / 'fixtures' / 'live_samples' /
                      'ws_private_samples.json').read_text())


class M11SnapshotDedupTests(unittest.TestCase):
    def setUp(self):
        self.state = AccountState([MARKET])

    def test_identical_ws_position_keeps_market_version_and_advances_watermark(self):
        self.state.apply_position_snapshot(MARKET, '0', 100, source='ws')
        version = self.state.market_revisions[MARKET]
        for at in (180, 260, 340):
            self.state.apply_position_snapshot(MARKET, '0.0', at, source='ws')
        self.assertEqual(self.state.market_revisions[MARKET], version)
        self.assertEqual(self.state.position_bases[MARKET], (Decimal(0), 340, 'ws'))
        self.state.apply_fill('already-covered', MARKET, 'buy', '1', 260)
        self.assertEqual(self.state.positions[MARKET], 0)

    def test_changed_ws_position_increments_market_version(self):
        self.state.apply_position_snapshot(MARKET, '0', 100, source='ws')
        version = self.state.market_revisions[MARKET]
        self.state.apply_position_snapshot(MARKET, '1', 180, source='ws')
        self.assertEqual(self.state.market_revisions[MARKET], version + 1)
        self.assertEqual(self.state.positions[MARKET], 1)

    def test_duplicate_ws_position_preserves_rest_race_protection(self):
        self.state.apply_position_snapshot(MARKET, '2', 100, source='ws')
        version = self.state.market_revisions[MARKET]
        self.state.apply_position_snapshot(MARKET, '2', 180, source='ws')
        self.assertEqual(self.state.market_revisions[MARKET], version)
        self.assertFalse(self.state.apply_position_snapshot(MARKET, '0', 170))
        self.assertFalse(self.state.apply_position_snapshot(MARKET, '0', 180))
        self.assertEqual(self.state.positions[MARKET], 2)
        self.assertEqual(self.state.position_bases[MARKET], (Decimal(2), 180, 'ws'))
        self.assertIn('矛盾', self.state.sync_reasons[MARKET])

    def test_identical_live_balance_pushes_keep_revision_and_latest_watermark(self):
        balance = normalize_balance(SAMPLES['balance_update']['data'])
        self.state.apply_balance_snapshot(dict(balance, time_ms=100), source='ws')
        revision = self.state.revision
        for at in (180, 260, 340):
            self.state.apply_balance_snapshot(dict(balance, time_ms=at), source='ws')
        self.assertEqual(self.state.revision, revision)
        self.assertEqual(self.state.balance_ms, 340)
        self.assertEqual(self.state.balance_source, 'ws')
        self.assertFalse(self.state.apply_balance_snapshot(
            dict(balance, available='0', time_ms=340), source='rest'))
        self.assertEqual(self.state.balances['available'], '300')

    def test_changed_live_balance_increments_revision(self):
        balance = normalize_balance(SAMPLES['balance_update']['data'])
        self.state.apply_balance_snapshot(balance, source='ws')
        revision = self.state.revision
        self.state.apply_balance_snapshot(dict(balance, available='299'), source='ws')
        self.assertEqual(self.state.revision, revision + 1)


class M11PositionBurstTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = wiring.M3WiringTests.asyncSetUp
    asyncTearDown = wiring.M3WiringTests.asyncTearDown
    wall = wiring.M3WiringTests.wall
    drive = wiring.M3WiringTests.drive
    ready = wiring.M3WiringTests.ready
    publish = wiring.M3WiringTests.publish

    def position_message(self, quantity='0'):
        message = deepcopy(SAMPLES['positions_update_empty'])
        message['data'] = [{'market': MARKET, 'quantity': quantity,
                            'time_ms': int(self.wall() * 1000)}]
        return message

    async def test_identical_private_position_ignores_push_time_and_keeps_rest_authority(self):
        await self.ready()
        await self.bot.on_private(self.position_message())
        version = self.bot.state.market_revisions[MARKET]
        generation = self.bot.state.generations[MARKET]
        for _ in range(4):
            self.clock.advance(.08)
            await self.bot.on_private(self.position_message())
        self.assertEqual(self.bot.state.market_revisions[MARKET], version)
        self.assertEqual(self.bot.state.generations[MARKET], generation)
        await self.bot.on_private(self.position_message('1'))
        self.assertEqual(self.bot.state.market_revisions[MARKET], version + 1)
        self.assertEqual(self.bot.state.positions[MARKET], 0)
        self.assertEqual(self.bot.state.market_mode(MARKET), 'SYNCING')

    async def test_same_private_position_every_80ms_allows_zero_confirmation(self):
        await self.ready()
        await self.drive(self.bot.tick())
        await self.bot.on_private(self.position_message())
        self.requests.clear()
        transport = self.bot.api.transport
        pushes = 0

        async def repeated_position(request):
            nonlocal pushes
            response = await transport(request)
            if request.method == 'GET' and 'status=open' in request.path_query:
                for _ in range(4):
                    self.clock.advance(.08)
                    await self.bot.on_private(self.position_message())
                    pushes += 1
            return response

        self.bot.api.transport = repeated_position
        self.assertTrue(await self.drive(self.bot.guard.trigger('80ms 重复持仓核零')))
        self.assertFalse(self.bot.state.active_orders(MARKET))
        self.assertEqual(self.remote, {})
        self.assertEqual(self.bot.guard.state, 'COOLDOWN')
        self.assertEqual(pushes, 4)
        self.assertEqual(sum(request.method == 'DELETE' for request in self.requests), 1)

    async def test_empty_private_position_every_80ms_allows_zero_confirmation(self):
        await self.ready()
        await self.drive(self.bot.tick())
        await self.bot.on_private(deepcopy(SAMPLES['positions_update_empty']))
        self.requests.clear()
        transport = self.bot.api.transport
        pushes = 0

        async def repeated_empty_position(request):
            nonlocal pushes
            response = await transport(request)
            if request.method == 'GET' and 'status=open' in request.path_query:
                for _ in range(4):
                    self.clock.advance(.08)
                    await self.bot.on_private(deepcopy(SAMPLES['positions_update_empty']))
                    pushes += 1
            return response

        self.bot.api.transport = repeated_empty_position
        self.assertTrue(await self.drive(self.bot.guard.trigger('80ms 真实空持仓核零')))
        self.assertFalse(self.bot.state.active_orders(MARKET))
        self.assertEqual(self.remote, {})
        self.assertEqual(self.bot.guard.state, 'COOLDOWN')
        self.assertEqual(pushes, 4)
        self.assertEqual(sum(request.method == 'DELETE' for request in self.requests), 1)

    async def test_empty_private_position_zeros_observed_position_without_clearing_fill_barrier(self):
        await self.ready()
        state = self.bot.state
        order = self.bot.manager.create_intent(MARKET, 'buy', '2', '100')
        state.apply_order_event(order_row(order.client_order_id, status='PARTIAL',
                                         filled='1', at=100))
        state.apply_untimed_position_snapshot(MARKET, '2')
        state.apply_order_event(order_row(order.client_order_id, status='FILLED',
                                         filled='2', at=180))
        self.assertTrue(state.has_unaccounted_fills(MARKET))
        bases = dict(state.position_bases)
        coverage = dict(state._position_order_coverage)
        self.assertEqual(state.positions[MARKET], 2)

        await self.bot.on_private(deepcopy(SAMPLES['positions_update_empty']))

        self.assertEqual(state.positions[MARKET], 0)
        self.assertEqual(state.position_bases, bases)
        self.assertEqual(state._position_order_coverage, coverage)
        self.assertTrue(state.has_unaccounted_fills(MARKET))
        self.assertEqual(state.market_mode(MARKET), 'SYNCING')
        with self.assertRaises(OrdersBlocked):
            state.require_running(MARKET)

    async def test_repeated_private_positions_and_balances_do_not_emit_info(self):
        await self.ready()
        with patch.object(self.bot.logger, 'info') as info:
            for _ in range(5):
                self.clock.advance(.08)
                await self.bot.on_private(deepcopy(SAMPLES['positions_update_empty']))
                await self.bot.on_private(deepcopy(SAMPLES['balance_update']))
            info.assert_not_called()


if __name__ == '__main__':
    unittest.main()
