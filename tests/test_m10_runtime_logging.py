"""M10 regressions for runtime failures, market data log filtering, and configuration calibration using in-memory dependencies."""

import asyncio
import contextlib
import io
import json
import logging
import tempfile
import unittest
from datetime import datetime
from decimal import Decimal as D
from pathlib import Path
from zoneinfo import ZoneInfo

from pendle.alerts import Alerter, TelegramCredentials
from pendle.maker import Maker, MarketRules
from pendle.maker_config import MakerSettings
from pendle.maker_transport import build_maker
from pendle.ondo_api import Credentials
from test_maker import MARKET
from test_maker_e2e import Clock, Exchange, Files


class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.rows = []

    def emit(self, record):
        self.rows.append((record.levelno, record.getMessage()))


class RuntimeLoggingTests(unittest.IsolatedAsyncioTestCase):
    def make_bot(self, mode='dry-run'):
        clock, files = Clock(), Files()
        exchange = Exchange(clock, files)
        exchange.credentials = Credentials('private-key', 'private-secret')
        sent = []

        async def http(method, url, **kwargs):
            sent.append(kwargs['json']['text'])
            return {'ok': True}

        capture = Capture()
        logger = logging.Logger('m10-runtime', level=logging.DEBUG)
        logger.addHandler(capture)
        alerter = Alerter(TelegramCredentials('private-bot', 'private-chat'),
            http=http, logger=logger, monotonic=clock)
        settings = MakerSettings(mode=mode, guards_calibrated=mode == 'live',
            sandbox_verified=mode == 'live', healthcheck_url='https://example.invalid/check')
        bot = Maker(settings, api=exchange, alerter=alerter, files=files,
            rules=MarketRules(D('.01'), D('.001')), logger=logger,
            monotonic=clock, clock=clock.wall, sleep=clock.sleep)
        return bot, capture, sent, exchange

    def fail_runtime(self, bot):
        async def blocked(*args, **kwargs):
            await asyncio.Event().wait()

        async def fail():
            raise RuntimeError('行情处理异常 private-key private-secret private-bot '
                               'private-chat sign=' + 'a' * 64)

        bot.feed = bot.monitor_loop = bot.reconcile_loop = blocked
        bot.order_loop = bot.watchdog_loop = blocked
        bot.strategy_loop = fail

    def assert_redacted_failure(self, capture, stderr):
        errors = [text for level, text in capture.rows if level == logging.ERROR]
        self.assertTrue(any(text.startswith('运行失败：行情处理异常') for text in errors), errors)
        for destination in ('\n'.join(errors), stderr):
            self.assertIn('运行失败：行情处理异常', destination)
            self.assertNotIn('启动失败', destination)
            for secret in ('private-key', 'private-secret', 'private-bot', 'private-chat', 'a' * 64):
                self.assertNotIn(secret, destination)

    async def test_runtime_failure_after_startup_is_redacted_and_dry_run_only_logs(self):
        bot, capture, sent, exchange = self.make_bot()
        self.fail_runtime(bot)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = await bot.run()
        self.assertEqual(code, 2)
        self.assert_redacted_failure(capture, stderr.getvalue())
        self.assertEqual(sent, [])
        self.assertEqual(exchange.calls, [])

    async def test_runtime_failure_halts_and_sends_urgent_alert_in_sandbox_and_live(self):
        for mode in ('sandbox', 'live'):
            with self.subTest(mode=mode):
                bot, capture, sent, exchange = self.make_bot(mode)
                self.fail_runtime(bot)
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    code = await bot.run(live=mode == 'live')
                self.assertEqual(code, 76)
                self.assert_redacted_failure(capture, stderr.getvalue())
                urgent = [text for text in sent if text.startswith(f'[{MARKET}] 紧急告警：')]
                self.assertTrue(any('运行失败：行情处理异常' in text for text in urgent), urgent)
                self.assertNotIn('private-key', '\n'.join(urgent))
                self.assertNotIn('private-secret', '\n'.join(urgent))
                self.assertTrue(any(row[0] == 'cancel_all' for row in exchange.calls))

    async def test_startup_failure_keeps_startup_label(self):
        bot, capture, sent, _ = self.make_bot()

        async def fail_startup(**kwargs):
            raise ValueError('配置无效 private-key')

        bot.startup = fail_startup
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = await bot.run()
        self.assertEqual(code, 2)
        self.assertTrue(any(text.startswith('启动失败：配置无效') for _, text in capture.rows))
        self.assertNotIn('private-key', stderr.getvalue())
        self.assertEqual(sent, [])

    async def test_runtime_failure_after_existing_halt_still_sends_urgent_reason(self):
        bot, capture, sent, _ = self.make_bot('sandbox')
        self.fail_runtime(bot)
        fail = bot.strategy_loop

        async def fail_after_halt():
            await bot.trip_halt('先前持仓风险已熔断')
            await fail()

        bot.strategy_loop = fail_after_halt
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = await bot.run()
        self.assertEqual(code, 76)
        self.assert_redacted_failure(capture, stderr.getvalue())
        self.assertTrue(any(text.startswith(f'[{MARKET}] 紧急告警：') and '运行失败：行情处理异常' in text
                            for text in sent), sent)

    async def test_frequent_public_events_use_debug_and_health_stays_info(self):
        bot, capture, _, _ = self.make_bot()
        source_time = datetime.fromtimestamp(bot.clock(), ZoneInfo('UTC')).isoformat()
        await bot.on_public({'channel': bot.settings.mark_channel, 'timestamp': source_time,
            'data': [{'market': MARKET, 'markPrice': '100', 'inPriceDiscovery': False,
                      'lastUpdatedTime': source_time}]})
        await bot.on_public({'channel': 'tradesPerps', 'data': [{
            'market': MARKET, 'price': '100', 'size': '.1', 'aggressor_side': 'buy'}]})
        bot.log({'事件': '健康状态'})
        self.assertEqual([(level, json.loads(text)['事件']) for level, text in capture.rows],
            [(logging.DEBUG, '标记价格'), (logging.DEBUG, '公开成交'), (logging.INFO, '健康状态')])


class LoggingSettingsTests(unittest.TestCase):
    def test_log_level_defaults_to_info_and_toml_configures_debug(self):
        self.assertEqual(MakerSettings().log_level, 'INFO')
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'maker.toml'
            config.write_text('[maker]\nlog_level="debug"\n', encoding='utf-8')
            self.assertEqual(MakerSettings.from_file(config).log_level, 'DEBUG')

    def test_log_level_rejects_unknown_values(self):
        for value in ('VERBOSE', '', 10, None, True):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'log_level'):
                MakerSettings(log_level=value)

    def test_default_builder_honors_log_level(self):
        for level in ('INFO', 'DEBUG'):
            with self.subTest(level=level), tempfile.TemporaryDirectory() as directory:
                settings = MakerSettings(data_dir=directory, account='m10-log-' + level,
                                         log_level=level)
                with contextlib.redirect_stderr(io.StringIO()):
                    bot = build_maker(settings, env={})
                    bot.log({'事件': '标记价格'}, level=logging.DEBUG)
                    bot.log({'事件': '健康状态'})
                try:
                    text = (Path(directory) / 'maker.log').read_text(encoding='utf-8')
                    self.assertEqual('标记价格' in text, level == 'DEBUG')
                    self.assertIn('INFO', text)
                    self.assertIn('健康状态', text)
                finally:
                    for handler in tuple(bot.logger.handlers):
                        bot.logger.removeHandler(handler)
                        handler.close()

    def test_example_book_age_uses_server_calibration(self):
        path = Path(__file__).parents[1] / 'maker.example.toml'
        self.assertEqual(MakerSettings.from_file(path).max_book_age_s, 5.0)
        self.assertIn('IBM order book update intervals observed on the server: P90 0.26 seconds, maximum 3.2 seconds (weekday, 2026-11-04).',
                      path.read_text(encoding='utf-8'))
