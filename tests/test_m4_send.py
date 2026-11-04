"""M4: fixed queued orders must pass all quote constraints at send time."""

import asyncio
import json
import unittest
from decimal import Decimal as D

from pendle.maker_orders import Order, OrderStatus
from test_maker import MARKET, book
from test_ondo_api import settle
import test_m3_wiring as m3


class M4SendRecheckTests(unittest.IsolatedAsyncioTestCase):
    # Reuse the real builder and in-memory HTTP stubs without inheriting the test class, avoiding duplicate M3 test discovery.
    asyncSetUp = m3.M3WiringTests.asyncSetUp
    asyncTearDown = m3.M3WiringTests.asyncTearDown
    wall = m3.M3WiringTests.wall
    drive = m3.M3WiringTests.drive
    ready = m3.M3WiringTests.ready
    publish = m3.M3WiringTests.publish

    def posts(self):
        return [json.loads(request.body) for request in self.requests
                if request.method == 'POST']

    def position_at(self, quantity):
        self.bot.state.positions[MARKET] = self.position = D(quantity)
        self.publish()

    def fresh_position(self, quantity):
        self.bot.state.orders.clear()
        self.remote.clear()
        self.position_at(quantity)

    async def queued(self, side, quantity, price, update, **flags):
        """Wait for a token in the real signing queue, then change the risk snapshot."""
        order = self.bot.manager.create_intent(MARKET, side, D(quantity), D(price), **flags)
        before = len(self.posts())
        self.bot.api._tokens = 0
        self.bot.api._last_refill = self.clock.monotonic()
        self.bot.api.rate = 1
        submit = asyncio.create_task(self.bot.manager.submit(order))
        await settle()
        self.assertEqual(order.status, OrderStatus.INTENT)
        self.assertEqual(len(self.posts()), before)
        update()
        await self.drive(submit)
        return order, self.posts()[before:]

    def rejected(self, order, sent):
        self.assertEqual(sent, [], '旧固定意图必须在实际 HTTP 之前被取消')
        self.assertEqual(order.status, OrderStatus.REJECTED)
        self.assertNotIn(order.client_order_id, self.remote)

    async def test_queued_audit_buy_is_rejected_after_position_crosses_soft_cap(self):
        await self.ready()
        order, sent = await self.queued('buy', '2.5', '99.88',
                                        lambda: self.position_at('3.1'))
        self.rejected(order, sent)
        # After cancelling the old intent, the next tick can regenerate the soft-side quantity and band-edge price.
        decision = self.bot.engine.evaluate(self.bot.book, self.bot.state, self.bot.rules,
                                            now=self.clock.monotonic())
        self.assertTrue(decision.eligible, decision.reason)
        buy = next(quote for quote in decision.quotes if quote.side == 'buy')
        self.assertEqual(buy.price, D('99.78'))
        self.assertLessEqual(buy.price * buy.quantity, D(200))
        replacement = self.bot.manager.create_intent(MARKET, buy.side, buy.quantity, buy.price)
        await self.drive(self.bot.manager.submit(replacement))
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(replacement.status, OrderStatus.OPEN)

    async def test_soft_adding_notional_is_rechecked_at_compliant_band_edge_both_sides(self):
        await self.ready()
        for side, price, position in (('buy', '99.78', '3.1'),
                                      ('sell', '100.22', '-3.1')):
            with self.subTest(side=side):
                self.fresh_position('0')
                order, sent = await self.queued(side, '2.5', price,
                                                lambda: self.position_at(position))
                self.rejected(order, sent)

    async def test_small_soft_adding_order_passes_without_matching_theoretical_band_edge_both_sides(self):
        await self.ready()
        for side, price, position in (('buy', '99.88', '3.1'),
                                      ('sell', '100.12', '-3.1')):
            with self.subTest(side=side):
                self.fresh_position('0')
                order, sent = await self.queued(side, '1', price,
                                                lambda: self.position_at(position))
                self.assertLessEqual(order.quantity*order.price, D(200), '实际加仓金额仍符合软上限')
                self.assertEqual(len(sent), 1, '满足实际约束的固定意图应允许发送')
                self.assertEqual(order.status, OrderStatus.OPEN)
                self.assertIn(order.client_order_id, self.remote)

    async def test_inventory_skew_does_not_reject_constraint_compliant_fixed_price_both_sides(self):
        await self.ready()
        for side, price, position in (('buy', '99.88', '1.5'),
                                      ('sell', '100.12', '-1.5')):
            with self.subTest(side=side):
                self.fresh_position('0')
                order, sent = await self.queued(side, '1', price,
                                                lambda: self.position_at(position))
                self.assertEqual(len(sent), 1, '库存变化后的理论偏移不影响实际合规固定价')
                self.assertEqual(order.status, OrderStatus.OPEN)
                self.assertIn(order.client_order_id, self.remote)

    async def test_latest_position_rechecks_hard_cap_both_sides(self):
        await self.ready()
        for side, price, position in (('buy', '99.78', '5'),
                                      ('sell', '100.22', '-5')):
            with self.subTest(side=side):
                self.fresh_position('0')
                order, sent = await self.queued(side, '1.5', price,
                                                lambda: self.position_at(position))
                self.rejected(order, sent)

    async def test_latest_live_orders_reserve_same_side_hard_capacity_both_sides(self):
        await self.ready()
        cases = (('buy', '99.84', '2.5', '1'),
                 ('sell', '100.16', '-2.5', '.994'))
        for side, price, position, quantity in cases:
            with self.subTest(side=side):
                self.fresh_position(position)

                def add_live():
                    live = Order('existing-' + side, MARKET, side, D('2.5'), D(price),
                                 status=OrderStatus.OPEN,
                                 exchange_ms=int(self.wall() * 1000))
                    self.bot.state.orders[live.client_order_id] = live
                    self.remote[live.client_order_id] = dict(clientOrderId=live.client_order_id,
                        orderId=live.client_order_id, market=MARKET, side=side,
                        size='2.5', price=price, filledSize='0', status='OPEN')
                    self.publish()

                order, sent = await self.queued(side, quantity, price, add_live)
                self.rejected(order, sent)

    async def test_center_move_rechecks_outer_band_both_sides(self):
        await self.ready()
        for side, price, center in (('buy', '99.78', D('100.1')),
                                    ('sell', '100.22', D('99.9'))):
            with self.subTest(side=side):
                self.fresh_position('0')

                def move_center():
                    self.bot.book = book(bids=[(center-D('.1'), 10), (center-1, 10)],
                                         asks=[(center+D('.1'), 10), (center+1, 10)],
                                         at=int(self.wall()*1000))
                    self.bot.book_seen = self.clock.monotonic()
                    self.bot.engine.mark = center, self.clock.monotonic()

                order, sent = await self.queued(side, '1', price, move_center)
                self.rejected(order, sent)

    async def test_center_move_rechecks_minimum_offset_both_sides(self):
        await self.ready()
        for side, price, center in (('buy', '99.88', D('99.9')),
                                    ('sell', '100.12', D('100.1'))):
            with self.subTest(side=side):
                self.fresh_position('0')

                def move_center():
                    self.bot.book = book(bids=[(center-D('.1'), 10), (center-1, 10)],
                                         asks=[(center+D('.1'), 10), (center+1, 10)],
                                         at=int(self.wall()*1000))
                    self.bot.book_seen = self.clock.monotonic()
                    self.bot.engine.mark = center, self.clock.monotonic()

                order, sent = await self.queued(side, '1', price, move_center)
                self.rejected(order, sent)

    async def test_latest_opposite_level_rechecks_post_only_both_sides(self):
        await self.ready()
        for side, price, position in (('buy', '99.88', '-1.5'),
                                      ('sell', '100.12', '1.5')):
            with self.subTest(side=side):
                self.fresh_position(position)

                def crossing_level():
                    bids = [('99.82', '10'), ('99', '10')]
                    asks = [('99.88', '10'), ('101', '10')]
                    if side == 'sell':
                        bids, asks = [('100.12', '10'), ('99', '10')], [
                            ('100.18', '10'), ('101', '10')]
                    self.bot.book = book(bids=bids, asks=asks, at=int(self.wall()*1000))
                    self.bot.book_seen = self.clock.monotonic()
                    self.bot.engine.mark = self.bot.book.mid, self.clock.monotonic()

                order, sent = await self.queued(side, '1', price, crossing_level)
                self.rejected(order, sent)

    async def test_compliant_fixed_outer_prices_are_still_sent_both_sides(self):
        await self.ready()
        for side, price, position in (('buy', '99.85', '1.5'),
                                      ('sell', '100.15', '-1.5')):
            with self.subTest(side=side):
                self.fresh_position('0')
                order, sent = await self.queued(side, '1', price,
                                                lambda: self.position_at(position))
                self.assertEqual(len(sent), 1)
                self.assertEqual((sent[0]['side'], sent[0]['price'], sent[0]['size']),
                                 (side, price, '1'))
                self.assertEqual(order.status, OrderStatus.OPEN)

    async def test_gtc_requires_post_only_flag_both_sides(self):
        await self.ready()
        for side, price, position in (('buy', '99.88', '-1'),
                                      ('sell', '100.12', '1')):
            with self.subTest(side=side):
                self.fresh_position(position)
                order, sent = await self.queued(side, '1', price, lambda: self.publish(),
                                                reduce_only=True, post_only=False)
                self.rejected(order, sent)

    async def test_soft_reducing_order_cannot_flip_after_position_changes_both_sides(self):
        await self.ready()
        for side, price, position in (('sell', '100.12', '3.1'),
                                      ('buy', '99.88', '-3.1')):
            with self.subTest(side=side):
                self.fresh_position('0')
                order, sent = await self.queued(side, '3.5', price,
                                                lambda: self.position_at(position))
                self.rejected(order, sent)

    async def test_compliant_soft_edge_orders_are_still_sent_both_sides(self):
        await self.ready()
        for side, price, position in (('buy', '99.78', '3.1'),
                                      ('sell', '100.22', '-3.1')):
            with self.subTest(side=side):
                self.fresh_position('0')
                order, sent = await self.queued(side, '1', price,
                                                lambda: self.position_at(position))
                self.assertEqual(len(sent), 1)
                self.assertEqual(order.status, OrderStatus.OPEN)

    async def test_normal_quote_quantity_is_recomputed_using_fixed_price(self):
        await self.ready()
        order, sent = await self.queued('buy', '4.005', '99.78', lambda: self.publish())
        self.assertEqual(len(sent), 1)
        self.assertEqual(order.status, OrderStatus.OPEN)
        self.assertLessEqual(D(sent[0]['price'])*D(sent[0]['size']), D(400))
