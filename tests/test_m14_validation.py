"""M14 固定订单只复核约束，保留与同价同剩量的新意图结论等价。"""

import random
import unittest
from dataclasses import replace
from decimal import Decimal as D

from pendle.maker import BPS, Maker, MarketRules
from pendle.maker_config import MakerSettings
from pendle.maker_orders import Order, OrderStatus, OrdersBlocked
from pendle.maker_risk import RiskRejected
from test_m13_center import MARKET, depth
from test_maker_e2e import Alerts, Clock, Exchange, Files


class M14ConstraintValidationTests(unittest.TestCase):
    def make_bot(self):
        clock, files = Clock(), Files()
        bot = Maker(replace(MakerSettings(), mode='sandbox'), api=Exchange(clock, files),
                    alerter=Alerts(), files=files, rules=MarketRules('.01', '.001'),
                    monotonic=clock, clock=clock.wall, sleep=clock.sleep)
        # 源时间入站测试独立覆盖；此处直接注入已确认的新鲜数据。
        bot.engine.mark = (D(100), clock())
        bot.engine.in_price_discovery = False
        bot.engine.price_discovery_seen = clock()
        bot.book_seen = clock()
        bot.state.balances = dict(equity=D(10000), available=D(9000), used_margin=D(0),
                                  under_liquidation=False, margin_ratio=D('.01'))
        return bot, clock

    def check_pair(self, side, price, *, bids=(('99', '1'),), asks=(('101', '1'),),
                   remaining=D(1), filled=D(0), post_only=True):
        bot, clock = self.make_bot()
        price = D(price)
        old = Order('same', MARKET, side, remaining+filled, price, post_only=post_only,
                    filled=filled, status=OrderStatus.PARTIAL if filled else OrderStatus.OPEN,
                    exchange_ms=int(clock()*1000))
        sides = [dict((D(p), D(q)) for p, q in bids),
                 dict((D(p), D(q)) for p, q in asks)]
        levels = sides[0 if side == 'buy' else 1]
        levels[price] = levels.get(price, D(0))+remaining
        public = depth(tuple(sides[0].items()), tuple(sides[1].items()), clock())
        pending = replace(old, quantity=remaining, filled=D(0), status=OrderStatus.INTENT)
        outcomes = []
        for order, book in ((old, public), (pending, depth(bids, asks, clock()))):
            bot.state.orders['same'], bot.book = order, book
            try:
                bot.validate_resting_or_pending(order, bot.state, now=clock())
            except (OrdersBlocked, RiskRejected):
                outcomes.append(False)
            else:
                outcomes.append(True)
        self.assertIsNone(bot.engine.ema, '复核副本不能推动主引擎锚')
        return tuple(outcomes)

    def test_one_bps_center_move_keeps_constraint_compliant_old_pair(self):
        # mark 为 100，第三方中间价为 100.01；旧单仍满足 center/mark 距离和 post-only。
        bids = (('99.91', '1'), ('99', '1'))
        asks = (('100.11', '1'), ('101', '1'))
        bot, clock = self.make_bot()
        decision = bot.engine.evaluate(depth(bids, asks, clock()), bot.state, bot.rules,
                                       now=clock(), guard_only=True)
        self.assertTrue(decision.eligible, decision.reason)
        self.assertEqual((decision.center, decision.center_source),
                         (D('100.01'), 'third_party_mid'))
        for side, price in (('buy', '99.88'), ('sell', '100.12')):
            with self.subTest(side=side):
                self.assertEqual(self.check_pair(side, price, bids=bids, asks=asks),
                                 (True, True))

    def test_orders_inside_theoretical_quote_pass_when_all_constraints_pass(self):
        for side, price in (('buy', '99.91'), ('sell', '100.09')):
            with self.subTest(side=side):
                self.assertEqual(self.check_pair(side, price), (True, True))

    def test_fixed_quotes_must_remain_on_their_own_side_of_center_and_mark(self):
        for side, price in (('buy', '100.12'), ('sell', '99.88')):
            with self.subTest(side=side):
                self.assertEqual(self.check_pair(side, price), (False, False),
                                 '取消理论价内沿后，仍须拒绝方向颠倒的固定报价')

    def test_valid_fixed_orders_ignore_post_only_failure_of_hypothetical_quotes(self):
        bids = (('99.5', '1'), ('99', '1'))
        asks = (('99.87', '1'), ('101', '1'))
        bot, clock = self.make_bot()
        hypothetical = bot.engine.evaluate(depth(bids, asks, clock()), bot.state, bot.rules,
                                           now=clock(), guard_only=True)
        self.assertFalse(hypothetical.eligible, '理论买价 99.88 会跨入第三方卖价 99.87')
        self.assertEqual(hypothetical.reason, '取整后任一侧不合规')
        for side, price in (('buy', '99.85'), ('sell', '100.15')):
            with self.subTest(side=side):
                self.assertEqual(self.check_pair(side, price, bids=bids, asks=asks),
                                 (True, True), '固定单只应检查实际价格的 post-only 约束')

    def test_retention_iff_same_price_remaining_intent_covers_inside_and_outside_theory(self):
        rng = random.Random(20261114)
        accepted = rejected = inside = outside = 0
        for case in range(240):
            side = rng.choice(('buy', 'sell'))
            distance = rng.choice((D(7), D(9), D(10), D(15), D(18), D(21), D(23)))
            price = D(100)*(1+(-distance if side == 'buy' else distance)/BPS)
            remaining = rng.choice((D('.5'), D(1), D(5)))
            filled = rng.choice((D(0), D('.7'), D('2.3')))
            post_only = case % 13 != 0
            # 两种冷启动来源均覆盖，避免把等价性与来源切换计时耦合。
            bids, asks = ((('99', '1'),), (('101', '1'),)) if case % 2 else (
                (('99.95', '1'), ('99', '1')), (('100.05', '1'), ('101', '1')))
            expected = D(8) <= distance <= D(22) and remaining <= 1 and post_only
            with self.subTest(case=case, side=side, distance=distance, remaining=remaining):
                retained, pending = self.check_pair(side, price, bids=bids, asks=asks,
                    remaining=remaining, filled=filled, post_only=post_only)
                self.assertEqual(retained, pending, '同价同剩量新意图与旧单必须等价')
                self.assertEqual(retained, expected, '结论由约束决定，不能由理论价内沿决定')
            accepted += retained
            rejected += not retained
            inside += retained and distance < 12
            outside += retained and distance > 12
        self.assertGreater(accepted, 25)
        self.assertGreater(rejected, 25)
        self.assertGreater(inside, 10, '必须真实接受内于理论价的样本')
        self.assertGreater(outside, 10, '必须真实接受外于理论价的样本')


if __name__ == '__main__':
    unittest.main()
