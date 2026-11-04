"""M14 健康日志与摘要持续记录中心来源、超慢锚偏差及无偏移模式。"""

import unittest
from datetime import datetime
from decimal import Decimal as D
from zoneinfo import ZoneInfo

from pendle.maker import Maker, MarketRules
from pendle.maker_config import MakerSettings
from test_m5_observations import MemoryLogger, ObservationAlerts
from test_maker import MARKET
from test_maker_e2e import Clock, Files


class M14ObservationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock, self.logger, self.alerts = Clock(), MemoryLogger(), ObservationAlerts()
        self.bot = Maker(MakerSettings(), api=None, alerter=self.alerts,
            rules=MarketRules('.01', '.001'), files=Files(), logger=self.logger,
            monotonic=self.clock, clock=self.clock.wall, sleep=self.clock.sleep)

    async def publish(self, *, source, center):
        center = D(center)
        half_spread = D('.5') if source == 'mark' else D('.1')
        source_time = datetime.fromtimestamp(self.clock.wall(), ZoneInfo('UTC')).isoformat()
        await self.bot.on_public({'channel': 'depthBooksPerps', 'data': [{
            'market': MARKET, 'time': source_time,
            'bids': [[str(center-half_spread), '1'], [str(center-1), '1']],
            'asks': [[str(center+half_spread), '1'], [str(center+1), '1']]}]})
        await self.bot.on_public({'channel': self.bot.settings.mark_channel,
            'timestamp': source_time, 'data': [{'market': MARKET,
                'markPrice': str(center), 'inPriceDiscovery': False,
                'lastUpdatedTime': source_time}]})

    async def monitor_once(self):
        async def finish_iteration(delay):
            self.bot.stop_event.set()

        self.bot.sleep = finish_iteration
        await self.bot.monitor_loop()
        self.bot.stop_event.clear()
        health = next(row for row in reversed(self.logger.rows) if row['事件'] == '健康状态')
        return health, self.alerts.summaries[-1]

    async def check_source_metrics(self, source):
        await self.publish(source=source, center='100')
        await self.bot.tick()
        self.assertTrue(self.bot.last_decision.eligible, self.bot.last_decision.reason)
        self.clock.now += 10
        await self.publish(source=source, center='100.01')
        await self.bot.tick()
        self.assertTrue(self.bot.last_decision.eligible, self.bot.last_decision.reason)
        health, summary = await self.monitor_once()
        for row in (health, summary):
            self.assertEqual(row['中心价来源'], source)
            self.assertIs(row['无偏移模式'], source == 'mark')
            drift = D(row['超慢锚偏差bps'])
            self.assertGreater(drift, 0)
            self.assertLessEqual(drift, 1)
        self.assertEqual(health['超慢锚偏差bps'], summary['超慢锚偏差bps'])

    async def test_mark_health_and_summary_report_slow_drift_and_no_skew_mode(self):
        await self.check_source_metrics('mark')

    async def test_mid_health_and_summary_report_slow_drift_and_inventory_skew_mode(self):
        await self.check_source_metrics('third_party_mid')

    async def test_missing_initial_center_has_explicit_empty_anchor_metrics(self):
        await self.bot.tick()
        health, summary = await self.monitor_once()
        for row in (health, summary):
            self.assertIsNone(row['中心价来源'])
            self.assertIsNone(row['超慢锚偏差bps'])
            self.assertIs(row['无偏移模式'], False)


if __name__ == '__main__':
    unittest.main()
