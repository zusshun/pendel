"""M13 双来源报价中心、共同守卫及自我引用反馈；全部离线运行。"""

import unittest
from dataclasses import replace
from decimal import Decimal as D

from pendle.maker import BPS, MarketRules, QuoteEngine
from pendle.maker_config import MakerSettings
from pendle.maker_orders import AccountState, Order, OrderStatus
from pendle.market import Book


MARKET = 'IBM-USD.P'


def depth(bids=(('99', '1'),), asks=(('101', '1'),), now=100):
    # 单侧/空侧表示扣除自身后的盘口；直接构造以保留现有公共双侧校验。
    return Book('ondo', 'IBM', int(now*1000), int(now*1000),
                tuple(sorted(((D(p), D(q)) for p, q in bids), reverse=True)),
                tuple(sorted((D(p), D(q)) for p, q in asks)))


class M13CenterTests(unittest.TestCase):
    def setUp(self):
        self.settings = MakerSettings()
        self.engine = QuoteEngine(self.settings)
        self.state = AccountState([MARKET])
        self.rules = MarketRules('.000001', '.001')
        self.refresh()

    def refresh(self, mark='100', now=100):
        self.engine.mark = (D(mark), now)
        self.engine.in_price_discovery = False
        self.engine.price_discovery_seen = now

    def evaluate(self, book=None, now=100, **kwargs):
        return self.engine.evaluate(book if book is not None else depth(), self.state,
                                    self.rules, now=now, **kwargs)

    def assert_center(self, result, center, source):
        self.assertTrue(result.eligible, result.reason)
        self.assertEqual(result.center, D(center))
        self.assertEqual(getattr(result, 'center_source', None), source)
        self.assertEqual({q.side for q in result.quotes}, {'buy', 'sell'})
        for q in result.quotes:
            self.assertLessEqual(abs(q.price/self.engine.mark[0]-1)*BPS, D(22))
            self.assertGreaterEqual(abs(q.price/self.engine.mark[0]-1)*BPS, D(8))

    def test_wide_spread_uses_fresh_mark_and_quotes_inside_third_party(self):
        self.refresh('100.05')
        result = self.evaluate()
        self.assert_center(result, '100.05', 'mark')
        self.assertGreater(result.quotes[0].price, D(99))
        self.assertLess(result.quotes[1].price, D(101))

    def test_narrow_spread_including_twenty_bps_uses_third_party_mid(self):
        self.refresh('100.01')
        self.assert_center(self.evaluate(depth((('99.9', '1'), ('99', '1')),
                                               (('100.1', '1'), ('101', '1')))),
                           '100', 'third_party_mid')

    def test_mid_mark_deviation_above_eight_bps_blocks_both_sides(self):
        self.refresh('99.91')
        result = self.evaluate(depth((('99.9', '1'), ('99', '1')),
                                     (('100.1', '1'), ('101', '1'))))
        self.assertFalse(result.eligible)
        self.assertFalse(result.quotes)
        self.assertIn('中间价与标记价格偏离', result.reason)
        self.assertEqual(getattr(result, 'center_source', None), 'third_party_mid')

    def test_extreme_spread_above_three_hundred_bps_stops_quotes(self):
        self.assert_center(self.evaluate(depth((('98.5', '1'),), (('101.5', '1'),))),
                           '100', 'mark')
        result = self.evaluate(depth((('98.4999', '1'),), (('101.5', '1'),)))
        self.assertFalse(result.eligible)
        self.assertEqual(result.reason, '第三方价差超过阈值')

    def test_one_sided_or_empty_third_party_book_uses_mark(self):
        for bids, asks in (((('99', '1'),), ()), ((), (('101', '1'),)), ((), ())):
            with self.subTest(bids=bids, asks=asks):
                self.assert_center(self.evaluate(depth(bids, asks)), '100', 'mark')

    def test_stale_or_missing_mark_blocks_every_mode_and_center_source(self):
        for mode in ('dry-run', 'sandbox'):
            for book in (depth(), depth((('99.9', '1'), ('99', '1')),
                                        (('100.1', '1'), ('101', '1')))):
                stale_at = 100-self.settings.mark_quiet_max_s-.001
                for mark in (None, (D(100), stale_at), (D(100), 101)):
                    with self.subTest(mode=mode, book=book, mark=mark):
                        self.engine.settings = replace(self.settings, mode=mode)
                        self.engine.mark = mark
                        result = self.evaluate(book)
                        self.assertFalse(result.eligible)
                        self.assertIn('新鲜标记价格', result.reason)

    def test_mark_and_discovery_use_separate_quiet_limits(self):
        mark_expiry = 100+self.settings.mark_quiet_max_s
        self.assert_center(self.evaluate(now=mark_expiry), '100', 'mark')
        discovery_expiry = 100+self.settings.discovery_quiet_max_s
        self.engine.mark = (D(100), discovery_expiry)
        self.assert_center(self.evaluate(now=discovery_expiry), '100', 'mark')
        self.engine.mark = (D(100), discovery_expiry+.001)
        result = self.evaluate(now=discovery_expiry+.001)
        self.assertFalse(result.eligible)
        self.assertIn('新鲜价格发现状态', result.reason)

    def test_price_discovery_must_be_fresh_explicit_false(self):
        stale_at = 100-self.settings.discovery_quiet_max_s-.001
        for discovery, invalid, seen in ((True, False, 100), (None, False, 100),
                                       (False, True, 100), (False, False, stale_at)):
            with self.subTest(discovery=discovery, invalid=invalid, seen=seen):
                self.engine.in_price_discovery = discovery
                self.engine.price_discovery_invalid = invalid
                self.engine.price_discovery_seen = seen
                result = self.evaluate()
                self.assertFalse(result.eligible)
                self.assertIn('价格发现', result.reason)

    def test_post_only_checks_available_third_party_opponent_including_equality(self):
        for bids, asks in (((('99', '1'),), (('99.88', '1'), ('101', '1'))),
                           ((('100.12', '1'), ('99', '1')), (('101', '1'),)),
                           ((), (('99.88', '1'), ('101', '1'))),
                           ((('100.12', '1'), ('99', '1')), ())):
            with self.subTest(bids=bids, asks=asks):
                result = self.evaluate(depth(bids, asks))
                self.assertFalse(result.eligible)
                self.assertEqual(result.reason, '取整后任一侧不合规')

    def test_own_opposing_orders_are_subtracted_before_post_only_check(self):
        self.state.orders = {side: Order(side, MARKET, side, D(1), D(price),
            status=OrderStatus.OPEN, exchange_ms=100000)
            for side, price in (('buy', '99.8'), ('sell', '99.88'))}
        result = self.evaluate(depth((('99.8', '1'), ('99', '1')),
                                     (('99.88', '1'), ('101', '1'))), guard_only=True)
        self.assert_center(result, '100', 'mark')

    def test_anchor_ema_updates_from_each_actual_center_source(self):
        self.assert_center(self.evaluate(), '100', 'mark')
        self.refresh('100.01', 101)
        self.assert_center(self.evaluate(now=101), '100.01', 'mark')
        self.assertAlmostEqual(float(self.engine.ema), 100.0000829871, places=10)
        self.refresh('100.01', 130)
        self.assert_center(self.evaluate(depth((('99.95', '1'), ('99', '1')),
                                               (('100.09', '1'), ('101', '1'))), now=130),
                           '100.02', 'third_party_mid')
        self.assertAlmostEqual(float(self.engine.ema), 100.0043588131, places=10)
        self.assertEqual(self.engine.anchor, D('100.02'))

    def test_anchor_jump_and_external_guards_apply_to_both_centers(self):
        for source in ('mark', 'third_party_mid'):
            for guard in ('anchor', 'jump', 'external'):
                with self.subTest(source=source, guard=guard):
                    self.setUp()
                    if guard == 'jump':
                        self.engine.settings = replace(self.settings, max_anchor_drift_bps=D(100))
                    self.engine.external = (D(50), 100)
                    initial = (depth() if source == 'mark' else
                               depth((('99.95', '1'), ('99', '1')),
                                     (('100.05', '1'), ('101', '1'))))
                    self.assertTrue(self.evaluate(initial).eligible)
                    before_ema = self.engine.ema
                    if guard == 'external':
                        self.refresh(now=101)
                        self.engine.external = (D(51), 101)
                        changed, reason = initial, '外部参考偏离'
                    else:
                        self.refresh('100.2', 101)
                        changed = (depth() if source == 'mark' else
                                   depth((('100.15', '1'), ('99', '1')),
                                         (('100.25', '1'), ('102', '1'))))
                        reason = '偏离独立慢锚' if guard == 'anchor' else '中心价跳变'
                    result = self.evaluate(changed, now=101)
                    self.assertFalse(result.eligible)
                    self.assertEqual(result.reason, reason)
                    if guard != 'external':
                        self.assertGreater(self.engine.ema, before_ema)

    def test_one_hour_full_book_feedback_with_continuous_inventory_is_tick_bounded(self):
        # 压力模型：无固定外部项，mark 完全跟随我方双边报价中点。
        for position in ('.1', '-.1', '3.5', '-3.5'):
            with self.subTest(position=position):
                self.setUp()
                self.state.positions[MARKET] = D(position)
                mark, first, previous = D(100), D(100), ()
                for elapsed in range(0, 3601, 10):
                    now = 100+elapsed
                    if previous:
                        midpoint = sum((q.price for q in previous), D(0))/2
                        mark = midpoint
                    self.refresh(mark, now)
                    self.state.orders = {q.side: Order(q.side, MARKET, q.side, q.quantity,
                        q.price, status=OrderStatus.OPEN, exchange_ms=int(now*1000))
                        for q in previous}
                    book = depth(tuple((q.price, q.quantity) for q in previous if q.side == 'buy'),
                                 tuple((q.price, q.quantity) for q in previous if q.side == 'sell'), now)
                    result = self.evaluate(book, now=now, guard_only=True)
                    self.assert_center(result, mark, 'mark')
                    self.assertLessEqual(abs(result.center-first), self.rules.tick)
                    midpoint = sum((q.price for q in result.quotes), D(0))/2
                    self.assertLessEqual(abs(midpoint-result.center), self.rules.tick/2)
                    previous = result.quotes


if __name__ == '__main__':
    unittest.main()
