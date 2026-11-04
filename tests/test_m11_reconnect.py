"""M11 重连退避、故障日志和紧急告警使用纯内存连接验证。"""

import asyncio
import json
import logging
import unittest
from unittest.mock import patch

from pendle.alerts import Alerter, TelegramCredentials
from pendle.maker import Maker
from pendle.maker_config import MakerSettings
from pendle.ondo_api import Credentials
from test_maker_e2e import Clock, Exchange, Files


class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.rows = []

    def emit(self, record):
        self.rows.append((record.levelno, record.getMessage()))


class FailedSocket:
    def __init__(self, clock, duration=0, *, clean_end=False, close_error=None):
        self.clock, self.duration, self.clean_end = clock, duration, clean_end
        self.close_error = close_error
        self.closed = False

    async def send(self, message):
        pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        self.clock.now += self.duration
        if self.clean_end:
            raise StopAsyncIteration
        raise OSError('测试连接断开')

    async def close(self):
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


class ReconnectTests(unittest.IsolatedAsyncioTestCase):
    def make_bot(self, mode='dry-run'):
        clock, files = Clock(), Files()
        api = Exchange(clock, files)
        api.credentials = Credentials('private-key', 'private-secret')
        capture = Capture()
        logger = logging.Logger('m11-reconnect', level=logging.DEBUG)
        logger.addHandler(capture)
        sent = []

        async def http(method, url, **kwargs):
            sent.append(kwargs['json']['text'])
            return {'ok': True}

        alerter = Alerter(TelegramCredentials('private-bot', 'private-chat'),
            http=http, monotonic=clock, logger=logger)
        bot = Maker(MakerSettings(mode=mode, binance_symbol='BTCUSDT',
                    guards_calibrated=mode == 'live', sandbox_verified=mode == 'live',
                    healthcheck_url='https://private-health.invalid/check'),
            api=api, alerter=alerter, logger=logger, monotonic=clock,
            clock=clock.wall, files=files)

        async def guard(reason):
            return True

        bot.guard.trigger = guard
        return bot, clock, capture, sent

    async def run_failures(self, kind, count, *, durations=None, error=None,
                           mode='dry-run', clean_end=False, close_error=None):
        bot, clock, capture, sent = self.make_bot(mode)
        waits, sockets = [], []

        async def connect(url, **kwargs):
            if error is not None:
                raise error
            ws = FailedSocket(clock, durations[len(sockets)] if durations else 0,
                              clean_end=clean_end, close_error=close_error)
            sockets.append(ws)
            return ws

        async def sleep(delay):
            waits.append(delay)
            clock.now += delay
            if len(waits) >= count:
                bot.request_stop()
            await asyncio.sleep(0)

        bot.connect, bot.sleep = connect, sleep
        with patch('random.uniform', return_value=0):
            await (bot.binance_feed() if kind == 'binance' else bot.feed(kind == 'private'))
        return bot, waits, capture, sent, sockets

    async def test_each_connection_uses_exponential_backoff_capped_at_sixty(self):
        for kind in ('public', 'private', 'binance'):
            with self.subTest(kind=kind):
                _, waits, _, _, _ = await self.run_failures(kind, 8)
                self.assertEqual(waits, [3, 6, 12, 24, 48, 60, 60, 60])

    async def test_short_connections_keep_backoff_and_stable_sixty_seconds_resets(self):
        for kind in ('public', 'private', 'binance'):
            with self.subTest(kind=kind):
                _, waits, _, _, _ = await self.run_failures(kind, 5,
                    durations=[0, 59.999, 0, 60, 0])
                self.assertEqual(waits, [3, 6, 12, 3, 6])

    async def test_handshake_failures_log_429_type_message_source_count_and_wait(self):
        for kind, source in (('public', '公开'), ('private', '私有'), ('binance', '币安')):
            with self.subTest(kind=kind):
                _, waits, capture, _, _ = await self.run_failures(kind, 2,
                    error=RuntimeError('HTTP 429 Too Many Requests'))
                warnings = [json.loads(text) for level, text in capture.rows
                            if level == logging.WARNING]
                self.assertEqual(len(warnings), 2)
                self.assertEqual([row['来源'] for row in warnings], [source, source])
                self.assertEqual([row['连续失败次数'] for row in warnings], [1, 2])
                self.assertEqual([row['下次等待秒'] for row in warnings], waits)
                self.assertTrue(all(row['异常类型'] == 'RuntimeError' for row in warnings))
                self.assertTrue(all('HTTP 429' in row['异常消息'] for row in warnings))

    async def test_failure_logs_and_urgent_alerts_redact_all_configured_secrets(self):
        secret_error = RuntimeError('HTTP 429 private-key private-secret private-bot '
            'private-chat https://private-health.invalid/check sign=' + 'a' * 64)
        _, _, capture, sent, _ = await self.run_failures('private', 6,
            error=secret_error, mode='sandbox')
        warnings = [text for level, text in capture.rows if level == logging.WARNING]
        self.assertEqual(len(warnings), 6)
        urgent = [text for text in sent if text.startswith('紧急告警：')]
        self.assertEqual(len(urgent), 1)
        for text in warnings + urgent:
            self.assertIn('HTTP 429', text)
            for secret in ('private-key', 'private-secret', 'private-bot', 'private-chat',
                           'https://private-health.invalid/check', 'a' * 64):
                self.assertNotIn(secret, text)

    async def test_dry_run_never_sends_urgent_connection_alerts(self):
        _, _, _, sent, _ = await self.run_failures('private', 8,
            error=OSError('HTTP 429'), mode='dry-run')
        self.assertFalse([text for text in sent if text.startswith('紧急告警：')])

    async def test_clean_stream_end_is_a_logged_failure_for_every_source(self):
        for kind in ('public', 'private', 'binance'):
            with self.subTest(kind=kind):
                _, waits, capture, _, sockets = await self.run_failures(kind, 2,
                    clean_end=True)
                self.assertEqual(waits, [3, 6])
                self.assertEqual(len([text for level, text in capture.rows
                                      if level == logging.WARNING]), 2)
                self.assertTrue(all(ws.closed for ws in sockets))

    async def test_backoff_state_is_independent_between_all_three_connections(self):
        bot, _, _, _ = self.make_bot()
        waits = []

        async def connect(url, **kwargs):
            raise OSError('测试握手失败')

        async def sleep(delay):
            waits.append(delay)
            bot.request_stop()

        bot.connect, bot.sleep = connect, sleep
        with patch('random.uniform', return_value=0):
            for kind in ('public', 'private', 'binance') * 2:
                bot.stop_event.clear()
                await (bot.binance_feed() if kind == 'binance' else bot.feed(kind == 'private'))
        self.assertEqual(waits, [3, 3, 3, 6, 6, 6])

    async def test_simultaneous_failures_merge_into_one_urgent_alert(self):
        bot, _, _, sent = self.make_bot('sandbox')
        task_sources, waits = {}, {}
        barrier = asyncio.Event()

        async def connect(url, **kwargs):
            raise OSError('HTTP 429')

        async def sleep(delay):
            source = task_sources[asyncio.current_task()]
            waits[source] = waits.get(source, 0) + 1
            if all(waits.get(kind, 0) >= 7 for kind in ('公开', '私有', '币安')):
                bot.request_stop()
                barrier.set()
            if waits[source] >= 7:
                await barrier.wait()
            else:
                await asyncio.sleep(0)

        bot.connect, bot.sleep = connect, sleep
        with patch('random.uniform', return_value=0):
            tasks = [asyncio.create_task(bot.feed()), asyncio.create_task(bot.feed(True)),
                     asyncio.create_task(bot.binance_feed())]
            task_sources.update(zip(tasks, ('公开', '私有', '币安')))
            await asyncio.wait_for(asyncio.gather(*tasks), 2)
        urgent = [text for text in sent if text.startswith('紧急告警：')]
        self.assertEqual(len(urgent), 1)
        self.assertTrue(all(source in urgent[0] for source in ('公开', '私有', '币安')))

    async def test_urgent_starts_at_fifth_failure_and_rearms_after_stable_recovery(self):
        for mode in ('sandbox', 'live'):
            with self.subTest(mode=mode):
                bot, clock, _, _ = self.make_bot(mode)
                attempts, waits, urgent_at = [], [], []
                original_alert = bot.alerter.alert

                async def alert(reason, **kwargs):
                    if kwargs.get('urgent'):
                        urgent_at.append(len(attempts))
                    return await original_alert(reason, **kwargs)

                async def connect(url, **kwargs):
                    attempts.append(url)
                    return FailedSocket(clock, 60 if len(attempts) == 6 else 0)

                async def sleep(delay):
                    waits.append(delay)
                    clock.now += delay
                    if len(waits) == 10:
                        bot.request_stop()
                    await asyncio.sleep(0)

                bot.connect, bot.sleep, bot.alerter.alert = connect, sleep, alert
                with patch('random.uniform', return_value=0):
                    await bot.feed(True)
                self.assertEqual(urgent_at, [5, 10])
                self.assertEqual(waits, [3, 6, 12, 24, 48] * 2)

    async def test_disconnected_socket_references_and_private_login_are_cleared(self):
        for kind in ('public', 'private'):
            with self.subTest(kind=kind):
                bot, _, _, _ = self.make_bot()
                ws = FailedSocket(bot.monotonic)

                async def connect(url, **kwargs):
                    return ws

                async def sleep(delay):
                    self.assertIsNone(bot.private_ws if kind == 'private' else bot.public_ws)
                    if kind == 'private':
                        self.assertFalse(bot.private_logged_in)
                    bot.request_stop()

                bot.connect, bot.sleep = connect, sleep
                await bot.feed(kind == 'private')

    async def test_jitter_changes_wait_without_exceeding_sixty_seconds(self):
        for endpoint in ('low', 'high'):
            with self.subTest(endpoint=endpoint):
                bot, _, _, _ = self.make_bot()
                waits = []

                async def connect(url, **kwargs):
                    raise OSError('测试抖动')

                async def sleep(delay):
                    waits.append(delay)
                    if len(waits) == 7:
                        bot.request_stop()

                bot.connect, bot.sleep = connect, sleep
                with patch('random.uniform', side_effect=lambda low, high:
                           low if endpoint == 'low' else high):
                    await bot.feed()
                self.assertNotEqual(waits[0], 3)
                self.assertTrue(all(base - base * .2 <= delay <= 60
                    for base, delay in zip((3, 6, 12, 24, 48, 60, 60), waits)))

    async def test_backoff_cap_still_has_random_jitter(self):
        from pendle.maker_reconnect import ReconnectBackoff

        with patch('random.uniform', side_effect=lambda low, high: low):
            lower = ReconnectBackoff(failures=5).failed()
        with patch('random.uniform', side_effect=lambda low, high: high):
            upper = ReconnectBackoff(failures=5).failed()
        self.assertEqual(lower, 48)
        self.assertEqual(upper, 60)

    async def test_close_failures_retry_and_log_without_counting_connection_twice(self):
        for kind, source in (('public', '公开'), ('private', '私有'), ('binance', '币安')):
            with self.subTest(kind=kind):
                try:
                    _, waits, capture, sent, _ = await self.run_failures(kind, 6,
                        mode='sandbox', close_error=RuntimeError(
                            'HTTP 429 关闭失败 private-key sign=' + 'a' * 64))
                except RuntimeError:
                    self.fail('关闭异常使关键行情协程退出，未继续退避')
                self.assertEqual(waits, [3, 6, 12, 24, 48, 60])
                warnings = [json.loads(text) for level, text in capture.rows
                            if level == logging.WARNING]
                failures = [row for row in warnings if row['事件'] == '行情连接失败']
                closes = [row for row in warnings if row['事件'] == '行情连接关闭失败']
                self.assertEqual([row['连续失败次数'] for row in failures], list(range(1, 7)))
                self.assertEqual([row['连续失败次数'] for row in closes], list(range(1, 7)))
                self.assertEqual([row['下次等待秒'] for row in closes], waits)
                self.assertTrue(all(row['来源'] == source for row in warnings))
                self.assertTrue(all('HTTP 429 关闭失败' in row['异常消息'] for row in closes))
                self.assertTrue(all(row['异常类型'] == 'RuntimeError' for row in closes))
                self.assertEqual(len([text for text in sent if text.startswith('紧急告警：')]), 1)
                for row in closes:
                    self.assertNotIn('private-key', row['异常消息'])
                    self.assertNotIn('a' * 64, row['异常消息'])

    async def test_cancellation_survives_close_failures_and_never_retries(self):
        for kind in ('public', 'private', 'binance'):
            for cancelled_at in ('receive', 'close'):
                with self.subTest(kind=kind, cancelled_at=cancelled_at):
                    bot, clock, _, _ = self.make_bot()
                    waits = []

                    class CancelledSocket(FailedSocket):
                        async def __anext__(self):
                            if cancelled_at == 'receive':
                                raise asyncio.CancelledError
                            return await super().__anext__()

                    ws = CancelledSocket(clock, close_error=RuntimeError('取消时关闭失败')
                        if cancelled_at == 'receive' else asyncio.CancelledError())

                    async def connect(url, **kwargs):
                        return ws

                    async def sleep(delay):
                        waits.append(delay)

                    bot.connect, bot.sleep = connect, sleep
                    try:
                        await (bot.binance_feed() if kind == 'binance' else bot.feed(kind == 'private'))
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        self.fail('关闭异常覆盖了原始取消异常')
                    else:
                        self.fail('取消异常没有向调用方传播')
                    self.assertEqual(waits, [])


if __name__ == '__main__':
    unittest.main()
