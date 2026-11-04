"""M6：旧单保留与同价同剩量新意图复用完整发送前复核。"""

import random
import unittest
from copy import copy
from dataclasses import replace
from decimal import Decimal as D

import test_m5_requote as m5
from pendle.maker import Maker, MarketRules
from pendle.maker_config import MakerSettings
from pendle.maker_orders import Order, OrderStatus, OrdersBlocked
from pendle.maker_risk import RiskRejected
from test_maker import MARKET, book
from test_maker_e2e import Alerts, Clock, Exchange, Files


class M6ImmediateCancelTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = m5.M5RequoteTests.asyncSetUp
    asyncTearDown = m5.M5RequoteTests.asyncTearDown
    wall = m5.M5RequoteTests.wall
    drive = m5.M5RequoteTests.drive
    ready = m5.M5RequoteTests.ready
    publish = m5.M5RequoteTests.publish
    live_pair = m5.M5RequoteTests.live_pair
    deletes = m5.M5RequoteTests.deletes

    async def test_partial_fill_crossing_soft_cap_keeps_constraint_compliant_buy(self):
        self.bot.state.positions[MARKET] = self.position = D('1.9')
        await self.live_pair()
        buy = next(o for o in self.bot.state.active_orders(MARKET) if o.side == 'buy')
        self.assertEqual((buy.price, buy.quantity), (D('99.85'), D('4.006')))
        at = int(self.wall()*1000)
        self.remote[buy.client_order_id].update(status='PARTIAL', filledSize='2.3', time_ms=at)
        self.bot.state.apply_order_event(self.remote[buy.client_order_id])
        self.bot.state.apply_position_snapshot(MARKET, '4.2', at+1, source='ws')
        self.position = D('4.2')
        self.assertEqual(buy.remaining, D('1.706'))
        self.bot.last_requote = self.clock.monotonic()
        self.bot.backoff_until = self.clock.monotonic()+60
        self.bot.gate_wait_until = self.clock.monotonic()+60
        self.bot.settings = replace(self.bot.settings, requote_bps=D(100))
        self.bot.engine.settings = self.bot.settings
        self.clock.advance(.01)
        self.publish()
        await self.drive(self.bot.tick())
        self.assertFalse(self.deletes(), '旧 15bp 单的剩量与最终持仓均合规，应受普通重报节流保护')
        self.assertEqual(len(self.bot.state.active_orders(MARKET)), 2)
        self.assertEqual(len(self.remote), 2)

    async def test_partial_fill_crossing_soft_cap_cancels_excess_remaining_notional_immediately(self):
        self.bot.state.positions[MARKET] = self.position = D('1.9')
        await self.live_pair()
        buy = next(o for o in self.bot.state.active_orders(MARKET) if o.side == 'buy')
        self.assertEqual((buy.price, buy.quantity), (D('99.85'), D('4.006')))
        at = int(self.wall()*1000)
        self.remote[buy.client_order_id].update(status='PARTIAL', filledSize='2', time_ms=at)
        self.bot.state.apply_order_event(self.remote[buy.client_order_id])
        self.bot.state.apply_position_snapshot(MARKET, '3.9', at+1, source='ws')
        self.position = D('3.9')
        self.assertEqual(buy.remaining, D('2.006'))
        self.assertGreater(buy.remaining*buy.price, self.bot.settings.quote_notional/D(2))
        self.bot.last_requote = self.clock.monotonic()
        self.bot.backoff_until = self.clock.monotonic()+60
        self.bot.gate_wait_until = self.clock.monotonic()+60
        self.bot.settings = replace(self.bot.settings, requote_bps=D(100))
        self.bot.engine.settings = self.bot.settings
        self.clock.advance(.01)
        self.publish()
        await self.drive(self.bot.tick())
        self.assertTrue(self.deletes(), '软上限加仓侧的实际剩余名义金额超限，必须立即撤销')
        self.assertFalse(self.bot.state.active_orders(MARKET))
        self.assertFalse(self.remote)

    async def test_mark_rise_over_six_hundred_endpoint_cancels_immediately(self):
        self.bot.state.positions[MARKET] = self.position = D(2)
        await self.live_pair()
        buy = next(o for o in self.bot.state.active_orders(MARKET) if o.side == 'buy')
        self.assertEqual((buy.price, buy.remaining), (D('99.85'), D('3.999')))
        self.assertEqual((D(2)+buy.remaining)*D('100.018'), D('600.007982'))
        self.bot.last_requote = self.clock.monotonic()
        self.bot.backoff_until = self.clock.monotonic()+60
        self.bot.gate_wait_until = self.clock.monotonic()+60
        self.bot.settings = replace(self.bot.settings, requote_bps=D(100))
        self.bot.engine.settings = self.bot.settings
        self.clock.advance(.01)
        self.publish()
        self.bot.engine.mark = (D('100.018'), self.clock.monotonic())
        await self.drive(self.bot.tick())
        self.assertTrue(self.deletes(), '保守估值超过 600，不能按中心价 599.9 保留')
        self.assertFalse(self.bot.state.active_orders(MARKET))
        self.assertFalse(self.remote)


class M6ValidationPropertyTests(unittest.TestCase):
    @staticmethod
    def permitted(check):
        try:
            check()
        except (OrdersBlocked, RiskRejected):
            return False
        return True

    def test_missing_margin_ratio_uses_same_third_party_center_for_old_and_pending(self):
        clock, files = Clock(), Files()
        bot = Maker(replace(MakerSettings(), mode='sandbox'), api=Exchange(clock, files),
                    alerter=Alerts(), files=files, rules=MarketRules(D('.01'), D('.001')),
                    monotonic=clock, clock=clock.wall, sleep=clock.sleep)
        bot.state.positions[MARKET] = D('-3.1')
        bot.state.balances = dict(equity=D('103.335'), available=D(1000),
                                  margin_ratio=None, used_margin=D(0), under_liquidation=False)
        bot.engine.mark = (D(100), clock())
        bot.engine.in_price_discovery = False
        bot.engine.price_discovery_seen = clock()
        old = Order('old', MARKET, 'buy', D(1), D('99.92'), status=OrderStatus.OPEN,
                    exchange_ms=100000)
        bot.state.orders['old'] = old
        bot.book = book(bids=[('99.92', '1'), ('99.9', '10'), ('99', '10')])
        bot.book_seen = clock()
        retained = self.permitted(lambda: bot.validate_resting_or_pending(old, bot.state, now=clock()))
        intent = replace(old, status=OrderStatus.INTENT)
        bot.state.orders['old'] = intent
        bot.book = book()
        sent = self.permitted(lambda: bot.check_order(intent, bot.state))
        self.assertEqual((retained, sent), (True, True), '旧单自身不能抬高回退保证金的估值中心')

    def test_retention_iff_same_price_remaining_intent_passes_m4_for_500_seeded_cases(self):
        self.assertTrue(callable(getattr(Maker, 'validate_resting_or_pending', None)),
                        '发送与保留必须有唯一的完整校验入口')
        rng = random.Random(20261104)
        accepted = rejected = 0
        for case in range(500):
            with self.subTest(case=case):
                clock, files = Clock(), Files()
                settings = replace(MakerSettings(), mode='sandbox')
                rules = MarketRules(D('.001'), D('.001'), rng.choice((D(0), D(10), D(100))))
                bot = Maker(settings, api=Exchange(clock, files), alerter=Alerts(), files=files,
                            rules=rules, monotonic=clock, clock=clock.wall, sleep=clock.sleep)
                center = D(rng.randrange(95000, 105001))/1000
                position = rng.choice((D(0), D(300)/center, -D(300)/center,
                                       D(rng.randrange(-5900, 5901))/1000))
                side = rng.choice(('buy', 'sell'))
                mark = center*(1+D(rng.randrange(-20, 21))/100000)
                bot.engine.mark = (mark, clock())
                bot.engine.in_price_discovery = False
                bot.engine.price_discovery_seen = clock()
                bot.state.positions[MARKET] = position
                bot.state.balances = dict(equity=D(10000), available=D(9000), used_margin=D(0),
                                          margin_ratio=None if case % 2 else D('.01'),
                                          under_liquidation=False)
                spread = center*D('.0009')
                bids = [(center-spread, D(10)), (center-1, D(10))]
                asks = [(center+spread, D(10)), (center+1, D(10))]
                third_party = book(bids=bids, asks=asks)
                bot.book, bot.book_seen = third_party, clock()
                if case % 3 == 0:
                    # 保证覆盖真实可发送报价，而非所有随机样本都被拒绝。
                    bot.engine.mark = (center, clock())
                    engine = copy(bot.engine)
                    decision = engine.evaluate(third_party, bot.state, rules, now=clock(), guard_only=True)
                    self.assertTrue(decision.eligible, decision.reason)
                    quote = next(q for q in decision.quotes if q.side == side)
                    price, remaining = quote.price, min(quote.quantity, D('.5'))
                    post_only, reduce_only = True, quote.reduce_only
                else:
                    distance = D(rng.randrange(6, 25))/10000
                    price = (center*(1+(-distance if side == 'buy' else distance))).quantize(rules.tick)
                    remaining = D(rng.randrange(1, 4501))/1000
                    post_only, reduce_only = rng.random() > .05, rng.random() < .1
                filled = D(rng.randrange(1, 4001))/1000 if rng.random() < .5 else D(0)
                old = Order('old', MARKET, side, remaining+filled, price,
                            reduce_only=reduce_only, post_only=post_only, filled=filled,
                            status=OrderStatus.PARTIAL if filled else OrderStatus.OPEN, exchange_ms=100000)
                bot.state.orders[old.client_order_id] = old
                # 其他同侧或反侧活单须继续占用容量，不能随候选单一起排除。
                other_side = rng.choice(('buy', 'sell'))
                other = Order('other', MARKET, other_side, D(rng.randrange(1, 1501))/1000,
                              center+(-D('.18') if other_side == 'buy' else D('.18')),
                              status=OrderStatus.OPEN, exchange_ms=100000)
                bot.state.orders[other.client_order_id] = other
                for order in (old, other):
                    (bids if order.side == 'buy' else asks).append((order.price, order.remaining))
                bot.book = book(bids=bids, asks=asks)
                bot.book_seen = clock()
                public_book = bot.book
                retained = self.permitted(lambda: bot.validate_resting_or_pending(old, bot.state, now=clock()))
                # 替换为尚未公开的新意图：移除旧单公开剩量，保持相同第三方盘口和其他活单。
                snapshot = copy(bot.state)
                intent = replace(old, quantity=remaining, filled=D(0), status=OrderStatus.INTENT)
                snapshot.orders = {**bot.state.orders, old.client_order_id: intent}
                bot.state = snapshot
                sides = [dict(public_book.bids), dict(public_book.asks)]
                levels = sides[0 if side == 'buy' else 1]
                levels[price] -= remaining
                bot.book = replace(public_book, bids=tuple((p, q) for p, q in sides[0].items() if q),
                                   asks=tuple((p, q) for p, q in sides[1].items() if q))
                sent = self.permitted(lambda: bot.check_order(intent, snapshot))
                self.assertEqual(retained, sent, '同价同剩量的保留与发送前复核结论必须相同')
                accepted += retained
                rejected += not retained
        self.assertGreater(accepted, 25, '必须实际覆盖通过样本')
        self.assertGreater(rejected, 25, '必须实际覆盖拒绝样本')
        self.assertEqual(accepted+rejected, 500)
