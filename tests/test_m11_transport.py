"""Verify failure diagnostics through the real WS adapter with in-memory substitutes for all network dependencies."""

import json
import logging
import traceback
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from pendle import maker_transport as transport
from pendle.alerts import Alerter, TelegramCredentials
from pendle.maker import Maker
from pendle.maker_config import MakerSettings
from pendle.ondo_api import Credentials
from test_maker_e2e import Clock, Exchange, Files


class InvalidStatus(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.response = SimpleNamespace(status_code=429)


class InvalidStatusCode(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.status_code = 429


class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.rows = []

    def emit(self, record):
        self.rows.append((record.levelno, record.getMessage()))


class WebSocketDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def log_adapter_failure(self, error, source='私有'):
        clock, files, capture = Clock(), Files(), Capture()
        api = Exchange(clock, files)
        api.credentials = Credentials('private-key', 'private-secret')
        logger = logging.Logger('m11-transport', level=logging.DEBUG)
        logger.addHandler(capture)

        async def http(*args, **kwargs):
            return {'ok': True}

        alerter = Alerter(TelegramCredentials('private-bot', 'private-chat'),
                          http=http, monotonic=clock, logger=logger)
        bot = Maker(MakerSettings(mode='dry-run', binance_symbol='BTCUSDT',
                    healthcheck_url='https://private-health.invalid/check'),
                    api=api, alerter=alerter, files=files, logger=logger,
                    monotonic=clock, connect=transport.websocket_connect)

        async def failed_connect(*args, **kwargs):
            raise error

        async def stop_after_wait(delay):
            bot.request_stop()

        bot.sleep = stop_after_wait
        with patch.dict('sys.modules', {'websockets': SimpleNamespace(connect=failed_connect)}):
            await (bot.binance_feed() if source == '币安' else bot.feed(source == '私有'))
        return [json.loads(text) for level, text in capture.rows if level == logging.WARNING]

    async def test_actual_ws_adapter_keeps_429_type_and_redacts_diagnostic_message(self):
        secrets = ('private-key', 'private-secret', 'private-bot', 'private-chat',
                   'https://private-health.invalid/check', 'a' * 64)
        message = ('server rejected WebSocket connection: HTTP 429 Too Many Requests ' +
                   ' '.join(secrets[:-1]) + ' sign=' + secrets[-1])
        for source in ('公开', '私有', '币安'):
            with self.subTest(source=source):
                rows = await self.log_adapter_failure(InvalidStatus(message), source)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]['来源'], source)
                self.assertEqual(rows[0]['异常类型'], 'InvalidStatus')
                self.assertIn('HTTP 429', rows[0]['异常消息'])
                self.assertIn('Too Many Requests', rows[0]['异常消息'])
                for secret in secrets:
                    self.assertNotIn(secret, rows[0]['异常消息'])

    async def test_ws_adapter_preserves_structured_429_for_modern_and_legacy_errors(self):
        for error in (InvalidStatus('服务端拒绝握手'), InvalidStatusCode('服务端拒绝握手')):
            with self.subTest(error=type(error).__name__):
                rows = await self.log_adapter_failure(error)
                self.assertEqual(rows[0]['异常类型'], type(error).__name__)
                self.assertIn('HTTP 429', rows[0]['异常消息'])
                self.assertIn('服务端拒绝握手', rows[0]['异常消息'])

    async def test_ws_adapter_preserves_non_http_failure_reason_in_redacted_log(self):
        rows = await self.log_adapter_failure(OSError('TLS 握手失败 private-secret'))
        self.assertEqual(rows[0]['异常类型'], 'OSError')
        self.assertIn('TLS 握手失败', rows[0]['异常消息'])
        self.assertNotIn('private-secret', rows[0]['异常消息'])

    async def test_ws_network_error_public_text_and_traceback_never_expose_original(self):
        async def failed_connect(*args, **kwargs):
            raise InvalidStatus('HTTP 429 sensitive https://private.invalid/ws sign=' + 'a' * 64)

        url = 'wss://private.invalid/ws'
        with patch.dict('sys.modules', {'websockets': SimpleNamespace(connect=failed_connect)}):
            try:
                await transport.websocket_connect(url)
            except transport.NetworkError as error:
                rendered = str(error) + repr(error) + ''.join(traceback.format_exception(error))
                self.assertIn('WebSocket 连接失败', str(error))
                self.assertIsNone(error.__cause__)
                for secret in ('sensitive', 'private.invalid', 'a' * 64):
                    self.assertNotIn(secret, rendered)
            else:
                self.fail('握手错误必须转换为安全的 NetworkError')


if __name__ == '__main__':
    unittest.main()
