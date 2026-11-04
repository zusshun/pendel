"""M4 普通告警后台发送的纯内存回归测试。"""

import asyncio
import logging
import sys
import unittest

from pendle.alerts import Alerter, TelegramCredentials
from pendle.ondo_api import HttpResponse


def _forbid_network(event, args):
    if event in {'socket.connect', 'socket.connect_ex', 'socket.bind', 'socket.getaddrinfo'}:
        raise AssertionError('M4 测试禁止联网及绑定端口')


sys.addaudithook(_forbid_network)


class OrdinaryAlertQueueTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = 0
        self.sent = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = asyncio.Event()

        async def http(method, url, *, json=None, timeout=10):
            self.sent.append(json['text'])
            if json['text'].startswith(('做市告警', '每小时摘要')):
                self.started.set()
                await self.release.wait()
                self.finished.set()
            return HttpResponse(200, {'ok': True})

        self.alerter = Alerter(TelegramCredentials('token', 'chat'),
                               http=http, monotonic=lambda: self.now)

    async def test_ordinary_alert_returns_while_its_http_is_blocked(self):
        caller = asyncio.create_task(self.alerter.alert('必须先撤单'))
        try:
            await self.started.wait()
            self.assertTrue(caller.done(), '普通告警等待了 HTTP，阻塞策略关键路径')
            self.assertTrue(caller.result())
            self.assertFalse(self.finished.is_set())
        finally:
            self.release.set()
            await caller
            await self.finished.wait()

    async def test_inflight_batch_does_not_block_or_drop_new_keyed_messages(self):
        first = asyncio.create_task(self.alerter.alert('旧判据', key='criterion'))
        second = None
        try:
            await self.started.wait()
            second = asyncio.create_task(self.alerter.alert('新判据', key='criterion'))
            await asyncio.sleep(0)
            self.assertTrue(second.done(), '队列入队等待了此前 HTTP')
            self.assertFalse(second.result())
            self.release.set()
            await self.finished.wait()
            await asyncio.sleep(0)
            self.now = 300
            await self.alerter.alert('新守卫', key='guard')
            await asyncio.sleep(0)
            self.assertEqual(len(self.sent), 2)
            self.assertIn('新判据', self.sent[-1])
            self.assertIn('新守卫', self.sent[-1])
            self.assertNotIn('旧判据', self.sent[-1])
        finally:
            self.release.set()
            await asyncio.gather(*(task for task in (first, second) if task is not None))

    async def test_queued_ordinary_http_does_not_block_urgent_delivery(self):
        caller = asyncio.create_task(self.alerter.alert('普通守卫'))
        try:
            await self.started.wait()
            self.assertTrue(await self.alerter.alert('STOP_FAILED', urgent=True))
            self.assertEqual(self.sent[-1], '紧急告警：STOP_FAILED')
            self.assertFalse(self.finished.is_set())
        finally:
            self.release.set()
            await caller
            await self.finished.wait()

    async def test_observe_ordinary_alerts_return_before_http_finishes(self):
        await self.alerter.observe(healthy=False, waiting=False, qualified=True,
                                  last_quote=None)
        self.now = 601
        caller = asyncio.create_task(self.alerter.observe(
            healthy=False, waiting=False, qualified=True, last_quote=None))
        try:
            await self.started.wait()
            self.assertTrue(caller.done(), '周期观察等待了普通告警 HTTP')
            self.assertIn('10 分钟', self.sent[-1])
        finally:
            self.release.set()
            await caller
            await self.finished.wait()

    async def test_observe_hourly_summary_returns_before_http_finishes(self):
        await self.alerter.observe(healthy=False, waiting=True, qualified=True,
                                  last_quote=None)
        self.now = 3600
        caller = asyncio.create_task(self.alerter.observe(
            healthy=False, waiting=True, qualified=True, last_quote=None,
            summary={'持仓': 3}))
        try:
            await self.started.wait()
            self.assertTrue(caller.done(), '周期观察等待了小时摘要 HTTP')
            self.assertIn('每小时摘要', self.sent[-1])
            self.assertIn('持仓', self.sent[-1])
        finally:
            self.release.set()
            await caller
            await self.finished.wait()

    async def test_failed_inflight_batch_keeps_the_newest_value_for_retry(self):
        first_finished = asyncio.Event()

        async def http(method, url, *, json=None, timeout=10):
            self.sent.append(json['text'])
            if len(self.sent) == 1:
                self.started.set()
                await self.release.wait()
                first_finished.set()
                return HttpResponse(500, {'ok': False})
            return HttpResponse(200, {'ok': True})

        self.alerter._http = http
        caller = asyncio.create_task(self.alerter.alert('旧判据', key='criterion'))
        update = None
        try:
            await self.started.wait()
            update = asyncio.create_task(self.alerter.alert('新判据', key='criterion'))
            await asyncio.sleep(0)
            self.assertTrue(update.done(), 'HTTP 失败前的新入队被阻塞')
            with self.assertLogs('pendle.alerts', logging.WARNING):
                self.release.set()
                await first_finished.wait()
                await asyncio.sleep(0)
            self.now = 300
            await self.alerter.alert('第二守卫', key='guard')
            await asyncio.sleep(0)
            self.assertEqual(len(self.sent), 2)
            self.assertIn('新判据', self.sent[-1])
            self.assertIn('第二守卫', self.sent[-1])
            self.assertNotIn('旧判据', self.sent[-1])
        finally:
            self.release.set()
            await asyncio.gather(*(task for task in (caller, update) if task is not None))

    async def test_background_failures_are_collected_and_log_no_exception_secrets(self):
        started = asyncio.Event()

        async def failed_telegram(text):
            started.set()
            raise RuntimeError('token chat sign=' + 'a' * 64)

        self.alerter._telegram = failed_telegram
        with self.assertLogs('pendle.alerts', logging.WARNING) as logs:
            try:
                self.assertTrue(await self.alerter.alert('后台故障'))
            except RuntimeError:
                self.fail('后台发送异常逸出策略关键路径')
            await started.wait()
            await asyncio.sleep(0)
        self.assertEqual(self.alerter._background_tasks, set())
        for secret in ('token', 'chat', 'a' * 64):
            self.assertNotIn(secret, '\n'.join(logs.output))
