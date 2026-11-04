"""M13 中心来源按虚拟时钟累计，健康日志与摘要共享同一统计。"""

import unittest
from dataclasses import replace
from datetime import datetime
from decimal import Decimal as D
from zoneinfo import ZoneInfo

from pendle.maker import Maker, MarketRules
from pendle.maker_config import MakerSettings
from test_m5_observations import MemoryLogger, ObservationAlerts
from test_maker import MARKET
from test_maker_e2e import Clock, Files


class CenterObservationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock, self.logger, self.alerts = Clock(), MemoryLogger(), ObservationAlerts()
        self.bot = Maker(replace(MakerSettings(), cooldown_s=.01, stable_s=.01),
            api=None, alerter=self.alerts, files=Files(), logger=self.logger,
            rules=MarketRules(D('.01'), D('.001')), monotonic=self.clock,
            clock=self.clock.wall, sleep=self.clock.sleep)

    async def publish(self, *, wide=False, mark='100'):
        bid, ask = ('99.5', '100.5') if wide else ('99.9', '100.1')
        source_time = datetime.fromtimestamp(self.clock.wall(), ZoneInfo('UTC')).isoformat()
        await self.bot.on_public({'channel': 'depthBooksPerps', 'data': [{
            'market': MARKET,
            'time': datetime.fromtimestamp(self.clock.wall(), ZoneInfo('UTC')).isoformat(),
            'bids': [[bid, '10'], ['99', '10']],
            'asks': [[ask, '10'], ['101', '10']]}]})
        if mark is not None:
            await self.bot.on_public({'channel': self.bot.settings.mark_channel,
                'timestamp': source_time,
                'data': [{'market': MARKET, 'markPrice': mark, 'inPriceDiscovery': False,
                          'lastUpdatedTime': source_time}]})

    async def monitor_once(self):
        async def finish_iteration(delay):
            self.bot.stop_event.set()

        self.bot.sleep = finish_iteration
        self.bot.stop_event.clear()
        await self.bot.monitor_loop()
        self.bot.stop_event.clear()
        return self.alerts.summaries[-1]

    def assert_sources(self, row, source, third_party_seconds, mark_seconds):
        self.assertIn('中心价来源', row)
        self.assertEqual(row['中心价来源'], source)
        durations = row['中心价来源时长秒']
        self.assertEqual(set(durations), {'third_party_mid', 'mark'})
        self.assertAlmostEqual(durations['third_party_mid'], third_party_seconds)
        self.assertAlmostEqual(durations['mark'], mark_seconds)

    async def test_source_duration_tracks_narrow_to_wide_center_switch(self):
        await self.publish()
        await self.bot.tick()
        self.assertTrue(self.bot.last_decision.eligible, self.bot.last_decision.reason)
        self.assertEqual(getattr(self.bot.last_decision, 'center_source', None), 'third_party_mid')
        self.clock.now += self.bot.settings.source_min_dwell_s
        await self.publish(wide=True)
        await self.bot.tick()
        self.assertTrue(self.bot.last_decision.eligible, self.bot.last_decision.reason)
        self.assertEqual(self.bot.last_decision.center_source, 'mark')
        self.clock.now += 3
        summary = await self.monitor_once()
        self.assert_sources(summary, 'mark', 30, 3)
        health = next(row for row in reversed(self.logger.rows) if row['事件'] == '健康状态')
        self.assert_sources(health, 'mark', 30, 3)

    async def test_missing_first_prices_have_no_current_source_or_source_duration(self):
        await self.bot.tick()
        self.clock.now += 5
        summary = await self.monitor_once()
        self.assert_sources(summary, None, 0, 0)

    async def test_expired_mark_wait_does_not_inherit_last_selected_source(self):
        await self.publish(wide=True)
        await self.bot.tick()
        self.assertTrue(self.bot.last_decision.eligible, self.bot.last_decision.reason)
        self.clock.now += 3
        await self.publish(wide=True, mark=None)
        self.bot.engine.mark = (D(100), self.clock()-self.bot.settings.mark_quiet_max_s-.001)
        self.bot.engine.mark_ws_seen = self.bot.engine.mark[1]
        await self.bot.tick()
        self.assertFalse(self.bot.qualified)
        self.assertIn('标记价格', self.bot.guard.reason)
        summary = await self.monitor_once()
        self.assert_sources(summary, None, 0, 3)
        self.clock.now += 7
        summary = await self.monitor_once()
        self.assert_sources(summary, None, 0, 3)

    async def test_expired_book_wait_does_not_inherit_last_selected_source(self):
        await self.publish()
        await self.bot.tick()
        self.clock.now += self.bot.settings.max_book_age_s+.001
        self.bot.engine.mark = (D(100), self.clock())
        self.bot.engine.price_discovery_seen = self.clock()
        await self.bot.tick()
        self.assertIn('盘口过期', self.bot.guard.reason)
        summary = await self.monitor_once()
        selected_seconds = self.bot.settings.max_book_age_s+.001
        self.assert_sources(summary, None, selected_seconds, 0)
        self.clock.now += 7
        summary = await self.monitor_once()
        self.assert_sources(summary, None, selected_seconds, 0)

    async def test_guard_with_selected_center_still_records_its_source(self):
        await self.publish(mark='101')
        await self.bot.tick()
        self.assertFalse(self.bot.last_decision.eligible)
        self.assertEqual(self.bot.last_decision.center, D(100))
        self.clock.now += 2
        summary = await self.monitor_once()
        self.assert_sources(summary, 'third_party_mid', 2, 0)

    async def test_monitor_repetition_cannot_double_count_elapsed_source_duration(self):
        await self.publish()
        await self.bot.tick()
        self.clock.now += 2
        first = await self.monitor_once()
        second = await self.monitor_once()
        self.assert_sources(first, 'third_party_mid', 2, 0)
        self.assert_sources(second, 'third_party_mid', 2, 0)
        self.clock.now += 1
        third = await self.monitor_once()
        self.assert_sources(third, 'third_party_mid', 3, 0)

    async def test_dry_run_quote_logs_center_source_for_both_pricing_paths(self):
        await self.publish()
        await self.bot.tick()
        self.clock.now += self.bot.settings.source_min_dwell_s
        await self.publish(wide=True)
        await self.bot.tick()
        quotes = [row for row in self.logger.rows if row['事件'] == '拟报价']
        self.assertEqual(len(quotes), 2)
        self.assertEqual([row.get('中心价来源') for row in quotes], ['third_party_mid', 'mark'])


if __name__ == '__main__':
    unittest.main()
