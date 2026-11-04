"""Revalidate fixed intents against the latest order book at the end of the send queue without changing the strategy slow anchor."""

import unittest
from dataclasses import replace
from datetime import datetime
from decimal import Decimal as D
from zoneinfo import ZoneInfo

from pendle.maker import Maker, MarketRules
from pendle.maker_config import MakerSettings
from pendle.maker_orders import OrdersBlocked
from pendle.maker_risk import RiskRejected
from test_maker import MARKET, book
from test_maker_e2e import Alerts, Clock, Exchange, Files


class DispatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock, self.files = Clock(), Files()
        self.exchange = Exchange(self.clock, self.files)
        self.bot = Maker(replace(MakerSettings(), mode='sandbox', quiet_s=.01),
            api=self.exchange, alerter=Alerts(), files=self.files,
            rules=MarketRules(D('.01'), D('.001')), monotonic=self.clock,
            clock=self.clock.wall, sleep=self.clock.sleep)
        await self.bot.startup()
        self.bot.state.deadman_ack()
        await self.bot.reconcile_once()
        self.publish()
        self.bot.engine.evaluate(self.bot.book, self.bot.state, self.bot.rules, now=self.clock())
        self.exchange.on_quote = self.publish

    async def asyncTearDown(self):
        await self.bot.shutdown()

    def publish(self, center='100', size='10'):
        center = D(center)
        bids = [(str(center-D('.1')), size), (str(center-1), '10')]
        asks = [(str(center+D('.1')), size), (str(center+1), '10')]
        for row in self.exchange.rows.values():
            if row['status'] in ('OPEN', 'PARTIAL'):
                remaining = D(row['size']) - D(row['filledSize'])
                (bids if row['side'] == 'buy' else asks).append((row['price'], str(remaining)))
        self.bot.book = book(bids=bids, asks=asks, at=int(self.clock.wall()*1000))
        self.bot.book_seen = self.clock()
        self.bot.engine.mark = (center, self.clock())
        self.bot.engine.in_price_discovery = False
        self.bot.engine.price_discovery_seen = self.clock()

    def intent(self, side='buy', *, quantity='1', price=None, ioc=False):
        return self.bot.manager.create_intent(MARKET, side, D(quantity),
            D(price or ('99.88' if side == 'buy' else '100.12')),
            reduce_only=ioc, time_in_force='IOC' if ioc else 'GTC', post_only=not ioc)

    def rejected(self, order):
        with self.assertRaises((OrdersBlocked, RiskRejected)):
            self.bot.check_order(order, self.bot.state)

    async def test_new_third_party_jump_rejects_queued_fixed_quote(self):
        order = self.intent()
        self.publish('105')
        self.rejected(order)

    async def test_quote_too_close_to_new_center_is_rejected_before_wire(self):
        order = self.intent('sell')
        self.publish('100.1')
        self.rejected(order)

    async def test_new_competition_blocks_queued_order(self):
        order = self.intent()
        self.publish(size='1000')
        self.rejected(order)

    async def test_fixed_intent_checks_latest_mark_distance(self):
        order = self.intent('sell')
        self.bot.engine.mark = (D('100.1'), self.clock())
        self.rejected(order)

    async def test_before_send_validation_has_no_anchor_ema_or_history_side_effects(self):
        order = self.intent()
        original = (self.bot.engine.anchor, self.bot.engine.anchor_at,
                    self.bot.engine.ema, list(self.bot.engine.history))
        self.publish('100.01')
        self.bot.check_order(order, self.bot.state)
        self.assertEqual((self.bot.engine.anchor, self.bot.engine.anchor_at,
            self.bot.engine.ema, list(self.bot.engine.history)), original)

    async def test_other_open_order_missing_from_snapshot_blocks_second_side(self):
        self.exchange.on_quote = None
        buy = self.intent()
        await self.bot.manager.submit(buy)
        sell = self.intent('sell')
        self.rejected(sell)

    async def test_realistic_own_book_allows_both_sides_after_first_is_accepted(self):
        await self.bot.tick()
        self.assertEqual(len([row for row in self.exchange.calls if row[0] == 'place']), 2)

    async def test_ioc_uses_fresh_center_instead_of_stale_anchor(self):
        self.bot.state.positions[MARKET] = self.exchange.position = D(5)
        order = self.intent('sell', price='99.4', ioc=True)
        self.publish('105')
        self.rejected(order)

    async def test_more_favorable_ioc_limit_is_allowed(self):
        self.bot.state.positions[MARKET] = self.exchange.position = D(5)
        order = self.intent('sell', price='101', ioc=True)
        self.bot.check_order(order, self.bot.state)

    async def test_price_and_quantity_must_be_on_exchange_grids(self):
        for values in ({'price': '99.881'}, {'quantity': '1.0001'}):
            with self.subTest(values=values):
                order = self.intent(**values)
                try:
                    self.rejected(order)
                finally:
                    self.bot.state.orders.pop(order.client_order_id)

    async def test_queued_gtc_cannot_cross_window_end(self):
        order = self.intent()
        self.clock.epoch = datetime(2026, 11, 9, 5, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        self.publish()
        self.rejected(order)

    async def test_last_hour_rejects_queued_opening_gtc(self):
        order = self.intent()
        self.clock.epoch = datetime(2026, 11, 9, 4, 15, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        self.publish()
        self.rejected(order)

    async def test_used_margin_fallback_is_rechecked_at_actual_send(self):
        self.bot.state.balances.update(equity='1000',available='900',
                                       margin_ratio=None,used_margin='290')
        order=self.intent(quantity='4')
        self.rejected(order)

    async def test_local_margin_fallback_does_not_double_count_candidate(self):
        self.bot.state.balances.update(equity='200',available='200',
                                       margin_ratio=None,used_margin='0')
        order=self.intent(quantity='4')
        self.bot.check_order(order,self.bot.state)
