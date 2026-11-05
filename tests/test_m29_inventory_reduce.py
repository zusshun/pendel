"""M29 inventory-cap regressions through the real Maker builder and mock exchange."""

import json
import unittest
from dataclasses import replace
from decimal import Decimal as D
from unittest.mock import patch

from pendle.ondo_api import HttpResponse
from pendle.maker_risk import RiskManager
from test_maker import MARKET, book
import test_m3_wiring as wiring


PAUSE_REASON = '库存达到上限，仅挂 reduceOnly 减仓单'
EXCHANGE_REASON = '交易所最大基础持仓量限制，双侧停止报价'


class InventoryCapReductionTests(unittest.IsolatedAsyncioTestCase):
    wall = wiring.M3WiringTests.wall
    drive = wiring.M3WiringTests.drive
    ready = wiring.M3WiringTests.ready
    asyncTearDown = wiring.M3WiringTests.asyncTearDown

    async def asyncSetUp(self):
        await wiring.M3WiringTests.asyncSetUp(self)
        self.bot.rules = replace(self.bot.rules, increment=D('.01'),
                                 max_position_base_size=D('4300'))
        self.rest_fills = []
        original = self.bot.api.transport

        async def transport(request):
            path = request.path_query.partition('?')[0]
            if request.method == 'GET' and path.endswith('/fills'):
                self.requests.append(request)
                return HttpResponse(200, {'success': True, 'result': self.rest_fills[:]})
            return await original(request)

        self.bot.api.transport = transport
        self.socket_patch = patch('socket.socket.connect',
                                  side_effect=AssertionError('回放禁止联网'))
        self.socket_patch.start()
        self.addCleanup(self.socket_patch.stop)
        await self.ready()

    def publish(self, *, mark=True):
        bids = [('255.0', '1'), ('252', '10')]
        asks = [('255.2', '1'), ('258', '10')]
        for row in self.remote.values():
            remaining = D(row['size']) - D(row['filledSize'])
            (bids if row['side'] == 'buy' else asks).append((row['price'], remaining))
        self.bot.book = book(bids=bids, asks=asks, at=int(self.wall() * 1000))
        self.bot.book_seen = self.clock.monotonic()
        if mark:
            self.bot.engine.mark = (D('255.1'), self.clock.monotonic())
            self.bot.engine.in_price_discovery = False
            self.bot.engine.price_discovery_seen = self.clock.monotonic()

    async def fill(self, order, quantity=None):
        self.clock.advance(.1)
        quantity = order.remaining if quantity is None else D(quantity)
        row = self.remote[order.client_order_id]
        row['filledSize'] = str(D(row['filledSize']) + quantity)
        if D(row['filledSize']) == D(row['size']):
            self.remote.pop(order.client_order_id)
        self.position += quantity if order.side == 'buy' else -quantity
        fill = dict(id='fill-' + str(len(self.rest_fills)), market=MARKET,
                    orderId=row['orderId'], clientOrderId=order.client_order_id,
                    price=str(order.price), size=str(quantity), side=order.side,
                    fee='0', pnl='0', time=int(self.wall() * 1000), isMaker=True)
        self.rest_fills.append(fill)
        await self.bot.on_private({'channel': 'fillsPerps', 'data': [fill]})
        self.assertEqual(self.bot.state.positions[MARKET], self.position)
        self.publish()

    async def quote_until(self, sides, *, reduce_only=None):
        for _ in range(8):
            self.publish()
            await self.drive(self.bot.tick())
            active = self.bot.state.active_orders(MARKET)
            if ({order.side for order in active} == sides and len(active) == len(sides)
                    and (reduce_only is None or all(order.reduce_only == reduce_only
                                                    for order in active))):
                if reduce_only is not None:
                    self.assertEqual(len(active), 1)
                    order = active[0]
                    self.assertTrue(order.post_only)
                    self.assertEqual(order.time_in_force, 'GTC')
                    self.assertEqual(order.side, 'sell' if self.position > 0 else 'buy')
                    self.assertLessEqual(order.remaining, abs(self.position))
                return {order.side: order for order in active}
            self.clock.advance(11)
        self.fail('报价未达到预期：' + str(self.bot.last_decision))

    def assert_not_halted(self):
        self.assertTrue(self.bot.gate.is_open)
        self.assertFalse(self.bot.gate.trading_halted)
        self.assertIsNone(self.bot.halt.exit_code)
        self.assertFalse(self.bot._stop_failed)
        self.assertEqual(self.bot.guard.state, 'RUNNING')

    async def replay_cap(self, side, *, reduce_only=False):
        orders = await self.quote_until({'buy', 'sell'})
        self.assertEqual(orders[side].quantity, D('1.56'))
        await self.fill(orders[side])
        orders = await self.quote_until({'buy', 'sell'})
        self.assertEqual(orders[side].quantity, D('.78'))
        await self.fill(orders[side])
        self.assertEqual(abs(self.position), D('2.34'))
        self.assertEqual(abs(self.position) * D('255.1'), D('596.934'))
        reducing_side = 'sell' if side == 'buy' else 'buy'
        orders = await self.quote_until({reducing_side}, reduce_only=reduce_only)
        self.assert_not_halted()
        self.assertFalse(self.bot.qualified)
        self.assertFalse(self.bot.observed_qualified)
        self.assertEqual(self.bot.observed_reason, PAUSE_REASON)
        order = orders[reducing_side]
        self.assertTrue(order.post_only)
        self.assertEqual(order.time_in_force, 'GTC')
        self.assertEqual(order.quantity, D('1.56'))
        expected_price = D('255.31') if reducing_side == 'sell' else D('254.89')
        self.assertEqual(order.price, expected_price)
        self.assertEqual(order.quantity % self.bot.rules.increment, 0)
        self.assertLessEqual(order.quantity, abs(self.position))
        self.assertFalse(any(json.loads(request.body)['timeInForce'] == 'IOC'
                             for request in self.requests if request.method == 'POST'))
        for _ in range(3):
            self.clock.advance(1)
            self.publish()
            await self.drive(self.bot.tick())
            self.assertEqual(self.bot.state.active_orders(MARKET), (order,))
            self.assertFalse(self.bot.qualified)
            self.assertEqual(self.bot.observed_reason, PAUSE_REASON)
        metrics = self.bot.observation_metrics()
        pause = metrics['守卫及停报原因'][PAUSE_REASON]
        self.assertGreaterEqual(pause['时长秒'], 3)
        self.assertGreaterEqual(pause['次数'], 1)
        return order

    async def test_two_buy_fills_enter_reduce_mode_and_sell_fill_resumes_two_sides(self):
        order = await self.replay_cap('buy')
        await self.fill(order)
        await self.quote_until({'buy', 'sell'})
        self.assertEqual(self.position, D('.78'))
        self.assertTrue(self.bot.qualified)
        self.assertTrue(self.bot.observed_qualified)
        self.assertIsNone(self.bot.observed_reason)
        self.assert_not_halted()

    async def test_two_sell_fills_enter_reduce_mode_and_buy_fill_resumes_two_sides(self):
        order = await self.replay_cap('sell')
        await self.fill(order)
        await self.quote_until({'buy', 'sell'})
        self.assertEqual(self.position, D('-.78'))
        self.assertTrue(self.bot.qualified)
        self.assertTrue(self.bot.observed_qualified)
        self.assert_not_halted()

    async def test_verified_reduce_only_gtc_reduces_long_inventory_and_resumes_two_sides(self):
        self.bot.settings = replace(self.bot.settings, reduce_only_gtc_verified=True)
        self.bot.engine.settings = self.bot.settings
        order = await self.replay_cap('buy', reduce_only=True)
        await self.fill(order)
        await self.quote_until({'buy', 'sell'})
        self.assertEqual(self.position, D('.78'))
        self.assertTrue(self.bot.qualified)
        self.assertTrue(self.bot.observed_qualified)
        self.assertIsNone(self.bot.observed_reason)
        self.assert_not_halted()

    async def test_verified_reduce_only_gtc_reduces_short_inventory_and_resumes_two_sides(self):
        self.bot.settings = replace(self.bot.settings, reduce_only_gtc_verified=True)
        self.bot.engine.settings = self.bot.settings
        order = await self.replay_cap('sell', reduce_only=True)
        await self.fill(order)
        await self.quote_until({'buy', 'sell'})
        self.assertEqual(self.position, D('-.78'))
        self.assertTrue(self.bot.qualified)
        self.assertTrue(self.bot.observed_qualified)
        self.assert_not_halted()

    async def test_cap_below_minimum_notional_uses_only_reducing_quote(self):
        self.bot.rules = replace(self.bot.rules, min_notional=D(5))
        self.position = self.bot.state.positions[MARKET] = D('2.33')
        orders = await self.quote_until({'sell'}, reduce_only=False)
        self.assertEqual(orders['sell'].quantity, D('1.56'))
        self.assertFalse(self.bot.qualified)
        self.assertEqual(self.bot.observed_reason, PAUSE_REASON)
        self.assert_not_halted()

    async def test_partial_reduction_cancels_remaining_single_order_and_resumes_two_sides(self):
        self.position = self.bot.state.positions[MARKET] = D('2.34')
        orders = await self.quote_until({'sell'}, reduce_only=False)
        old = orders['sell']
        await self.fill(old, '.02')
        self.assertEqual(old.remaining, D('1.54'))
        await self.quote_until({'buy', 'sell'})
        self.assertNotIn(old.client_order_id, self.remote)
        self.assertEqual(self.position, D('2.32'))
        self.assertTrue(self.bot.qualified)
        self.assertFalse(self.bot.inventory_reducing)
        self.assert_not_halted()

    async def test_plain_reducing_order_stays_within_remaining_position_after_partial_fill(self):
        self.assertFalse(self.bot.settings.reduce_only_gtc_verified)
        self.bot.settings = replace(self.bot.settings, hard_cap_usd=D(400))
        self.bot.engine.settings = self.bot.settings
        self.bot.risk = RiskManager(self.bot.settings.risk_limits)
        self.position = self.bot.state.positions[MARKET] = D('1.56')
        orders = await self.quote_until({'sell'}, reduce_only=False)
        old = orders['sell']
        self.assertEqual(old.quantity, self.position)
        await self.fill(old, '.01')
        self.assertEqual(self.position, D('1.55'))
        self.assertEqual(old.remaining, self.position)
        for _ in range(3):
            self.clock.advance(1)
            orders = await self.quote_until({'sell'}, reduce_only=False)
            self.assertEqual(orders['sell'], old)
            self.assertTrue(self.bot.inventory_reducing)
            self.assertFalse(self.bot.qualified)
            self.assertEqual(self.bot.observed_reason, PAUSE_REASON)
            self.assertLessEqual(old.remaining, abs(self.position))
            self.assert_not_halted()

        # Move the target price enough to exercise cancellation and replacement after the fill.
        self.bot.settings = replace(self.bot.settings, offset_bps=D(16))
        self.bot.engine.settings = self.bot.settings
        self.clock.advance(11)
        orders = await self.quote_until({'sell'}, reduce_only=False)
        replacement = orders['sell']
        self.assertNotEqual(replacement.client_order_id, old.client_order_id)
        self.assertNotIn(old.client_order_id, self.remote)
        self.assertEqual(replacement.quantity, self.position)
        self.assertLessEqual(replacement.remaining, abs(self.position))
        self.assertTrue(self.bot.inventory_reducing)
        self.assertFalse(self.bot.qualified)
        self.assertEqual(self.bot.observed_reason, PAUSE_REASON)
        self.assert_not_halted()

    async def test_inventory_cap_without_exchange_base_limit_uses_reduce_mode(self):
        self.bot.rules = replace(self.bot.rules, max_position_base_size=None)
        self.position = self.bot.state.positions[MARKET] = D('2.34')
        await self.quote_until({'sell'}, reduce_only=False)
        self.assertEqual(self.bot.observed_reason, PAUSE_REASON)
        self.assert_not_halted()

    async def test_account_exposure_cap_reduces_then_resumes_within_available_capacity(self):
        self.bot.settings = replace(self.bot.settings, max_account_exposure_usd=D(590))
        self.bot.engine.settings = self.bot.settings
        self.bot.risk = RiskManager(self.bot.settings.risk_limits)
        self.position = self.bot.state.positions[MARKET] = D('2.31')
        orders = await self.quote_until({'sell'}, reduce_only=False)
        self.assertFalse(self.bot.qualified)
        self.assertEqual(self.bot.observed_reason, PAUSE_REASON)
        self.assert_not_halted()
        await self.fill(orders['sell'], '.02')
        orders = await self.quote_until({'buy', 'sell'})
        self.assertLessEqual((self.position + orders['buy'].remaining) * D('255.1'), D(590))
        self.assertTrue(self.bot.qualified)
        self.assert_not_halted()

    async def test_real_exchange_base_limit_still_stops_both_sides(self):
        self.bot.rules = replace(self.bot.rules, max_position_base_size=D('1.5'))
        self.position = self.bot.state.positions[MARKET] = D('1.5')
        await self.drive(self.bot.tick())
        self.assertFalse(self.bot.last_decision.eligible)
        self.assertEqual(self.bot.last_decision.reason, EXCHANGE_REASON)
        self.assertFalse(self.remote)
        self.assertFalse(self.bot.state.active_orders())
        self.assertFalse(self.bot.qualified)

    async def test_flat_position_with_zero_cap_quantity_still_stops(self):
        self.bot.settings = replace(self.settings, hard_cap_usd=D(1), soft_cap_usd=D('.5'))
        self.bot.engine.settings = self.bot.settings
        await self.drive(self.bot.tick())
        self.assertFalse(self.bot.last_decision.eligible)
        self.assertNotEqual(self.bot.last_decision.reason, EXCHANGE_REASON)
        self.assertFalse(self.remote)
        self.assertFalse(self.bot.state.active_orders())

    async def test_exchange_limit_takes_precedence_when_local_cap_is_also_exhausted(self):
        self.bot.rules = replace(self.bot.rules, max_position_base_size=D('2.34'))
        self.position = self.bot.state.positions[MARKET] = D('2.34')
        await self.drive(self.bot.tick())
        self.assertEqual(self.bot.last_decision.reason, EXCHANGE_REASON)
        self.assertFalse(self.bot.last_decision.eligible)
        self.assertFalse(self.remote)

    async def test_competition_guard_still_stops_both_sides_at_inventory_cap(self):
        self.position = self.bot.state.positions[MARKET] = D('2.34')
        self.bot.engine.settings = replace(self.bot.settings, gate_comp_max_usd=D(1))
        await self.drive(self.bot.tick())
        self.assertFalse(self.bot.last_decision.eligible)
        self.assertEqual(self.bot.last_decision.reason, '竞争量超过判据')
        self.assertFalse(self.remote)

    async def test_reduce_only_gtc_rejection_reuses_passive_reduce_unavailable_path(self):
        self.bot.settings = replace(self.bot.settings, reduce_only_gtc_verified=True)
        self.bot.engine.settings = self.bot.settings
        self.position = self.bot.state.positions[MARKET] = D('2.34')
        original = self.bot.api.transport

        async def reject_reduce_only(request):
            if request.method == 'POST' and json.loads(request.body).get('reduceOnly'):
                self.requests.append(request)
                return HttpResponse(400, {'success': False,
                                         'error_code': 'reduce_only_invalid_tif'})
            return await original(request)

        self.bot.api.transport = reject_reduce_only
        await self.drive(self.bot.tick())
        self.assertTrue(self.bot.passive_reduce_unavailable)
        rejected = [order for order in self.bot.state.orders.values()
                    if order.last_error_code == 'reduce_only_invalid_tif']
        self.assertEqual(len(rejected), 1)
        self.assertTrue(rejected[0].reduce_only)
        self.assertTrue(rejected[0].post_only)
        self.assertEqual(rejected[0].time_in_force, 'GTC')
        self.assertEqual(rejected[0].last_error_category, 'reduce_only_fallback')
        self.assertFalse(self.bot.settings.reduce_only_gtc_verified)
        self.assertFalse(self.bot.engine.settings.reduce_only_gtc_verified)
        self.publish()
        await self.drive(self.bot.tick())
        self.assertFalse(self.remote)
        self.assertFalse(self.bot.state.active_orders())
        self.assertFalse(self.bot.qualified)
        self.assert_not_halted()


if __name__ == '__main__':
    unittest.main()
