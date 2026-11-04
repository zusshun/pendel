"""M5 标定观测使用虚拟时钟与内存日志，不连接网络。"""

import json
import unittest
from dataclasses import replace
from datetime import datetime
from decimal import Decimal as D
from zoneinfo import ZoneInfo

from pendle.maker import Maker, MarketRules
from pendle.maker_config import MakerSettings
from test_maker import MARKET
from test_maker_e2e import Alerts, Clock, Files


class MemoryLogger:
    def __init__(self):
        self.rows = []

    def info(self, message):
        self.rows.append(json.loads(message))

    def debug(self, message):
        pass

    def warning(self, message):
        pass


class ObservationAlerts(Alerts):
    def __init__(self):
        super().__init__()
        self.summaries = []

    async def observe(self, **kwargs):
        self.summaries.append(kwargs['summary'])


class M5ObservationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock, self.logger, self.alerts = Clock(), MemoryLogger(), ObservationAlerts()
        self.bot = Maker(replace(MakerSettings(), cooldown_s=.01, stable_s=.01),
            api=None, alerter=self.alerts, files=Files(), logger=self.logger,
            rules=MarketRules(D('.01'), D('.001')), monotonic=self.clock,
            clock=self.clock.wall, sleep=self.clock.sleep)

    async def publish(self, *, mark='100', market=MARKET):
        source_time = datetime.fromtimestamp(self.clock.wall(), ZoneInfo('UTC')).isoformat()
        await self.bot.on_public({'channel': 'depthBooksPerps', 'data': [{
            'market': market,
            'time': datetime.fromtimestamp(self.clock.wall(), ZoneInfo('UTC')).isoformat(),
            'bids': [['99.9', '10'], ['99', '10']],
            'asks': [['100.1', '10'], ['101', '10']]}]})
        await self.bot.on_public({'channel': self.bot.settings.mark_channel,
            'timestamp': source_time,
            'data': [{'market': market, 'markPrice': mark, 'inPriceDiscovery': False,
                      'lastUpdatedTime': source_time}]})

    async def health(self):
        async def finish_iteration(delay):
            self.bot.stop_event.set()

        self.bot.sleep = finish_iteration
        self.bot.stop_event.clear()
        await self.bot.monitor_loop()
        self.bot.stop_event.clear()
        return [row for row in self.logger.rows if row['事件'] == '健康状态'][-1]

    async def test_health_logs_mark_deviation_and_both_competition_weights(self):
        await self.publish(mark='100.01')
        await self.bot.tick()
        row = await self.health()
        self.assertIn('标记价格偏差bps', row)
        self.assertEqual(D(row['标记价格偏差bps']), D(1))
        self.assertEqual(D(row['竞争量flat']), D(2000))
        self.assertEqual(D(row['竞争量linear']), D(1200))
        summary = self.alerts.summaries[-1]
        self.assertEqual(summary['竞争量flat'], row['竞争量flat'])
        self.assertEqual(summary['竞争量linear'], row['竞争量linear'])
        quote = next(row for row in self.logger.rows if row['事件'] == '拟报价')
        self.assertEqual(quote['竞争量flat'], row['竞争量flat'])

    async def test_health_logs_book_interval_max_and_nearest_rank_p90(self):
        await self.publish()
        for gap in range(1, 11):
            self.clock.now += gap
            await self.publish()
        row = await self.health()
        self.assertIn('盘口更新间隔最大秒', row)
        self.assertEqual(row['盘口更新间隔最大秒'], 10)
        self.assertEqual(row['盘口更新间隔P90秒'], 9)
        self.assertEqual(row['盘口更新间隔样本数'], 10)

    async def test_guard_counts_duration_and_qualification_use_elapsed_time(self):
        await self.publish()
        await self.bot.tick()
        self.clock.now = 110
        await self.publish(mark='101')
        await self.bot.tick()
        self.clock.now = 115
        await self.publish(mark='101')
        await self.bot.tick()
        self.clock.now = 120
        await self.publish()
        await self.bot.tick()
        self.clock.now = 130
        row = await self.health()
        self.assertIn('守卫及停报原因', row)
        reason = row['守卫及停报原因']['中间价与标记价格偏离超过阈值']
        self.assertEqual(reason['次数'], 2)
        self.assertEqual(reason['时长秒'], 20)
        self.assertAlmostEqual(row['有资格时长占比'], 1/3)
        self.assertEqual(row['观测时长秒'], 30)

    async def test_minute_health_cadence_and_missing_prices_are_explicit(self):
        row = await self.health()
        self.assertIn('标记价格偏差bps', row)
        self.assertIsNone(row['标记价格偏差bps'])
        self.assertIsNone(row['竞争量flat'])
        self.assertIsNone(row['盘口更新间隔P90秒'])
        self.clock.now += 59
        await self.health()
        self.assertEqual(sum(row['事件'] == '健康状态' for row in self.logger.rows), 1)
        self.clock.now += 1
        await self.health()
        self.assertEqual(sum(row['事件'] == '健康状态' for row in self.logger.rows), 2)

    async def check_paused_qualification(self, *, acknowledged, mode, reason):
        await self.publish()
        await self.bot.tick()
        self.clock.now = 110
        await self.publish()
        self.bot.settings = replace(self.bot.settings, mode='sandbox')
        self.bot.engine.settings = self.bot.settings
        self.bot.state.deadman_acked = acknowledged
        self.bot.state.modes[MARKET] = mode
        await self.bot.tick()
        self.clock.now = 130
        row = await self.health()
        self.assertAlmostEqual(row['有资格时长占比'], 1/3)
        self.assertEqual(row['守卫及停报原因'][reason]['时长秒'], 20)
        self.assertFalse(row['有资格'])
        self.assertFalse(self.alerts.summaries[-1]['判据'])

    async def test_missing_ack_cannot_inherit_previous_qualified_time(self):
        await self.check_paused_qualification(acknowledged=False, mode='RUNNING',
                                               reason='等待死人开关 ACK')

    async def test_sync_barrier_cannot_inherit_previous_qualified_time(self):
        await self.check_paused_qualification(acknowledged=True, mode='SYNCING',
                                               reason='账户同步屏障')


if __name__ == '__main__':
    unittest.main()
