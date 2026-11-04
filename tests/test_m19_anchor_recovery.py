"""M19：短周期锚按时间跟踪真实中心，停报后可自动恢复。"""

import math
import tempfile
import unittest
from copy import copy
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path

from pendle.maker import Maker, MarketRules, QuoteEngine
from pendle.maker_config import MakerSettings
from pendle.maker_orders import AccountState
from test_m13_center import MARKET, depth
from test_maker_e2e import Alerts, Clock, Files


class M19AnchorSettingsTests(unittest.TestCase):
    def test_anchor_tau_defaults_to_120_seconds_and_loads_from_configuration(self):
        self.assertEqual(getattr(MakerSettings(), 'anchor_ema_tau_s', None), 120.0)
        settings = MakerSettings.from_dict({'anchor_ema_tau_s': '90.5'})
        self.assertEqual(settings.anchor_ema_tau_s, 90.5)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'maker.toml'
            path.write_text('[maker]\nanchor_ema_tau_s = 180.5\n')
            self.assertEqual(MakerSettings.from_file(path).anchor_ema_tau_s, 180.5)
        self.assertEqual(settings.anchor_ema_alpha, D('.01'))

    def test_anchor_tau_rejects_nonpositive_nonfinite_and_boolean_values(self):
        for value in (0, -1, 'NaN', 'Infinity', '-Infinity', True, False):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, 'anchor_ema_tau_s 必须是大于零的有限数字'):
                    MakerSettings.from_dict({'anchor_ema_tau_s': value})


class M19AnchorObservationTests(unittest.TestCase):
    def setUp(self):
        self.settings = MakerSettings()
        self.engine = QuoteEngine(self.settings)
        self.state = AccountState([MARKET])
        self.rules = MarketRules('.000001', '.001')

    def evaluate(self, center='100', *, now=100, mark=None, narrow=False):
        center = D(center)
        self.engine.mark = (center if mark is None else D(mark), now)
        self.engine.in_price_discovery = False
        self.engine.price_discovery_seen = now
        book = (depth(((center-D('.05'), '1'), (center-1, '1')),
                      ((center+D('.05'), '1'), (center+1, '1')), now)
                if narrow else depth((), (), now))
        return self.engine.evaluate(book, self.state, self.rules, now=now)

    def test_anchor_ema_uses_elapsed_time_independently_of_tick_frequency(self):
        for interval in (.5, 1, 10):
            with self.subTest(interval=interval):
                self.setUp()
                self.assertTrue(self.evaluate().eligible)
                for tick in range(1, int(120/interval)+1):
                    self.evaluate('100.3', now=100+tick*interval)
                expected = 100+.3*(1-math.exp(-1))
                self.assertAlmostEqual(float(self.engine.ema), expected, places=10)

    def test_guard_uses_pre_update_ema_even_for_same_time_rechecks_and_copy(self):
        for narrow in (False, True):
            with self.subTest(narrow=narrow):
                self.setUp()
                self.assertTrue(self.evaluate(narrow=narrow).eligible)
                rejected = self.evaluate('100.3', now=220, narrow=narrow)
                self.assertFalse(rejected.eligible)
                self.assertEqual(rejected.reason, '偏离独立慢锚')
                self.assertGreater(self.engine.ema, D('100.15'))
                updated = self.engine.ema
                for engine in (self.engine, copy(self.engine)):
                    original = self.engine
                    self.engine = engine
                    try:
                        repeated = self.evaluate('100.3', now=220, narrow=narrow)
                    finally:
                        self.engine = original
                    self.assertFalse(repeated.eligible)
                    self.assertEqual(repeated.reason, '偏离独立慢锚')
                    self.assertEqual(engine.ema, updated)

    def test_jump_guard_uses_pre_update_history_and_records_rejected_center(self):
        self.engine.settings = replace(self.settings, max_anchor_drift_bps=D(100))
        self.assertTrue(self.evaluate().eligible)
        for engine in (self.engine, copy(self.engine)):
            engine.history = engine.history.copy()
            original = self.engine
            self.engine = engine
            try:
                rejected = self.evaluate('100.2', now=101)
                self.assertFalse(rejected.eligible)
                self.assertEqual(rejected.reason, '中心价跳变')
                self.assertIn((101, D('100.2')), engine.history)
                self.assertGreater(engine.ema, D(100))
            finally:
                self.engine = original

    def test_mid_mark_rejection_updates_history_until_jump_window_expires(self):
        self.engine.settings = replace(self.settings, max_anchor_drift_bps=D(100))
        self.assertTrue(self.evaluate(narrow=True).eligible)
        rejected = self.evaluate('100.2', mark='100', now=101, narrow=True)
        self.assertEqual(rejected.reason, '中间价与标记价格偏离超过阈值')
        # 初始样本已过期；被中间价守卫拒绝的真实中心仍须参与跳变检查。
        rejected = self.evaluate('100.4', now=106, narrow=True)
        self.assertFalse(rejected.eligible)
        self.assertEqual(rejected.reason, '中心价跳变')
        recovered = self.evaluate('100.4', now=112, narrow=True)
        self.assertTrue(recovered.eligible, recovered.reason)


class M19AnchorRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def make_bot(self):
        clock = Clock()
        bot = Maker(MakerSettings(), api=None, alerter=Alerts(), files=Files(),
                    rules=MarketRules('.000001', '.001'), monotonic=clock,
                    clock=clock.wall, sleep=clock.sleep)
        return bot, clock

    async def tick(self, bot, clock, elapsed, center):
        clock.now = 100+elapsed
        bot.book, bot.book_seen = depth((), (), clock()), clock()
        bot.engine.mark = (D(center), clock())
        bot.engine.in_price_discovery = False
        bot.engine.price_discovery_seen = clock()
        await bot.tick()

    async def test_price_step_recovers_after_cooldown_and_stable_within_five_minutes(self):
        for interval in (.5, 1, 5):
            with self.subTest(interval=interval):
                bot, clock = self.make_bot()
                await self.tick(bot, clock, 0, '100')
                self.assertTrue(bot.qualified)
                recovered_at = None
                states = set()
                for tick in range(1, int(300/interval)+1):
                    elapsed = tick*interval
                    await self.tick(bot, clock, elapsed, '100.3')
                    states.add(bot.guard.state)
                    if tick == 1:
                        self.assertFalse(bot.last_decision.eligible)
                        self.assertEqual(bot.last_decision.reason, '偏离独立慢锚')
                        self.assertFalse(bot.qualified)
                    if bot.qualified:
                        recovered_at = elapsed
                        break
                self.assertIsNotNone(recovered_at, '价格平稳五分钟后仍永久停报')
                self.assertGreaterEqual(recovered_at, 120)
                self.assertLessEqual(recovered_at, 300)
                self.assertTrue({'COOLDOWN', 'STABILIZING', 'RUNNING'} <= states)
                self.assertGreaterEqual(clock()-bot.guard.triggered_at,
                                        bot.settings.cooldown_s+bot.settings.stable_s)
                self.assertEqual(len(bot.last_decision.quotes), 2)

    async def test_fast_one_way_move_keeps_quotes_stopped(self):
        bot, clock = self.make_bot()
        await self.tick(bot, clock, 0, '100')
        for elapsed in range(1, 121):
            center = D('100.3')+D('.01')*(elapsed-1)
            await self.tick(bot, clock, elapsed, center)
            self.assertFalse(bot.last_decision.eligible)
            self.assertEqual(bot.last_decision.reason, '偏离独立慢锚')
            self.assertFalse(bot.qualified)
            self.assertFalse(bot.state.active_orders())
        self.assertGreater(bot.engine.ema, D('100.3'))


if __name__ == '__main__':
    unittest.main()
