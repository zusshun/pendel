"""M14 regressions for source hysteresis, zero-offset quotes, and full order book feedback stress."""

import unittest
from copy import copy
from dataclasses import replace
from decimal import Decimal as D

from pendle.maker import BPS, MarketRules, QuoteEngine
from pendle.maker_config import MakerSettings
from pendle.maker_orders import AccountState, Order, OrderStatus
from test_m13_center import MARKET, depth
import test_m5_requote as m5


class M14CenterTests(unittest.TestCase):
    def setUp(self):
        self.settings = MakerSettings()
        self.engine = QuoteEngine(self.settings)
        self.state = AccountState([MARKET])
        self.rules = MarketRules('.000001', '.001')

    def evaluate(self, *, spread=None, now=100, mark=D(100), center=D(100)):
        self.engine.mark = (mark, now)
        self.engine.in_price_discovery = False
        self.engine.price_discovery_seen = now
        half = center*D(spread)/BPS/2 if spread is not None else None
        book = (depth((), (), now) if half is None else
                depth(((center-half, '1'), (center-1, '1')),
                      ((center+half, '1'), (center+1, '1')), now))
        return self.engine.evaluate(book, self.state, self.rules, now=now, guard_only=True)

    def test_source_hysteresis_uses_twenty_and_fifteen_bps_boundaries(self):
        for spread, now, expected in ((20, 100, 'third_party_mid'),
                                      (21, 130, 'mark'),
                                      (16, 160, 'mark'),
                                      (15, 160, 'third_party_mid'),
                                      (20, 190, 'third_party_mid'),
                                      ('20.001', 190, 'mark')):
            result = self.evaluate(spread=spread, now=now)
            self.assertTrue(result.eligible, result.reason)
            self.assertEqual(result.center_source, expected)

    def test_repeated_threshold_oscillation_obeys_hysteresis_and_minimum_dwell(self):
        sources = []
        for elapsed in range(181):
            spread = (D('20.1'), D('19.9'), D('14.9'))[elapsed % 3]
            result = self.evaluate(spread=spread, now=100+elapsed)
            self.assertTrue(result.eligible, result.reason)
            sources.append(result.center_source)
        switches = [i for i in range(1, len(sources)) if sources[i] != sources[i-1]]
        self.assertTrue(switches)
        self.assertGreaterEqual(switches[0], 30)
        self.assertTrue(all(right-left >= 30 for left, right in zip(switches, switches[1:])))
        self.assertLessEqual(len(switches), 6)

    def test_mark_mode_is_symmetric_before_and_after_soft_cap_for_both_inventory_signs(self):
        for position in ('.1', '-.1', '3.5', '-3.5'):
            with self.subTest(position=position):
                self.state.positions[MARKET] = D(position)
                result = self.evaluate(mark=D('100.000043'))
                self.assertTrue(result.eligible, result.reason)
                buy, sell = result.quotes
                self.assertLessEqual(abs((result.center-buy.price)-(sell.price-result.center)),
                                     self.rules.tick)
                if abs(D(position)) >= 3:
                    adding = buy if D(position) > 0 else sell
                    self.assertLessEqual(adding.price*adding.quantity, D(200))
                    self.assertLessEqual(abs(adding.price/result.center-1)*BPS, D('12.001'))

    def test_third_party_mode_keeps_inventory_distance_skew_and_soft_edge(self):
        self.state.positions[MARKET] = D('3.5')
        result = self.evaluate(spread=10)
        self.assertTrue(result.eligible, result.reason)
        buy, sell = result.quotes
        self.assertEqual(buy.price, D('99.78'))
        self.assertEqual(sell.price, D('100.08'))

    def feedback(self, position, *, asymmetric=False):
        self.setUp()
        if asymmetric:
            self.assertTrue(hasattr(self.settings, 'max_mark_phase_drift_bps'))
            self.settings = replace(self.settings, max_mark_phase_drift_bps=D(40))
            self.engine.settings = self.settings
        self.state.positions[MARKET] = D(position)
        first = D(100) if asymmetric else D('100.000043')
        mark, previous = first, ()
        stopped = None
        for elapsed in range(0, 3601, 10):
            now = 100+elapsed
            if previous:
                # The stress model has no external price component: the mark follows our two-sided quote midpoint entirely.
                mark = sum((q.price for q in previous), D(0))/2
            self.engine.mark = (mark, now)
            self.engine.in_price_discovery = False
            self.engine.price_discovery_seen = now
            self.state.orders = {q.side: Order(q.side, MARKET, q.side, q.quantity, q.price,
                status=OrderStatus.OPEN, exchange_ms=int(now*1000)) for q in previous}
            book = depth(tuple((q.price, q.quantity) for q in previous if q.side == 'buy'),
                         tuple((q.price, q.quantity) for q in previous if q.side == 'sell'), now)
            result = self.engine.evaluate(book, self.state, self.rules, now=now, guard_only=True)
            if not result.eligible:
                stopped = (elapsed, mark, result)
                break
            previous = result.quotes
            if asymmetric:
                direction = 1 if D(position) > 0 else -1
                previous = tuple(replace(q, price=q.price+direction*D('.0025'))
                                 if q.side == 'buy' else q for q in previous)
            else:
                self.assertLessEqual(abs(mark-first), self.rules.tick)
        return first, stopped

    def test_one_hour_full_self_reference_with_persistent_long_and_short_inventory_is_tick_bounded(self):
        for position in ('.1', '-.1', '3.5', '-3.5'):
            with self.subTest(position=position):
                _, stopped = self.feedback(position)
                self.assertIsNone(stopped, stopped)

    def test_asymmetric_feedback_stops_at_configured_mark_phase_drift_threshold(self):
        for position in ('.1', '-.1', '3.5', '-3.5'):
            with self.subTest(position=position):
                first, stopped = self.feedback(position, asymmetric=True)
                self.assertIsNotNone(stopped, '连续不对称不能被慢锚与基差共同跟随放行')
                elapsed, mark, result = stopped
                self.assertIn('超慢锚', result.reason)
                self.assertFalse(result.quotes)
                self.assertGreaterEqual(abs(mark/first-1)*BPS,
                                        self.settings.max_mark_phase_drift_bps)
                self.assertLess(abs(mark/first-1)*BPS,
                                self.settings.max_mark_phase_drift_bps+D('.126'))
                self.assertLess(self.engine.slow_drift_bps, self.settings.max_slow_drift_bps)
                self.assertLess(elapsed, 3600)

    def test_slow_anchor_uses_elapsed_time_and_updates_during_guard_rejection(self):
        self.assertTrue(hasattr(self.settings, 'max_mark_phase_drift_bps'))
        self.engine.settings = replace(self.settings, max_slow_drift_bps=D(40),
                                       max_mark_phase_drift_bps=D(40))
        self.assertTrue(self.evaluate().eligible)
        result = self.evaluate(mark=D(101), now=3700)
        self.assertFalse(result.eligible)
        self.assertIn('超慢锚', result.reason)
        self.assertAlmostEqual(float(self.engine.slow_anchor), 100.6321205588, places=9)
        before = self.engine.slow_anchor
        clone = copy(self.engine)
        self.evaluate(mark=D(101), now=7300)
        self.assertGreater(self.engine.slow_anchor, before)
        self.assertEqual(clone.slow_anchor, before)
        self.assertFalse(self.evaluate(mark=D(101), now=10900).eligible,
                         '停报期间不能因 EMA 追上而重置累计反馈基准')

    def test_slow_drift_guard_allows_threshold_and_blocks_above_it(self):
        self.engine.settings = replace(self.settings, max_slow_drift_bps=D(40),
                                       max_anchor_drift_bps=D(200))
        self.assertTrue(self.evaluate().eligible)
        boundary = self.evaluate(mark=D('100.4'), now=3700)
        self.assertTrue(boundary.eligible, boundary.reason)
        self.assertEqual(self.engine.slow_drift_bps, D(40))
        rejected = self.evaluate(mark=D('100.400001'), now=3700)
        self.assertFalse(rejected.eligible)
        self.assertIn('超慢锚', rejected.reason)
        self.assertFalse(rejected.quotes)
        self.assertGreater(self.engine.slow_drift_bps, D(40))

    def test_mark_phase_drift_guard_blocks_at_threshold_independently_of_slow_anchor(self):
        self.assertTrue(hasattr(self.settings, 'max_mark_phase_drift_bps'))
        self.engine.settings = replace(self.settings, max_mark_phase_drift_bps=D(40),
                                       max_anchor_drift_bps=D(200))
        self.assertTrue(self.evaluate().eligible)
        below = self.evaluate(mark=D('100.399999'), now=3700)
        self.assertTrue(below.eligible, below.reason)
        rejected = self.evaluate(mark=D('100.4'), now=7300)
        self.assertFalse(rejected.eligible)
        self.assertIn('超慢锚', rejected.reason)
        self.assertFalse(rejected.quotes)
        self.assertEqual(abs(rejected.center/self.engine.mark_reference-1)*BPS, D(40))
        self.assertLess(self.engine.slow_drift_bps, self.settings.max_slow_drift_bps)

    def test_mark_phase_smooth_one_point_five_percent_rise_keeps_quoting(self):
        for elapsed in range(3601):
            mark = D(100)*(1+D('.015')*elapsed/3600)
            result = self.evaluate(mark=mark, now=100+elapsed)
            self.assertTrue(result.eligible, (elapsed, result.reason))
            self.assertEqual(result.center_source, 'mark')
            self.assertEqual(len(result.quotes), 2)
            self.assertEqual(self.engine.mark_reference, D(100))
        self.assertEqual(result.center, D('101.5'))
        self.assertGreater(self.engine.slow_drift_bps, D(40))
        self.assertLess(self.engine.slow_drift_bps, self.settings.max_slow_drift_bps)


class M14SourceRequoteTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = m5.M5RequoteTests.asyncSetUp
    asyncTearDown = m5.M5RequoteTests.asyncTearDown
    wall = m5.M5RequoteTests.wall
    drive = m5.M5RequoteTests.drive
    ready = m5.M5RequoteTests.ready
    publish = m5.M5RequoteTests.publish
    deletes = m5.M5RequoteTests.deletes

    async def source_transition(self, bid, ask, expected_source, expected_center):
        await self.ready()
        self.bot.book = depth((('99', '1'),), (('101', '1'),), self.wall())
        await self.drive(self.bot.tick())
        self.assertEqual(self.bot.last_decision.center_source, 'mark')
        before = {cid: dict(row) for cid, row in self.remote.items()}
        self.assertEqual(len(before), 2)
        self.clock.advance(30)
        self.publish(center='100.01')
        self.bot.engine.mark = (D(100), self.clock.monotonic())
        bids, asks = [(bid, '1'), ('99', '1')], [(ask, '1'), ('101', '1')]
        for row in self.remote.values():
            (bids if row['side'] == 'buy' else asks).append((row['price'], row['size']))
        self.bot.book = depth(bids, asks, self.wall())
        self.bot.book_seen = self.clock.monotonic()
        self.bot.last_requote = self.clock.monotonic()
        self.requests.clear()
        await self.drive(self.bot.tick())
        self.assertEqual(self.bot.last_decision.center_source, expected_source)
        self.assertEqual(self.bot.last_decision.center, D(expected_center))
        self.assertEqual(self.deletes(), [])
        self.assertEqual(self.remote, before)

    async def test_one_bps_source_switch_keeps_compliant_pair_without_cancel(self):
        # After switching to the mark, the spread must fall to 15bps before switching back.
        await self.source_transition('99.94', '100.08', 'third_party_mid', '100.01')

    async def test_review_twenty_bps_book_does_not_switch_or_cancel_mark_quotes(self):
        await self.source_transition('99.91', '100.11', 'mark', '100')


if __name__ == '__main__':
    unittest.main()
