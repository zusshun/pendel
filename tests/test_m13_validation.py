"""M13 的 mark 中心复用 M6 统一复核：旧单与同价同剩量意图结论一致。"""

import unittest
from dataclasses import replace
from decimal import Decimal as D

from pendle.maker import Maker, MarketRules
from pendle.maker_config import MakerSettings
from pendle.maker_orders import Order, OrderStatus, OrdersBlocked
from pendle.maker_risk import RiskRejected
from test_m13_center import MARKET, depth
from test_maker_e2e import Alerts, Clock, Exchange, Files


class M13ValidationTests(unittest.TestCase):
    def make_bot(self):
        clock, files = Clock(), Files()
        bot = Maker(replace(MakerSettings(), mode='sandbox'), api=Exchange(clock, files),
            alerter=Alerts(), files=files, rules=MarketRules('.01', '.001'),
            monotonic=clock, clock=clock.wall, sleep=clock.sleep)
        bot.engine.mark = (D(100), clock())
        bot.engine.in_price_discovery = False
        bot.engine.price_discovery_seen = clock()
        bot.book_seen = clock()
        bot.state.balances = dict(equity=D(10000), available=D(9000), used_margin=D(0),
                                 under_liquidation=False, margin_ratio=D('.01'))
        return bot, clock

    def check_pair(self, bids, asks, side, price):
        bot, clock = self.make_bot()
        old = Order('same', MARKET, side, D(1), D(price), status=OrderStatus.OPEN,
                    exchange_ms=100000)
        bot.state.orders['same'] = old
        own = ((price, '1'),)
        bot.book = depth(bids+own if side == 'buy' else bids,
                         asks+own if side == 'sell' else asks)
        outcomes = []
        for order, book in ((old, bot.book),
                            (replace(old, status=OrderStatus.INTENT), depth(bids, asks))):
            bot.state.orders['same'], bot.book = order, book
            try:
                bot.validate_resting_or_pending(order, bot.state, now=clock())
            except (OrdersBlocked, RiskRejected):
                outcomes.append(False)
            else:
                outcomes.append(True)
        self.assertIsNone(bot.engine.ema, '发送与保留复核不能推动主引擎慢锚')
        return tuple(outcomes)

    def test_mark_center_retention_and_pending_pass_for_wide_and_sparse_books(self):
        for bids, asks in (((('99', '1'),), (('101', '1'),)),
                           ((('99', '1'),), ()), ((), (('101', '1'),)), ((), ())):
            for side, price in (('buy', '99.88'), ('sell', '100.12')):
                with self.subTest(bids=bids, asks=asks, side=side):
                    self.assertEqual(self.check_pair(bids, asks, side, price), (True, True))

    def test_mark_center_retention_and_pending_reject_third_party_post_only_match(self):
        for bids, asks, side, price in (
                ((('99', '1'),), (('99.88', '1'), ('101', '1')), 'buy', '99.88'),
                ((('100.12', '1'), ('99', '1')), (('101', '1'),), 'sell', '100.12'),
                ((), (('99.88', '1'), ('101', '1')), 'buy', '99.88'),
                ((('100.12', '1'), ('99', '1')), (), 'sell', '100.12')):
            with self.subTest(bids=bids, asks=asks, side=side):
                self.assertEqual(self.check_pair(bids, asks, side, price), (False, False))


if __name__ == '__main__':
    unittest.main()
