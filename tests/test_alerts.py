"""In-memory tests for alerts and external heartbeats."""

import asyncio
import json
import logging
import stat
import unittest
from unittest.mock import patch

from pendle import alerts
from pendle.ondo_api import HttpResponse


class CredentialTests(unittest.TestCase):
    def test_environment_and_shared_secret_file(self):
        credentials = alerts.load_telegram_credentials(env={
            'TELEGRAM_BOT_TOKEN': 'token-value', 'TELEGRAM_CHAT_ID': 'chat-value'})
        self.assertEqual(credentials.bot_token, 'token-value')
        self.assertEqual(credentials.chat_id, 'chat-value')
        self.assertNotIn('token-value', repr(credentials))
        self.assertNotIn('chat-value', repr(credentials))
        content = json.dumps({'key': 'ondo-key', 'secret': 'ondo-secret',
                              'telegram_bot_token': 'token-value',
                              'telegram_chat_id': 'chat-value'})
        loaded = alerts.load_telegram_credentials(env={}, secret_file='fake',
            file_reader=lambda path: (stat.S_IFREG | 0o600, content))
        self.assertEqual(loaded, credentials)

    def test_file_requires_exact_permissions_regular_file_and_bounded_size(self):
        content = json.dumps({'telegram_bot_token': 'token', 'telegram_chat_id': 'chat'})
        for mode in (0o400, 0o640, 0o660, 0o777, stat.S_IFDIR | 0o600):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                alerts.load_telegram_credentials(env={}, secret_file='fake',
                    file_reader=lambda path: (mode, content))
        with self.assertRaises(ValueError):
            alerts.load_telegram_credentials(env={}, secret_file='fake',
                file_reader=lambda path: (stat.S_IFREG | 0o600, 'x' * 65537))

    def test_bad_credentials_never_appear_in_validation_errors(self):
        for env in ({}, {'TELEGRAM_BOT_TOKEN': 'sensitive', 'TELEGRAM_CHAT_ID': ''}):
            with self.subTest(env=env), self.assertRaises(ValueError) as failure:
                alerts.load_telegram_credentials(env=env)
            self.assertNotIn('sensitive', str(failure.exception))


class AlerterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = 0.0
        self.sent = []
        self.reply = HttpResponse(200, {'ok': True})

        async def http(method, url, *, json=None, timeout=10):
            self.sent.append((method, url, json, timeout))
            if isinstance(self.reply, Exception):
                raise self.reply
            return self.reply

        self.alerter = alerts.Alerter(alerts.TelegramCredentials('token-value', 'chat-value'),
            http=http, monotonic=lambda: self.now,
            healthcheck_url='https://health.invalid/secret')

    async def _drain_background(self):
        await asyncio.gather(*self.alerter._background_tasks)

    async def test_startup_self_test_checks_telegram_success(self):
        self.assertTrue(await self.alerter.self_test())
        method, url, payload, timeout = self.sent[-1]
        self.assertEqual(method, 'POST')
        self.assertEqual(url, 'https://api.telegram.org/bottoken-value/sendMessage')
        self.assertEqual(payload['chat_id'], 'chat-value')
        self.assertIn('自检', payload['text'])
        self.assertLessEqual(timeout, 10)
        for reply in (HttpResponse(200, {'ok': False}), HttpResponse(401, {'ok': True}),
                      HttpResponse(200, {}), RuntimeError('token-value chat-value')):
            self.reply = reply
            with self.assertLogs('pendle.alerts', logging.WARNING):
                self.assertFalse(await self.alerter.self_test())

    async def test_missing_credentials_or_transport_fails_closed_without_network(self):
        self.assertFalse(await alerts.Alerter().self_test())
        with self.assertLogs('pendle.alerts', logging.WARNING):
            self.assertFalse(await alerts.Alerter(
                alerts.TelegramCredentials('token-value', 'chat-value')).self_test())

    async def test_ordinary_alerts_merge_globally_for_thirty_minutes(self):
        self.assertTrue(await self.alerter.alert('守卫甲', key='guard-a'))
        await self._drain_background()
        self.now = 1
        self.assertFalse(await self.alerter.alert('守卫乙', key='guard-b'))
        self.now = 1799
        self.assertFalse(await self.alerter.alert('守卫丙', key='guard-c'))
        self.assertEqual(len(self.sent), 1)
        self.now = 1800
        self.assertTrue(await self.alerter.alert('守卫乙更新', key='guard-b'))
        await self._drain_background()
        self.assertEqual(len(self.sent), 2)
        self.assertIn('守卫乙更新', self.sent[-1][2]['text'])
        self.assertIn('守卫丙', self.sent[-1][2]['text'])

    async def test_urgent_alerts_bypass_ordinary_rate_limit(self):
        await self.alerter.alert('守卫')
        self.now = 1
        self.assertTrue(await self.alerter.alert('紧急熔断', urgent=True))
        self.assertEqual(len(self.sent), 2)

    async def test_urgent_http_does_not_wait_for_inflight_ordinary_alert(self):
        ordinary_started, release_ordinary, urgent_started = (
            asyncio.Event(), asyncio.Event(), asyncio.Event())
        sent = []

        async def http(method, url, *, json=None, timeout=10):
            sent.append(json['text'])
            if json['text'].startswith('做市告警'):
                ordinary_started.set()
                await release_ordinary.wait()
            else:
                urgent_started.set()
            return HttpResponse(200, {'ok': True})

        self.alerter._http = http
        ordinary = asyncio.create_task(self.alerter.alert('普通守卫'))
        await ordinary_started.wait()
        urgent = asyncio.create_task(self.alerter.alert('STOP_FAILED', urgent=True))
        try:
            await asyncio.wait_for(urgent_started.wait(), 0.1)
            self.assertEqual(sent[-1], '紧急告警：STOP_FAILED')
        finally:
            release_ordinary.set()
            await asyncio.gather(ordinary, urgent)

    async def test_flush_urgent_waits_for_http_after_alert_caller_is_cancelled(self):
        started, release, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def http(method, url, *, json=None, timeout=10):
            self.sent.append((method, url, json, timeout))
            started.set()
            await release.wait()
            completed.set()
            return HttpResponse(200, {'ok': True})

        self.alerter._http = http
        caller = asyncio.create_task(self.alerter.alert('熔断', urgent=True))
        await started.wait()
        caller.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        flush = asyncio.create_task(self.alerter.flush_urgent(timeout_s=0.1))
        try:
            await asyncio.sleep(0)
            self.assertFalse(flush.done())
            release.set()
            self.assertTrue(await flush)
            self.assertTrue(completed.is_set())
            self.assertEqual(len(self.sent), 1)
        finally:
            release.set()

    async def test_flush_urgent_timeout_keeps_http_alive_for_later_completion(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def http(method, url, *, json=None, timeout=10):
            started.set()
            await release.wait()
            self.sent.append(json['text'])
            return HttpResponse(200, {'ok': True})

        self.alerter._http = http
        caller = asyncio.create_task(self.alerter.alert('写锁失败', urgent=True))
        await started.wait()
        try:
            self.assertFalse(await self.alerter.flush_urgent(timeout_s=0.001))
            self.assertFalse(caller.done())
            release.set()
            self.assertTrue(await caller)
            self.assertEqual(self.sent, ['紧急告警：写锁失败'])
        finally:
            release.set()
            await caller

    async def test_urgent_timeout_and_exit_flush_share_one_ten_second_budget(self):
        release, started = asyncio.Event(), asyncio.Event()
        waits = []

        async def http(method, url, *, json=None, timeout=10):
            self.sent.append((method, url, json, timeout))
            started.set()
            await release.wait()
            return HttpResponse(200, {'ok': True})

        async def elapsed_wait(tasks, *, timeout):
            waits.append(timeout)
            await asyncio.sleep(0)
            self.now += timeout
            return set(), set(tasks)

        self.alerter._http = http
        try:
            with patch.object(alerts.asyncio, 'wait', side_effect=elapsed_wait):
                try:
                    result = await asyncio.wait_for(
                        self.alerter.alert('STOP_FAILED', urgent=True), 0.1)
                except TimeoutError:
                    self.fail('紧急告警没有独立的十秒等待上限')
                self.assertFalse(result)
                self.assertTrue(started.is_set())
                self.assertEqual(len(self.sent), 1)
                self.assertFalse(await self.alerter.flush_urgent(timeout_s=10))
                self.assertEqual(waits, [10, 0])
        finally:
            release.set()
            await asyncio.sleep(0)

    async def test_cancelled_urgent_caller_leaves_only_remaining_budget_for_flush(self):
        release, started = asyncio.Event(), asyncio.Event()

        async def http(method, url, *, json=None, timeout=10):
            started.set()
            await release.wait()
            return HttpResponse(200, {'ok': True})

        self.alerter._http = http
        caller = asyncio.create_task(self.alerter.alert('Watchdog 故障', urgent=True))
        await started.wait()
        caller.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        self.now += 4
        try:
            with patch.object(alerts.asyncio, 'wait', return_value=(set(), {object()})) as wait:
                self.assertFalse(await self.alerter.flush_urgent(timeout_s=10))
                self.assertEqual(wait.call_args.kwargs['timeout'], 6)
        finally:
            release.set()
            await asyncio.sleep(0)

    async def test_failure_logs_never_contain_credentials_or_exception_text(self):
        self.reply = RuntimeError('token-value chat-value sign=' + 'a' * 64)
        with self.assertLogs('pendle.alerts', logging.WARNING) as logs:
            self.assertTrue(await self.alerter.alert('故障'))
            await self._drain_background()
        text = '\n'.join(logs.output)
        for value in ('token-value', 'chat-value', 'a' * 64):
            self.assertNotIn(value, text)

    async def test_healthy_heartbeat_is_minutely_and_stale_quote_blocks_it(self):
        async def observe(**changes):
            values = dict(healthy=True, waiting=False, qualified=True, last_quote=0)
            values.update(changes)
            await self.alerter.observe(**values)
            await self._drain_background()

        await observe()
        self.now = 59
        await observe()
        self.assertEqual(sum(call[0] == 'GET' for call in self.sent), 1)
        self.now = 60
        await observe()
        self.assertEqual(sum(call[0] == 'GET' for call in self.sent), 2)
        self.now = 601
        await observe()
        self.assertEqual(sum(call[0] == 'GET' for call in self.sent), 2)
        self.assertIn('10 分钟', self.sent[-1][2]['text'])
        self.now = 602
        await observe(waiting=True)
        self.assertEqual(sum(call[0] == 'GET' for call in self.sent), 3)
        self.now = 662
        await observe(healthy=False, waiting=True)
        self.assertEqual(sum(call[0] == 'GET' for call in self.sent), 3)

    async def test_never_quoted_or_future_timestamp_cannot_claim_recent_quote(self):
        for last_quote in (None, 1000, float('nan'), float('inf')):
            with self.subTest(last_quote=last_quote):
                await self.alerter.observe(healthy=True, waiting=False, qualified=True,
                                           last_quote=last_quote)
        self.assertEqual(self.sent, [])

    async def test_explicit_waiting_suppresses_no_quote_alert(self):
        await self.alerter.observe(healthy=True, waiting=True, qualified=True, last_quote=None)
        self.now = 3600
        await self.alerter.observe(healthy=True, waiting=True, qualified=True, last_quote=None)
        await self._drain_background()
        messages = [call[2]['text'] for call in self.sent if call[0] == 'POST']
        self.assertFalse(any('10 分钟' in text for text in messages))

    async def test_no_successful_quote_alarms_after_ten_minutes_from_start(self):
        await self.alerter.observe(healthy=True, waiting=False, qualified=True, last_quote=None)
        self.now = 600
        await self.alerter.observe(healthy=True, waiting=False, qualified=True, last_quote=None)
        self.assertEqual(self.sent, [])
        self.now = 601
        await self.alerter.observe(healthy=True, waiting=False, qualified=True, last_quote=None)
        await self._drain_background()
        self.assertIn('10 分钟', self.sent[-1][2]['text'])

    async def test_qualification_ratio_requires_complete_thirty_minute_window(self):
        await self.alerter.observe(healthy=True, waiting=True, qualified=False, last_quote=None)
        self.now = 1799
        await self.alerter.observe(healthy=True, waiting=True, qualified=False, last_quote=None)
        self.assertFalse(any(call[0] == 'POST' for call in self.sent))
        self.now = 1800
        with self.assertLogs('pendle.alerts', logging.INFO) as logs:
            await self.alerter.observe(healthy=True, waiting=True, qualified=False, last_quote=None)
        await self._drain_background()
        messages = [call[2]['text'] for call in self.sent if call[0] == 'POST']
        self.assertEqual(messages, [])
        self.assertIn('30 分钟', logs.output[0])

    async def test_qualification_uses_time_weighting_and_fifty_percent_boundary(self):
        await self.alerter.observe(healthy=True, waiting=True, qualified=False, last_quote=None)
        self.now = 900
        await self.alerter.observe(healthy=True, waiting=True, qualified=True, last_quote=None)
        self.now = 1800
        await self.alerter.observe(healthy=True, waiting=True, qualified=True, last_quote=None)
        self.assertFalse(any(call[0] == 'POST' for call in self.sent))
        self.now = 3600
        await self.alerter.observe(healthy=True, waiting=True, qualified=True, last_quote=None)
        await self._drain_background()
        messages = [call[2]['text'] for call in self.sent if call[0] == 'POST']
        self.assertFalse(any('低于 50%' in text for text in messages))

    async def test_periodic_summary_includes_metrics_and_redacts_sensitive_values(self):
        summary = {'持仓': 3, '未实现盈亏': -2, '判据': '通过',
                   '外部心跳风险': 'token-value chat-value sign=' + 'a' * 64}
        await self.alerter.observe(healthy=True, waiting=True, qualified=True,
                                   last_quote=None, summary=summary)
        self.now = 14399
        await self.alerter.observe(healthy=True, waiting=True, qualified=True,
                                   last_quote=None, summary=summary)
        self.assertFalse(any(call[0] == 'POST' for call in self.sent))
        self.now = 14400
        await self.alerter.observe(healthy=True, waiting=True, qualified=True,
                                   last_quote=None, summary=summary)
        await self._drain_background()
        messages = [call[2]['text'] for call in self.sent if call[0] == 'POST']
        self.assertEqual(len(messages), 1)
        for text in ('做市摘要', '持仓', '盈亏', '有资格时长', '判据'):
            self.assertIn(text, messages[0])
        self.assertLessEqual(len(messages[0].splitlines()), 8)
        self.assertNotIn('{', messages[0])
        for secret in ('token-value', 'chat-value', 'a' * 64):
            self.assertNotIn(secret, messages[0])

    async def test_observe_flushes_merged_alert_without_new_event(self):
        await self.alerter.alert('告警甲')
        await self._drain_background()
        self.now = 1
        await self.alerter.alert('告警乙')
        self.now = 1800
        await self.alerter.observe(healthy=False, waiting=True, qualified=True, last_quote=None)
        await self._drain_background()
        self.assertEqual(len(self.sent), 2)
        self.assertIn('告警乙', self.sent[-1][2]['text'])

    async def test_time_weighted_qualification_ratio_below_fifty_percent_logs_only(self):
        await self.alerter.observe(healthy=True, waiting=True, qualified=False, last_quote=None)
        self.now = 1000
        await self.alerter.observe(healthy=True, waiting=True, qualified=True, last_quote=None)
        self.now = 1800
        with self.assertLogs('pendle.alerts', logging.INFO) as logs:
            await self.alerter.observe(healthy=True, waiting=True, qualified=True, last_quote=None)
        await self._drain_background()
        self.assertAlmostEqual(self.alerter.qualification_ratio, 800 / 1800)
        messages = [call[2]['text'] for call in self.sent if call[0] == 'POST']
        self.assertEqual(messages, [])
        self.assertIn('低于 50%', logs.output[0])

    async def test_failed_ordinary_alert_is_retained_and_attempts_are_rate_limited(self):
        self.reply = HttpResponse(500, {'ok': False})
        with self.assertLogs('pendle.alerts', logging.WARNING):
            self.assertTrue(await self.alerter.alert('守卫甲'))
            await self._drain_background()
        self.now = 1
        self.assertFalse(await self.alerter.alert('守卫乙'))
        self.assertEqual(len(self.sent), 1)
        self.reply = HttpResponse(200, {'ok': True})
        self.now = 1800
        await self.alerter.observe(healthy=False, waiting=True, qualified=True, last_quote=None)
        await self._drain_background()
        self.assertEqual(len(self.sent), 2)
        for text in ('守卫甲', '守卫乙'):
            self.assertIn(text, self.sent[-1][2]['text'])

    async def test_concurrent_alerts_keep_one_ordinary_send_per_window(self):
        results = await asyncio.gather(*(self.alerter.alert('故障' + str(i), key=str(i))
                                        for i in range(20)))
        self.assertEqual(sum(results), 1)
        self.assertEqual(len(self.sent), 1)
        self.now = 1800
        await self.alerter.observe(healthy=False, waiting=True, qualified=True, last_quote=None)
        await self._drain_background()
        self.assertEqual(len(self.sent), 2)
        for i in range(1, 20):
            self.assertIn('故障' + str(i), self.sent[-1][2]['text'])

    async def test_healthcheck_failures_are_redacted_and_not_retried_before_minute(self):
        self.reply = RuntimeError('token-value chat-value https://health.invalid/secret')
        with self.assertLogs('pendle.alerts', logging.WARNING) as logs:
            await self.alerter.observe(healthy=True, waiting=True, qualified=True, last_quote=None)
        self.now = 59
        await self.alerter.observe(healthy=True, waiting=True, qualified=True, last_quote=None)
        self.assertEqual(len(self.sent), 1)
        for secret in ('token-value', 'chat-value', 'https://health.invalid/secret'):
            self.assertNotIn(secret, '\n'.join(logs.output))

    async def test_cancellation_propagates_for_sigterm_shutdown(self):
        async def cancelled_http(*args, **kwargs):
            raise asyncio.CancelledError

        alerter = alerts.Alerter(alerts.TelegramCredentials('token', 'chat'), http=cancelled_http)
        with self.assertRaises(asyncio.CancelledError):
            await alerter.self_test()


if __name__ == '__main__':
    unittest.main()
