"""M26 notification regressions use virtual time and an in-memory HTTP transport."""

import asyncio
import logging
import unittest
from dataclasses import replace

from pendle.alerts import Alerter, TelegramCredentials
from pendle.maker import HEALTHCHECK_WAIVER_WARNING
from pendle.maker_config import MakerSettings
from pendle.maker_transport import build_maker
from pendle.ondo_api import HttpResponse


class NotificationFrequencyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = 0.0
        self.sent = []
        self.alerter = self.make_alerter()

    def make_alerter(self, **options):
        async def http(method, url, *, json=None, timeout=10):
            self.sent.append(json['text'])
            return HttpResponse(200, {'ok': True})

        return Alerter(TelegramCredentials('private-token', 'private-chat'),
                       http=http, monotonic=lambda: self.now, **options)

    async def drain(self):
        await asyncio.gather(*self.alerter._background_tasks)

    async def observe(self, *, qualified=True, waiting=True, summary=None):
        await self.alerter.observe(healthy=False, waiting=waiting,
            qualified=qualified, last_quote=None, summary=summary)
        await self.drain()

    async def test_gate_flips_log_at_info_without_telegram_or_pending_alerts(self):
        with self.assertLogs('pendle.alerts', logging.INFO) as records:
            for reason in ('报价判据通过', '报价判据不通过：竞争量超过判据', '报价判据通过'):
                await self.alerter.alert(reason, key='判据变化')
                self.now += 301
                await self.drain()
        self.assertEqual(self.sent, [])
        self.assertEqual(self.alerter._pending, {})
        self.assertEqual(len(records.records), 3)
        self.assertTrue(all(record.levelno == logging.INFO for record in records.records))

    async def test_low_qualification_logs_at_info_without_telegram(self):
        await self.observe(qualified=False)
        self.now = 1799
        await self.observe(qualified=False)
        with self.assertNoLogs('pendle.alerts', logging.INFO):
            await self.observe(qualified=False)
        self.now = 1800
        with self.assertLogs('pendle.alerts', logging.INFO) as records:
            await self.observe(qualified=False)
        self.assertIn('低于 50%', records.output[0])
        self.assertEqual(self.sent, [])
        self.assertEqual(self.alerter._pending, {})

    async def test_two_ordinary_alerts_within_thirty_minutes_send_one_batch(self):
        self.assertTrue(await self.alerter.alert('守卫甲', key='guard-a'))
        await self.drain()
        self.now = 300
        self.assertFalse(await self.alerter.alert('守卫乙', key='guard-b'))
        await self.drain()
        self.now = 1799
        await self.observe()
        self.assertEqual(len(self.sent), 1)
        self.now = 1800
        await self.observe()
        self.assertEqual(len(self.sent), 2)
        self.assertIn('守卫乙', self.sent[-1])

    async def test_custom_ordinary_interval_preserves_merge_by_key(self):
        self.alerter = self.make_alerter(alert_min_interval_s=42)
        await self.alerter.alert('首次告警')
        await self.drain()
        self.now = 1
        await self.alerter.alert('旧内容', key='guard')
        self.now = 41.9
        await self.alerter.alert('最新内容', key='guard')
        await self.observe()
        self.assertEqual(len(self.sent), 1)
        self.now = 42
        await self.observe()
        self.assertEqual(len(self.sent), 2)
        self.assertIn('最新内容', self.sent[-1])
        self.assertNotIn('旧内容', self.sent[-1])

    async def test_urgent_alerts_remain_immediate_and_do_not_extend_batch_interval(self):
        await self.alerter.alert('普通告警')
        await self.drain()
        self.now = 1
        for reason in ('熔断', 'STOP_FAILED', '紧急 IOC', '窗口结束仍有持仓',
                       '熔断锁写入失败', '运行失败', HEALTHCHECK_WAIVER_WARNING):
            previous = len(self.sent)
            self.assertTrue(await self.alerter.alert(reason, urgent=True))
            self.assertEqual(len(self.sent), previous + 1)
            self.assertEqual(self.sent[-1], '紧急告警：' + reason)
        await self.alerter.alert('待合并告警')
        self.now = 1800
        await self.observe()
        self.assertIn('待合并告警', self.sent[-1])

    async def test_no_successful_quote_trigger_stays_ten_minutes_but_uses_batch(self):
        await self.observe(waiting=False)
        await self.alerter.alert('普通告警')
        await self.drain()
        self.now = 600
        await self.observe(waiting=False)
        self.assertNotIn('no-successful-quote', self.alerter._pending)
        self.now = 601
        await self.observe(waiting=False)
        self.assertEqual(len(self.sent), 1)
        self.assertIn('no-successful-quote', self.alerter._pending)
        self.now = 1800
        await self.observe(waiting=False)
        self.assertEqual(len(self.sent), 2)
        self.assertIn('10 分钟没有成功报价', self.sent[-1])

    async def test_summary_defaults_to_four_hours_and_respects_custom_interval(self):
        for interval, options in ((14400, {}), (90, {'summary_interval_s': 90})):
            with self.subTest(interval=interval):
                self.sent.clear()
                self.now = 0
                self.alerter = self.make_alerter(**options)
                await self.observe()
                for now in (interval / 2, interval - .1):
                    self.now = now
                    await self.observe()
                    self.assertEqual(self.sent, [])
                self.now = interval
                await self.observe()
                self.assertEqual(len(self.sent), 1)
                self.now = 2 * interval - .1
                await self.observe()
                self.assertEqual(len(self.sent), 1)
                self.now = 2 * interval
                await self.observe()
                self.assertEqual(len(self.sent), 2)

    async def test_summary_is_short_chinese_text_with_period_metrics_and_event_counts(self):
        self.alerter = self.make_alerter(summary_interval_s=100)
        baseline = {'有资格时长秒': 200, '观测时长秒': 500, '成交数': 7,
                    '守卫及停报原因': {'旧原因': {'时长秒': 9999}}}
        await self.observe(summary=baseline)
        with self.assertLogs('pendle.alerts', logging.INFO):
            await self.alerter.alert('报价判据通过', key='判据变化')
            await self.alerter.alert('报价判据不通过：竞争量超过判据', key='判据变化')
            await self.alerter.alert('最近 30 分钟有资格挂单时长低于 50%',
                                     key='low-qualification')
        metrics = {**baseline, '持仓': '2.5', '未实现盈亏': '-1.25',
                   '窗口已实现盈亏': '8.75', '窗口总盈亏': '7.50',
                   '有资格时长秒': 240, '观测时长秒': 600, '成交数': 10,
                   '竞争量权重': 'linear', '竞争量flat': '2500', '竞争量linear': '1500',
                   '判据': False, '当前判据': True,
                   '外部心跳风险': HEALTHCHECK_WAIVER_WARNING,
                   '守卫及停报原因': {'旧原因': {'时长秒': 9999},
                       '竞争过高': {'时长秒': 30}, '盘口过期': {'时长秒': 20},
                       '同步等待': {'时长秒': 10}, '微小停顿': {'时长秒': 1}}}
        self.now = 100
        await self.observe(summary=metrics)
        text = self.sent[-1]
        self.assertLessEqual(len(text.splitlines()), 8)
        self.assertNotIn('{', text)
        self.assertNotIn('}', text)
        for value in ('持仓：2.5', '未实现盈亏：-1.25', '窗口已实现盈亏：8.75',
                      '窗口总盈亏：7.50', '40.0%', '判据变化：2', '低资格观测：1',
                      '竞争过高 30秒', '盘口过期 20秒', '同步等待 10秒',
                      '本期成交：3', '1500', '当前判据：通过', HEALTHCHECK_WAIVER_WARNING):
            self.assertIn(value, text)
        self.assertNotIn('旧原因', text)
        self.assertNotIn('微小停顿', text)
        self.assertLess(text.index('竞争过高'), text.index('盘口过期'))
        self.assertLess(text.index('盘口过期'), text.index('同步等待'))
        self.now = 200
        await self.observe(summary={**metrics, '有资格时长秒': 330, '观测时长秒': 700})
        text = self.sent[-1]
        for value in ('90.0%', '本期成交：0', '判据变化：0', '低资格观测：0', '停报前三：无'):
            self.assertIn(value, text)

    async def test_summary_uses_time_weighted_period_ratio_without_maker_metrics(self):
        self.alerter = self.make_alerter(summary_interval_s=100)
        await self.observe(qualified=False)
        self.now = 75
        await self.observe(qualified=True)
        self.now = 100
        await self.observe(qualified=True)
        self.assertIn('25.0%', self.sent[-1])
        self.now = 200
        await self.observe(qualified=True)
        self.assertIn('100.0%', self.sent[-1])

    async def test_summary_sanitizes_newlines_and_credentials_in_selected_fields(self):
        self.alerter = self.make_alerter(summary_interval_s=10)
        await self.observe()
        self.now = 10
        await self.observe(summary={'持仓': 'private-token\nprivate-chat',
            '守卫及停报原因': {'异常\nsign=' + 'a' * 64: {'时长秒': 10}}})
        text = self.sent[-1]
        self.assertLessEqual(len(text.splitlines()), 8)
        for secret in ('private-token', 'private-chat', 'a' * 64):
            self.assertNotIn(secret, text)

    async def test_summary_long_stop_reasons_preserve_metrics_and_full_waiver_line(self):
        self.alerter = self.make_alerter(summary_interval_s=10)
        await self.observe()
        self.now = 10
        await self.observe(summary={
            '持仓': '1', '未实现盈亏': '2', '窗口已实现盈亏': '3', '窗口总盈亏': '5',
            '竞争量flat': '1500', '当前判据': False,
            '守卫及停报原因': {'停报原因' * 1500: {'时长秒': 10}},
            '外部心跳风险': HEALTHCHECK_WAIVER_WARNING})
        text = self.sent[-1]
        self.assertLess(len(text), 4096)
        self.assertLessEqual(len(text.splitlines()), 8)
        self.assertIn('竞争量（flat）：1500', text)
        self.assertIn('当前判据：不通过', text)
        self.assertEqual(text.splitlines()[-1], HEALTHCHECK_WAIVER_WARNING)


class NotificationWiringTests(unittest.TestCase):
    def test_build_maker_passes_configured_notification_intervals(self):
        class Factory:
            def __init__(self, settings, **dependencies):
                self.alerter = dependencies['alerter']

        settings = replace(MakerSettings(), alert_min_interval_s=123, summary_interval_s=456)
        bot = build_maker(settings, env={}, maker_factory=Factory)
        self.assertEqual(bot.alerter.alert_min_interval_s, 123)
        self.assertEqual(bot.alerter.summary_interval_s, 456)

    def test_build_maker_uses_runtime_logger_for_log_only_notifications(self):
        class Factory:
            def __init__(self, settings, **dependencies):
                self.alerter = dependencies['alerter']

        logger = logging.Logger('m26-runtime')
        bot = build_maker(MakerSettings(), env={}, logger=logger, maker_factory=Factory)
        self.assertIs(bot.alerter.logger, logger)

    def test_build_maker_preserves_explicitly_injected_alerter_logger(self):
        class Factory:
            def __init__(self, settings, **dependencies):
                self.alerter = dependencies['alerter']

        custom_logger = logging.Logger('m26-custom-alerts')
        alerter = Alerter(logger=custom_logger)
        bot = build_maker(MakerSettings(), env={}, alerter=alerter,
                          logger=logging.Logger('m26-runtime'), maker_factory=Factory)
        self.assertIs(bot.alerter.logger, custom_logger)
