"""M10 首份有效行情等待与后续过期使用内存依赖验证。"""

import unittest
from dataclasses import replace
from datetime import datetime
from decimal import Decimal as D
from zoneinfo import ZoneInfo

from pendle.maker import Maker, MarketRules
from pendle.maker_config import MakerSettings
from test_maker import MARKET
from test_maker_e2e import Alerts, Clock, Exchange, Files


class StartupDataWaitTests(unittest.IsolatedAsyncioTestCase):
    def make_bot(self, mode='dry-run'):
        clock, files = Clock(), Files()
        exchange = Exchange(clock, files)
        settings = replace(MakerSettings(), mode=mode, guards_calibrated=True,
                           sandbox_verified=True, healthcheck_waived=True, quiet_s=.01)
        bot = Maker(settings, api=exchange if mode != 'dry-run' else None,
                    alerter=Alerts(), rules=MarketRules('.01', '.001'), files=files,
                    monotonic=clock, clock=clock.wall, sleep=clock.sleep)
        return bot, clock, exchange

    async def publish_book(self, bot, clock):
        await bot.on_public({'channel': 'depthBooksPerps', 'data': [{
            'market': MARKET,
            'time': datetime.fromtimestamp(clock.wall(), ZoneInfo('UTC')).isoformat(),
            'bids': [['99.9', '10'], ['99', '10']],
            'asks': [['100.1', '10'], ['101', '10']]}]})

    async def publish_mark(self, bot):
        source_time = datetime.fromtimestamp(bot.clock(), ZoneInfo('UTC')).isoformat()
        await bot.on_public({'channel': bot.settings.mark_channel, 'timestamp': source_time,
            'data': [{'market': MARKET, 'markPrice': '100', 'inPriceDiscovery': False,
                      'lastUpdatedTime': source_time}]})

    def assert_waiting_without_stop(self, bot, exchange, reason):
        self.assertTrue(bot.waiting)
        self.assertFalse(bot.qualified)
        self.assertEqual(bot.observed_reason, reason)
        self.assertEqual(bot.guard.state, 'RUNNING')
        self.assertIsNone(bot.guard.triggered_at)
        self.assertFalse(bot.state.stopping)
        self.assertIsNone(bot.last_stop_reason)
        self.assertEqual(exchange.calls, [])

    async def test_no_first_book_waits_without_cooldown_or_cancel_in_all_modes(self):
        for mode in ('dry-run', 'sandbox', 'live'):
            with self.subTest(mode=mode):
                bot, clock, exchange = self.make_bot(mode)
                for gap in (0, 10, 120):
                    clock.now += gap
                    await bot.tick()
                    self.assert_waiting_without_stop(bot, exchange, '等待首个盘口')
                self.assertNotIn('公开盘口过期', bot.pause_counts)

    async def test_no_first_mark_waits_without_cooldown_or_cancel_in_all_modes(self):
        for mode in ('dry-run', 'sandbox', 'live'):
            with self.subTest(mode=mode):
                bot, clock, exchange = self.make_bot(mode)
                for gap in (0, 10, 120):
                    clock.now += gap
                    await self.publish_book(bot, clock)
                    await bot.tick()
                    self.assert_waiting_without_stop(bot, exchange, '等待首个标记价格')

    async def test_no_first_discovery_waits_without_cooldown_or_cancel_in_all_modes(self):
        for mode in ('dry-run', 'sandbox', 'live'):
            with self.subTest(mode=mode):
                bot, clock, exchange = self.make_bot(mode)
                for gap in (0, 10, 120):
                    clock.now += gap
                    await self.publish_book(bot, clock)
                    bot.engine.mark = (D(100), clock())
                    await bot.tick()
                    self.assert_waiting_without_stop(bot, exchange, '等待首个价格发现状态')

    async def test_first_valid_data_quotes_immediately_without_startup_cooldown(self):
        bot, clock, exchange = self.make_bot()
        await bot.tick()
        clock.now += 120
        await self.publish_book(bot, clock)
        await bot.tick()
        await self.publish_mark(bot)
        await bot.tick()
        self.assertEqual(bot.guard.state, 'RUNNING')
        self.assertFalse(bot.waiting)
        self.assertTrue(bot.qualified)
        self.assertIsNone(bot.observed_reason)
        self.assertFalse(bot.state.stopping)
        self.assertEqual(exchange.calls, [])

    async def test_book_expires_only_after_first_valid_snapshot(self):
        for mode in ('dry-run', 'sandbox', 'live'):
            with self.subTest(mode=mode):
                bot, clock, exchange = self.make_bot(mode)
                await self.publish_book(bot, clock)
                await self.publish_mark(bot)
                clock.now += bot.settings.max_book_age_s + .001
                await bot.tick()
                self.assertEqual(bot.guard.state, 'COOLDOWN')
                self.assertEqual(bot.guard.reason, '公开盘口过期')
                if mode == 'dry-run':
                    self.assertEqual(exchange.calls, [])
                else:
                    self.assertTrue(any(call[0] == 'cancel_all' for call in exchange.calls))

    async def test_mark_expires_only_after_first_valid_mark_including_dry_run(self):
        for mode in ('dry-run', 'sandbox', 'live'):
            with self.subTest(mode=mode):
                bot, clock, exchange = self.make_bot(mode)
                await self.publish_mark(bot)
                clock.now += bot.settings.mark_quiet_max_s + .001
                await self.publish_book(bot, clock)
                bot.engine.price_discovery_seen = clock()
                await bot.tick()
                self.assertEqual(bot.guard.state, 'COOLDOWN')
                self.assertIn('标记价格', bot.guard.reason)
                self.assertFalse(bot.qualified)

    async def test_discovery_expires_after_first_confirmation_with_fresh_mark(self):
        for mode in ('dry-run', 'sandbox', 'live'):
            with self.subTest(mode=mode):
                bot, clock, exchange = self.make_bot(mode)
                await self.publish_mark(bot)
                clock.now += bot.settings.discovery_quiet_max_s + .001
                await self.publish_book(bot, clock)
                bot.engine.mark = (D(100), clock())
                bot.engine.mark_ws_seen = clock()
                bot.engine.mark_source_times = (clock.wall(), clock.wall())
                await bot.tick()
                self.assertEqual(bot.guard.state, 'COOLDOWN')
                self.assertIn('价格发现', bot.guard.reason)

    async def test_received_mark_expiry_is_not_hidden_by_initial_discovery_wait(self):
        for mode in ('dry-run', 'sandbox', 'live'):
            with self.subTest(mode=mode):
                bot, clock, exchange = self.make_bot(mode)
                bot.engine.mark = (D(100), clock())
                clock.now += bot.settings.mark_quiet_max_s + .001
                await self.publish_book(bot, clock)
                await bot.tick()
                self.assertEqual(bot.guard.state, 'COOLDOWN')
                self.assertIn('标记价格', bot.guard.reason)


if __name__ == '__main__':
    unittest.main()
