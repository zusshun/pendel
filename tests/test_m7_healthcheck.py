"""M7：明确放弃外部心跳后仍须持续披露风险。"""

import asyncio
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path

from pendle.alerts import Alerter, TelegramCredentials
from pendle.maker import Maker, MarketRules
from pendle.maker_config import MakerSettings
from pendle.ondo_api import HttpResponse
from test_maker_e2e import Clock, Exchange, Files


WARNING = ('未配置外部心跳：服务器或网络整体故障时不会收到告警；'
           '死人开关会撤销挂单，但已有持仓无人处理')


class M7HealthcheckConfigTests(unittest.TestCase):
    def test_healthcheck_waiver_defaults_to_false_in_settings_and_example(self):
        self.assertIs(getattr(MakerSettings(), 'healthcheck_waived', None), False)
        example = Path(__file__).parents[1] / 'maker.example.toml'
        self.assertIs(MakerSettings.from_file(example).healthcheck_waived, False)
        self.assertIn('healthcheck_waived = false', example.read_text())

    def test_live_without_heartbeat_or_waiver_is_rejected(self):
        with self.assertRaisesRegex(ValueError, '心跳'):
            MakerSettings(mode='live', guards_calibrated=True, sandbox_verified=True)

    def test_live_toml_accepts_explicit_healthcheck_waiver(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'maker.toml'
            path.write_text('[maker]\nmode = "live"\nguards_calibrated = true\n'
                            'sandbox_verified = true\nhealthcheck_waived = true\n')
            try:
                settings = MakerSettings.from_file(path)
            except ValueError as exc:
                self.fail(f'明确放弃外部心跳的 live 配置应可加载：{exc}')
        self.assertEqual(settings.healthcheck_url, '')
        self.assertIs(settings.healthcheck_waived, True)

    def test_healthcheck_waiver_rejects_non_boolean_values(self):
        self.assertTrue('healthcheck_waived' in MakerSettings.__dataclass_fields__)
        for value in ('true', 'false', 1, 0, None):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, '布尔'):
                MakerSettings(healthcheck_waived=value)


class M7HealthcheckLiveTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock, self.files = Clock(), Files()
        self.sent, self.telegram_ok = [], True
        self.urgent_delay = 0

    def make_bot(self, **kwargs):
        settings = MakerSettings(mode='live', guards_calibrated=True,
                                 sandbox_verified=True, **kwargs)

        async def http(method, url, *, json=None, timeout=10):
            self.sent.append((method, json, self.bot.started))
            if json and json['text'].startswith('紧急告警：'):
                self.clock.now += self.urgent_delay
            return HttpResponse(200, {'ok': self.telegram_ok})

        self.alerter = Alerter(TelegramCredentials('dummy-token', 'dummy-chat'),
                               http=http, monotonic=self.clock,
                               healthcheck_url=settings.healthcheck_url)
        self.bot = Maker(settings, api=Exchange(self.clock, self.files), alerter=self.alerter,
                         files=self.files, rules=MarketRules(D('.01'), D('.001')),
                         monotonic=self.clock, clock=self.clock.wall, sleep=self.clock.sleep)
        self.addCleanup(self.bot.instance.release)
        return self.bot

    def messages(self):
        return [payload['text'] for method, payload, _ in self.sent if method == 'POST']

    async def health_once(self):
        async def finish_iteration(delay):
            self.bot.stop_event.set()

        self.bot.sleep = finish_iteration
        self.bot.stop_event.clear()
        await self.bot.monitor_loop()
        self.bot.stop_event.clear()
        await asyncio.sleep(0)

    async def test_live_startup_rejects_missing_heartbeat_and_waiver(self):
        bot = self.make_bot(_for_emergency=True)
        with self.assertRaisesRegex(ValueError, '心跳'):
            await bot.startup(live=True)
        self.assertFalse(bot.started)
        self.assertFalse(self.sent)
        self.assertFalse(bot.api.calls)

    async def test_live_waiver_warns_via_urgent_after_successful_startup(self):
        bot = self.make_bot(healthcheck_waived=True)
        try:
            await bot.startup(live=True)
        except ValueError as exc:
            self.fail(f'明确放弃外部心跳时应可启动 live：{exc}')
        self.assertTrue(bot.started)
        warnings = [(payload['text'], started) for method, payload, started in self.sent
                    if method == 'POST' and WARNING in payload['text']]
        self.assertEqual(warnings, [('紧急告警：'+WARNING, True)])

    async def test_failed_live_startup_does_not_send_waiver_warning(self):
        bot = self.make_bot(healthcheck_waived=True)
        self.telegram_ok = False
        with self.assertLogs('pendle.alerts', level='WARNING'), \
                self.assertRaisesRegex(ValueError, 'Telegram'):
            await bot.startup(live=True)
        self.assertFalse(bot.started)
        self.assertFalse(any(WARNING in text for text in self.messages()))

    async def test_slow_waiver_warning_preserves_startup_health_and_ack_grace(self):
        bot = self.make_bot(healthcheck_waived=True, deadman_ack_timeout_s=1)
        self.urgent_delay = 10
        await bot.startup(live=True)
        self.assertEqual(bot.ack_wait_started, self.clock(),
                         '启动告警耗时不能占用连接任务的 ACK 宽限')
        self.assertTrue(all(seen == self.clock() for seen in bot.watchdog.last_seen.values()))

    async def test_each_hourly_summary_repeats_healthcheck_waiver_warning(self):
        self.make_bot(healthcheck_waived=True)
        await self.health_once()
        for _ in range(2):
            self.clock.now += 3600
            await self.health_once()
        summaries = [text for text in self.messages() if text.startswith('每小时摘要')]
        self.assertEqual(len(summaries), 2)
        for text in summaries:
            self.assertIn(WARNING, text)

    async def test_hourly_waiver_warning_survives_long_metrics_truncation(self):
        bot = self.make_bot(healthcheck_waived=True)
        reason = '停报原因' * 1500
        bot.pause_counts[reason], bot.pause_seconds[reason] = 1, 1
        await self.health_once()
        self.clock.now += 3600
        await self.health_once()
        summaries = [text for text in self.messages() if text.startswith('每小时摘要')]
        self.assertEqual(len(summaries), 1)
        self.assertTrue(WARNING in summaries[0], '摘要指标较长时也必须保留完整的心跳风险提醒')

    async def test_configured_heartbeat_needs_no_waiver_warning(self):
        for waived in (False, True):
            with self.subTest(waived=waived):
                bot = self.make_bot(healthcheck_url='https://heartbeat.invalid/check',
                                    healthcheck_waived=waived)
                await bot.startup(live=True)
                await self.health_once()
                self.clock.now += 3600
                await self.health_once()
                self.assertFalse(any(WARNING in text for text in self.messages()))
                bot.instance.release()
