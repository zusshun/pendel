"""实盘策略使用纯盘口与内存依赖验证，不联网。"""

import unittest
from dataclasses import replace
from datetime import datetime
from decimal import Decimal as D
from zoneinfo import ZoneInfo

from pendle import maker
from pendle.maker_config import MakerSettings
from pendle.maker_orders import AccountState, Order, OrderStatus
from pendle.market import Book


MARKET = 'IBM-USD.P'


def book(bids=None, asks=None, at=100000):
    return Book.make('ondo', 'IBM', at, at,
                     bids or [('99.9', '10'), ('99', '10')],
                     asks or [('100.1', '10'), ('101', '10')])


class QuoteTests(unittest.TestCase):
    def setUp(self):
        self.settings = MakerSettings()
        self.state = AccountState([MARKET])
        self.engine = maker.QuoteEngine(self.settings)
        self.engine.mark = (D(100), 100)
        self.engine.in_price_discovery = False
        self.engine.price_discovery_seen = 100
        self.rules = maker.MarketRules(D('.01'), D('.001'))

    def quote(self, b=None, position='0', now=100):
        self.state.positions[MARKET] = D(position)
        return self.engine.evaluate(b or book(), self.state, self.rules, now=now)

    def test_inventory_skew_and_rounding_are_in_band(self):
        result = self.quote(position='1.5')
        self.assertTrue(result.eligible, result.reason)
        self.assertEqual(result.center, 100)
        buy, sell = result.quotes
        self.assertLess(buy.price, D('99.88'))
        self.assertLess(sell.price, D('100.12'))
        for quote in result.quotes:
            distance = abs(quote.price / result.center - 1) * 10000
            self.assertGreaterEqual(distance, 8)
            self.assertLessEqual(distance, 22)
            self.assertLessEqual(quote.quantity * quote.price, 400)

    def test_self_reference_uses_fresh_mark_and_symmetric_inventory_prices(self):
        for i in range(20):
            b = book([('99.88', '4')], [('100.12', '4')], at=101000+i)
            self.state.orders = {side: Order(side, MARKET, side, D(4), D(price),
                status=OrderStatus.OPEN, exchange_ms=100000)
                for side, price in [('buy', '99.88'), ('sell', '100.12')]}
            now = 101+i
            self.engine.mark = (D(100), now)
            self.engine.price_discovery_seen = now
            result = self.quote(b, position='1', now=now)
            self.assertTrue(result.eligible, result.reason)
            self.assertEqual(result.center, 100)
            self.assertEqual(result.center_source, 'mark')
            self.assertEqual(result.quotes[0].price, D('99.88'))
            self.assertEqual(result.quotes[1].price, D('100.12'))
        self.engine.price_discovery_seen = 701
        stale = self.quote(b, now=701)
        self.assertFalse(stale.eligible)
        self.assertIn('标记价格', stale.reason)

    def test_uncertain_or_older_book_is_never_eligible(self):
        for status in (OrderStatus.SENT, OrderStatus.UNKNOWN, OrderStatus.CANCEL_SENT):
            self.state.orders = {'x': Order('x', MARKET, 'buy', D(1), D('99.9'), status=status)}
            self.assertFalse(self.quote().eligible)
        self.state.orders['x'].status = OrderStatus.OPEN
        self.state.orders['x'].exchange_ms = 100001
        self.assertFalse(self.quote().eligible)

    def test_full_100_level_truncated_book_is_incomplete(self):
        b = book([(str(D('99.99') - D('.0001')*i), '1') for i in range(100)],
                 [(str(D('100.01') + D('.0001')*i), '1') for i in range(100)])
        self.assertFalse(self.quote(b).eligible)

    def test_gate_uses_third_party_only_and_excess_fails(self):
        self.engine.settings = replace(self.settings, gate_comp_max_usd=D('1'),max_spread_bps=D(250))
        self.assertFalse(self.quote().eligible)
        own_book = book([('99.9', '4'), ('99', '10')],
                        [('100.1', '4'), ('101', '10')])
        self.state.orders = {side: Order(side, MARKET, side, D(4), D(price),
            status=OrderStatus.OPEN, exchange_ms=100000)
            for side, price in [('buy','99.9'), ('sell','100.1')]}
        # 前轮曾选择中间价；达到最短停留时间后才能切到 mark。
        self.engine.mark = (D(100), 130)
        self.engine.price_discovery_seen = 130
        result = self.quote(own_book, now=130)
        self.assertTrue(result.eligible, result.reason)
        self.assertEqual(result.center, self.engine.mark[0])
        self.assertEqual(result.center_source, 'mark')
        self.assertEqual(result.competition, 0)

    def test_soft_cap_halves_adding_side_and_caps_reducing_side(self):
        result = self.quote(position='3')
        self.assertTrue(result.eligible, result.reason)
        buy, sell = result.quotes
        self.assertLessEqual(buy.quantity * buy.price, 200)
        self.assertEqual(buy.price, D('99.78'))
        self.assertLessEqual(sell.quantity, 3)

    def test_soft_cap_band_edge_rounds_inward_with_fractional_mid(self):
        fractional = book([('99.903', '10'), ('99', '10')],
                          [('100.103', '10'), ('101', '10')])
        self.engine.mark = (D('100.003'), 100)
        for position, adding_side in (('3', 'buy'), ('-3', 'sell')):
            with self.subTest(position=position):
                result = self.quote(fractional, position=position)
                self.assertTrue(result.eligible, result.reason)
                adding = next(q for q in result.quotes if q.side == adding_side)
                distance = abs(adding.price/result.center-1)*10000
                self.assertGreaterEqual(distance, 8)
                self.assertLessEqual(distance, 22)
                self.assertLessEqual(adding.quantity*adding.price, 200)

    def test_adding_capacity_includes_all_existing_unfinished_orders(self):
        self.state.orders['buy'] = Order('buy', MARKET, 'buy', D(2), D('99.78'),
                                       status=OrderStatus.OPEN, exchange_ms=100000)
        existing = book([('99.9', '10'), ('99.78', '2'), ('99', '10')])
        result = self.quote(existing, position='3')
        self.assertTrue(result.eligible, result.reason)
        buy = next(q for q in result.quotes if q.side == 'buy')
        self.assertLessEqual(buy.quantity, D('.999'))
        self.assertLess((D(3)+D(2)+buy.quantity)*result.center, D(600))

    def test_stale_mark_blocks_quotes_and_fresh_mark_allows_them(self):
        self.engine.settings = replace(self.settings, mode='sandbox')
        self.engine.mark = (D(100), 100)
        expiry = 100+self.settings.mark_quiet_max_s
        self.assertTrue(self.quote(now=expiry).eligible)
        result = self.quote(now=expiry+.001)
        self.assertFalse(result.eligible)
        self.assertIn('标记价格', result.reason)

    def test_external_reference_is_guard_only_and_stale_prices_are_ignored(self):
        self.engine.external = (D(50), 100)
        initial = self.quote(now=100)
        self.assertTrue(initial.eligible)
        self.assertEqual(initial.center, 100)
        self.engine.external = (D(51), 101)
        self.assertFalse(self.quote(now=101).eligible)
        self.engine.price_discovery_seen = 107
        recovered = self.quote(now=107)
        self.assertTrue(recovered.eligible, recovered.reason)
        self.assertEqual(recovered.center, 100)

    def test_one_invalid_side_blocks_both(self):
        result = self.engine.evaluate(book(), self.state,
            maker.MarketRules(D('1'), D('.001')), now=100)
        self.assertFalse(result.eligible)
        self.assertEqual(result.quotes, ())

    def test_anchor_and_jump_and_mark_guards(self):
        moved = book([('100.9','1'), ('100','1')], [('101.1','1'), ('102','1')], at=101000)
        isolated = replace(self.settings, max_slow_drift_bps=D(300))
        cases = ((isolated, moved, D(101), '偏离独立慢锚'),
                 (replace(isolated, max_anchor_drift_bps=D(200)), moved,
                  D(101), '中心价跳变'),
                 (self.settings, book(at=101000), D(102), '中间价与标记价格偏离超过阈值'))
        for settings, current_book, mark, reason in cases:
            with self.subTest(reason=reason):
                self.engine = maker.QuoteEngine(settings)
                self.engine.mark = (D(100), 100)
                self.engine.in_price_discovery = False
                self.engine.price_discovery_seen = 100
                self.assertTrue(self.quote().eligible)
                self.engine.mark = (mark, 101)
                result = self.quote(current_book, now=101)
                self.assertFalse(result.eligible)
                self.assertEqual(result.reason, reason)

    def test_weekend_window_and_last_hour(self):
        tz = ZoneInfo('Asia/Shanghai')
        for at, active, reducing in [('2026-11-07T08:59:59',False,False),
              ('2026-11-07T09:00:00',True,False), ('2026-11-09T04:00:00',True,True),
              ('2026-11-09T05:00:00',False,False)]:
            value = maker.window_state(self.settings, datetime.fromisoformat(at).replace(tzinfo=tz))
            self.assertEqual((value.active, value.reducing), (active, reducing))


if __name__ == '__main__':
    unittest.main()
