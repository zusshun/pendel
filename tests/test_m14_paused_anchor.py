"""Paused quoting does not freeze the ultra-slow anchor for valid centers; invalid market data must not advance it."""

import unittest
from copy import copy
from dataclasses import replace
from decimal import Decimal as D

from pendle.maker import Maker, MarketRules
from pendle.maker_config import MakerSettings
from test_m13_center import depth
from test_maker_e2e import Alerts, Clock, Files


class M14PausedAnchorTests(unittest.IsolatedAsyncioTestCase):
    async def make_bot(self):
        clock = Clock()
        bot = Maker(MakerSettings(), api=None, alerter=Alerts(), files=Files(),
                    rules=MarketRules('.000001', '.001'), monotonic=clock,
                    clock=clock.wall, sleep=clock.sleep)
        self.refresh(bot, clock, '100')
        await bot.tick()
        self.assertEqual(bot.engine.slow_anchor, D(100))
        return bot, clock

    def refresh(self, bot, clock, center, *, competing=False):
        center = D(center)
        clock.now = 3700 if center != 100 else 100
        bids, asks = (), ()
        if competing:
            bids = ((center-D('.1'), '100'), (center-1, '1'))
            asks = ((center+D('.1'), '100'), (center+1, '1'))
        bot.book, bot.book_seen = depth(bids, asks, clock()), clock()
        bot.engine.mark = (center, clock())
        bot.engine.in_price_discovery = False
        bot.engine.price_discovery_seen = clock()
        times = (clock.wall(), clock.wall())
        bot.engine.mark_source_times = bot.engine.discovery_source_times = times

    async def test_persistent_halt_with_fresh_market_data_keeps_updating_slow_anchor(self):
        bot, clock = await self.make_bot()
        bot.halt.exit_code = 76
        self.refresh(bot, clock, '101')
        await bot.tick()
        self.assertAlmostEqual(float(bot.engine.slow_anchor), 100.6321205588, places=9)
        self.assertEqual(bot.engine.slow_anchor_at, clock())
        self.assertFalse(bot.state.active_orders())

    async def test_valid_price_discovery_pause_keeps_updating_slow_anchor(self):
        bot, clock = await self.make_bot()
        self.refresh(bot, clock, '101')
        bot.engine.in_price_discovery = True
        await bot.tick()
        self.assertAlmostEqual(float(bot.engine.slow_anchor), 100.6321205588, places=9)
        self.assertFalse(bot.last_decision.eligible)
        self.assertIn('价格发现', bot.last_decision.reason)
        self.assertFalse(bot.state.active_orders())

    async def test_outside_window_pause_keeps_updating_fresh_center_slow_anchor(self):
        bot, clock = await self.make_bot()
        clock.epoch += 2*24*3600  # Fresh market data remains available after the Monday window ends.
        self.refresh(bot, clock, '101')
        await bot.tick()
        self.assertAlmostEqual(float(bot.engine.slow_anchor), 100.6321205588, places=9)
        self.assertTrue(bot.waiting)
        self.assertFalse(bot.state.active_orders())

    async def test_guard_cooldown_keeps_updating_slow_anchor(self):
        bot, clock = await self.make_bot()
        await bot.guard.trigger('测试守卫停报')
        self.refresh(bot, clock, '100.1')
        bot.guard.triggered_at = clock()
        await bot.tick()
        self.assertGreater(bot.engine.slow_anchor, D(100))
        self.assertEqual(bot.guard.state, 'COOLDOWN')
        self.assertFalse(bot.state.active_orders())

    async def test_competition_pause_updates_slow_anchor_before_rejecting_quotes(self):
        bot, clock = await self.make_bot()
        self.refresh(bot, clock, '100.1', competing=True)
        await bot.tick()
        self.assertGreater(bot.engine.slow_anchor, D(100))
        self.assertEqual(bot.last_decision.reason, '竞争量超过判据')
        self.assertFalse(bot.last_decision.quotes)

    async def test_halted_observation_ignores_invalid_or_expired_market_data(self):
        for invalid in ('book', 'mark_source', 'discovery_source', 'discovery_value'):
            with self.subTest(invalid=invalid):
                bot, clock = await self.make_bot()
                bot.halt.exit_code = 76
                self.refresh(bot, clock, '101')
                if invalid == 'book':
                    bot.book_seen -= bot.settings.max_book_age_s+1
                elif invalid == 'mark_source':
                    bot.engine.mark_source_times = ()
                elif invalid == 'discovery_source':
                    bot.engine.discovery_source_times = ()
                else:
                    bot.engine.in_price_discovery = None
                await bot.tick()
                self.assertEqual(bot.engine.slow_anchor, D(100))
                self.assertEqual(bot.engine.slow_anchor_at, 100)

    async def test_same_time_repeated_observation_does_not_dilute_slow_guard(self):
        bot, clock = await self.make_bot()
        bot.engine.settings = replace(bot.settings, max_slow_drift_bps=D(40))
        self.refresh(bot, clock, '101')
        for engine in (bot.engine, bot.engine, copy(bot.engine)):
            decision = engine.evaluate(bot.book, bot.state, bot.rules, now=clock(),
                                       guard_only=True)
            self.assertFalse(decision.eligible)
            self.assertIn('超慢锚', decision.reason)
            self.assertEqual(engine.slow_drift_bps, D(100))


if __name__ == '__main__':
    unittest.main()
