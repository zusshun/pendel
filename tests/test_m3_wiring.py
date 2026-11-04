"""M3 safety regressions through the real builder, signing queue, and HTTP stubs."""

import asyncio
import io
import json
import logging
import unittest
from dataclasses import replace
from decimal import Decimal as D
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import patch

from pendle.maker import MarketRules
from pendle.maker_config import MakerSettings
from pendle.maker_risk import RiskError
from pendle.maker_transport import build_maker
from pendle.ondo_api import HttpResponse
from test_maker import MARKET, book
from test_maker_e2e import live_deadman_ack, Clock, Files
from test_maker_supervisor import WS
from test_ondo_api import FakeClock, settle


class M3SettingsTests(unittest.TestCase):
    def test_ack_timeout_defaults_to_fifteen_seconds_and_must_be_positive(self):
        self.assertEqual(getattr(MakerSettings(), 'deadman_ack_timeout_s', None), 15)
        for value in (0, -1, float('nan'), True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                MakerSettings.from_dict({'deadman_ack_timeout_s': value})

    def test_live_rejects_disabled_mark_channel(self):
        with self.assertRaisesRegex(ValueError, '标记'):
            MakerSettings(mode='live', guards_calibrated=True, sandbox_verified=True,
                          healthcheck_url='https://health.invalid/test', mark_channel='')


class M3WiringTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock, self.files, self.epoch = FakeClock(), Files(), Clock()
        self.requests, self.http_calls, self.connections = [], [], []
        self.remote, self.cancel_failure, self.zero_failure = {}, False, False
        self.position = D(0)
        self.settings = replace(MakerSettings(), mode='sandbox', quiet_s=.001,
                                rate_per_second=1000.)
        self.ws = WS()

        async def network(request):
            self.requests.append(request)
            path = request.path_query.partition('?')[0]
            if request.method == 'DELETE':
                self.files.events.append('撤单')
                if self.cancel_failure:
                    return HttpResponse(503, {'success': False})
                self.remote.clear()
                result = {}
            elif request.method == 'POST':
                row = json.loads(request.body)
                row.update(orderId=row['clientOrderId'], status='OPEN', filledSize='0',
                           time_ms=int(self.wall()*1000))
                self.remote[row['clientOrderId']] = row
                result = row
                self.publish()
            elif path.endswith('/positions'):
                result = [dict(market=MARKET, quantity=str(self.position),
                               unrealized_pnl='0', mark_price='100')]
            elif path.endswith('/balance'):
                result = dict(equity='10000', available='9000', margin_ratio='.01',
                              under_liquidation=False, time_ms=int(self.wall()*1000))
            elif path.endswith('/orders'):
                result = list(self.remote.values())
                if self.zero_failure:
                    result = [dict(market=MARKET, clientOrderId='remote', orderId='remote',
                                   side='sell', size='1', price='100.12', status='OPEN')]
            else:
                result = []
            return HttpResponse(200, {'success': True, 'result': result})

        async def http(method, url, **kwargs):
            self.http_calls.append((method, kwargs.get('json')))
            return HttpResponse(200, {'ok': True})

        async def connect(*args, **kwargs):
            self.connections.append(args)
            return self.ws

        logger = logging.getLogger(self.id())
        logger.addHandler(logging.NullHandler())
        self.bot = build_maker(self.settings, env={
            'ONDO_API_KEY': '测试-key', 'ONDO_API_SECRET': '测试-secret',
            'TELEGRAM_BOT_TOKEN': '测试-token', 'TELEGRAM_CHAT_ID': '测试-chat'},
            transport=network, http=http, connect=connect, files=self.files,
            rules=MarketRules(D('.01'), D('.001')), logger=logger,
            monotonic=self.clock.monotonic, clock=self.wall, sleep=self.clock.sleep)
        self.ws.on_send = self.bot.on_private
        self.background = []

    def wall(self):
        return self.epoch.wall()+self.clock.monotonic()

    async def drive(self, job, steps=1200):
        task = asyncio.ensure_future(job)
        for _ in range(steps):
            await settle()
            if task.done():
                return await task
            self.clock.advance(.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.fail('内存模拟超过预算')

    async def ready(self):
        await self.drive(self.bot.startup())
        self.bot.private_ws = self.ws
        await self.bot.on_private({'op': 'loggedIn'})
        await self.bot.on_private(live_deadman_ack(self.settings.deadman_timeout_s))
        self.assertTrue(await self.drive(self.bot.reconcile_once()))
        self.publish()

    def publish(self, *, mark=True):
        bids, asks = [('99.9', '10'), ('99', '10')], [('100.1', '10'), ('101', '10')]
        for row in self.remote.values():
            (bids if row['side']=='buy' else asks).append((row['price'], row['size']))
        self.bot.book = book(bids=bids, asks=asks, at=int(self.wall()*1000))
        self.bot.book_seen = self.clock.monotonic()
        if mark:
            self.bot.engine.mark = (D(100), self.clock.monotonic())
            self.bot.engine.in_price_discovery = False
            self.bot.engine.price_discovery_seen = self.clock.monotonic()

    async def asyncTearDown(self):
        for task in self.background:
            task.cancel()
        await asyncio.gather(*self.background, return_exceptions=True)
        if self.bot.started:
            await self.drive(self.bot.shutdown())

    def messages(self):
        return [payload['text'] for method, payload in self.http_calls if method=='POST']

    async def test_stop_failed_persists_halt_permanently_seals_without_renewal_or_reconnect(self):
        self.cancel_failure = True
        await self.ready()
        await self.bot.alerter.alert('此前普通告警')
        self.bot.gate._listeners.append(lambda: self.files.events.append('永久封门')
            if not self.bot.gate.is_open else None)
        before = len(self.http_calls)
        self.assertFalse(await self.drive(self.bot.guard.trigger('安全停止')))
        self.assertEqual(self.bot.guard.state, 'STOP_FAILED')
        self.assertIn(str(self.settings.halt_path), self.files.rows)
        self.assertFalse(self.bot.gate.is_open)
        self.assertEqual(self.bot.exit_code, 76)
        self.assertTrue(self.bot.stop_event.is_set())
        self.assertLess(self.files.events.index('fsync锁'), self.files.events.index('永久封门'))
        self.assertGreater(len(self.http_calls), before)
        self.assertTrue(any('紧急告警' in text for text in self.messages()))
        self.assertEqual(sum(r.method=='DELETE' for r in self.requests), 3)
        sent, connected = len(self.ws.sent), len(self.connections)
        for name in ('Strategy', 'OrderManager', 'Reconciler'):
            self.bot.watchdog.beat(name)
        self.bot.watchdog.pong()
        self.clock.advance(60)
        await self.bot.watchdog.check_once()
        await self.bot.feed(True)
        await self.bot.feed(False)
        self.assertEqual(len(self.ws.sent)-sent, 0)
        self.assertEqual(len(self.connections)-connected, 0)

    async def test_zero_confirmation_failure_also_seals_after_three_rounds(self):
        self.zero_failure = True
        await self.drive(self.bot.startup())
        await self.bot.alerter.alert('此前普通告警')
        self.assertFalse(await self.drive(self.bot.stop_orders('核零失败')))
        self.assertFalse(self.bot.gate.is_open)
        self.assertEqual(sum(r.method=='DELETE' for r in self.requests), 3)
        self.assertTrue(any('紧急告警' in text for text in self.messages()))

    async def test_stop_failure_after_existing_halt_sends_its_own_urgent_http(self):
        await self.ready()
        await self.drive(self.bot.trip_halt('此前已经熔断'))
        before = len(self.http_calls)
        self.cancel_failure = True
        self.assertFalse(await self.drive(self.bot.stop_orders('熔断监控中核零失败')))
        self.assertGreater(len(self.http_calls), before)
        self.assertFalse(self.bot.gate.is_open)
        self.assertTrue(any('STOP_FAILED' in text for text in self.messages()))

    async def test_failure_detection_seals_before_already_scheduled_renewal_or_reconnect(self):
        await self.ready()
        for name in ('Strategy', 'OrderManager', 'Reconciler'):
            self.bot.watchdog.beat(name)
        self.bot.watchdog.pong()
        after_failure = []
        send = self.ws.send
        async def observe_send(message):
            if self.bot._stop_failed:
                after_failure.append(message)
            return await send(message)
        self.ws.send = observe_send
        renewal = asyncio.create_task(self.bot.watchdog.check_once())
        await asyncio.sleep(0)  # The renewal check has passed, but the actual ping has not run yet.
        reconnect = asyncio.create_task(self.bot.feed(True))
        connected = len(self.connections)
        await self.bot.stop_failed('已检测到三轮撤单失败')
        reconnect.cancel()
        await asyncio.gather(renewal, reconnect, return_exceptions=True)
        self.assertEqual(after_failure, [])
        self.assertEqual(len(self.connections), connected)

    async def test_empty_local_drain_still_removes_remote_orphans(self):
        await self.ready()
        await self.drive(self.bot.stop_orders('首次停止'))
        self.remote['orphan'] = dict(clientOrderId='orphan', orderId='orphan', market=MARKET,
            side='buy', size='1', price='99.88', filledSize='0', status='OPEN')
        self.assertFalse(self.bot.state.active_orders())
        await self.drive(self.bot.manager.drain())
        self.assertFalse(self.remote)

    async def test_reconnect_stall_checks_expired_tasks_without_ack(self):
        await self.ready()
        async def stalled(*args, **kwargs):
            self.connections.append(args)
            await asyncio.Event().wait()
        self.bot.connect = stalled
        self.bot.state.private_disconnected()
        feed = asyncio.create_task(self.bot.feed(True))
        watchdog = asyncio.create_task(self.bot.watchdog_loop())
        self.background.extend((feed, watchdog))
        await settle()
        self.clock.advance(60)
        await self.drive(watchdog)
        self.assertFalse(self.bot.gate.is_open)
        self.assertTrue(self.bot.watchdog.tripped)
        self.assertFalse(self.bot.state.deadman_acked)
        self.assertTrue(any('紧急告警' in text for text in self.messages()))

    async def test_login_and_deadman_ack_have_independent_deadlines(self):
        await self.drive(self.bot.startup())
        feed = asyncio.create_task(self.bot.feed(True))
        watchdog = asyncio.create_task(self.bot.watchdog_loop())
        self.background.extend((feed, watchdog))
        await settle()
        # Keep critical tasks healthy and let only the login ACK time out.
        for _ in range(16):
            for name in ('Strategy', 'OrderManager', 'Reconciler'):
                self.bot.watchdog.beat(name)
            self.bot.watchdog.pong()
            self.clock.advance(1)
            await settle()
        await self.drive(watchdog)
        self.assertFalse(self.bot.gate.is_open)
        self.assertTrue(any('ACK' in text and '超时' in text for text in self.messages()))

    async def test_deadman_ack_timeout_after_successful_login_reaches_http(self):
        await self.drive(self.bot.startup())
        self.bot.private_ws = self.ws
        await self.bot.on_private({'op': 'loggedIn'})
        watchdog = asyncio.create_task(self.bot.watchdog_loop())
        self.background.append(watchdog)
        await self.bot.alerter.alert('此前普通告警')
        for _ in range(16):
            for name in ('Strategy', 'OrderManager', 'Reconciler'):
                self.bot.watchdog.beat(name)
            self.bot.watchdog.pong()
            self.clock.advance(1)
            await settle()
        await self.drive(watchdog)
        self.assertFalse(self.bot.gate.is_open)
        self.assertTrue(any('死人开关 ACK 超时' in text for text in self.messages()))

    async def test_repeated_login_after_ack_restarts_bounded_deadman_wait(self):
        await self.ready()
        await self.bot.on_private({'op': 'loggedIn'})
        started = self.bot.ack_wait_started
        self.assertIsNotNone(started)
        self.clock.advance(10)
        await self.bot.on_private({'op': 'loggedIn'})
        self.assertEqual(self.bot.ack_wait_started, started,
                         '等待中的重复登录确认不能刷新 ACK 截止时间')
        for name in ('Strategy', 'OrderManager', 'Reconciler'):
            self.bot.watchdog.beat(name)
        self.bot.watchdog.pong()
        watchdog = asyncio.create_task(self.bot.watchdog_loop())
        self.background.append(watchdog)
        await settle()
        self.clock.advance(5)
        await self.drive(watchdog)
        self.assertFalse(self.bot.gate.is_open)
        self.assertTrue(any('死人开关 ACK 超时' in text for text in self.messages()))

    async def test_healthy_tasks_without_ack_do_not_renew_deadman(self):
        await self.drive(self.bot.startup())
        watchdog = asyncio.create_task(self.bot.watchdog_loop())
        self.background.append(watchdog)
        await settle()
        for _ in range(10):
            for name in ('Strategy', 'OrderManager', 'Reconciler'):
                self.bot.watchdog.beat(name)
            self.bot.watchdog.pong()
            self.clock.advance(1)
            await settle()
        self.assertTrue(self.bot.gate.is_open)
        self.assertFalse(self.ws.sent)

    async def test_reverse_quote_eight_hundred_dollars_stays_within_hard_cap(self):
        await self.ready()
        self.bot.settings = replace(self.settings, quote_notional=D(800))
        self.bot.engine.settings = self.bot.settings
        self.bot.risk = type(self.bot.risk)(self.bot.settings.risk_limits)
        self.bot.state.positions[MARKET] = self.position = D(1)
        await self.drive(self.bot.tick())
        sells = [json.loads(r.body) for r in self.requests if r.method=='POST'
                 and json.loads(r.body)['side']=='sell']
        self.assertEqual(len(sells), 1)
        self.assertEqual(sum(r.method=='POST' for r in self.requests), 2)
        self.assertGreaterEqual((D(1)-D(sells[0]['size']))*D(100), -D(600))
        before = len(self.requests)
        self.clock.advance(11)
        self.publish()
        await self.drive(self.bot.tick())
        self.assertEqual(len(self.requests), before)

    async def test_rate_queue_rechecks_latest_position_before_actual_http(self):
        await self.ready()
        order = self.bot.manager.create_intent(MARKET, 'buy', D(3), D('99.88'))
        self.bot.api._tokens = 0
        self.bot.api.rate = 1
        submit = asyncio.create_task(self.bot.manager.submit(order))
        await settle()
        self.bot.state.positions[MARKET] = D(4)
        self.publish()
        await self.drive(submit)
        self.assertFalse([r for r in self.requests if r.method=='POST'])

    async def test_rate_queue_rechecks_mark_freshness_before_actual_http(self):
        await self.ready()
        order = self.bot.manager.create_intent(MARKET, 'buy', D(1), D('99.88'))
        self.bot.api._tokens = 0
        self.bot.api.rate = 1
        submit = asyncio.create_task(self.bot.manager.submit(order))
        await settle()
        self.clock.advance(1)
        self.bot.engine.mark = (D(100), self.clock.monotonic()-self.settings.mark_quiet_max_s-.001)
        self.publish(mark=False)
        await self.drive(submit)
        self.assertFalse([r for r in self.requests if r.method=='POST'])

    async def test_trading_without_fresh_mark_blocks_both_sides(self):
        await self.ready()
        for mark in (None, (D(100), self.clock.monotonic()-self.settings.mark_quiet_max_s-.001)):
            with self.subTest(mark=mark):
                self.bot.engine.mark = mark
                self.assertFalse(self.bot.engine.evaluate(self.bot.book, self.bot.state,
                    self.bot.rules, now=self.clock.monotonic()).eligible)
                await self.drive(self.bot.tick())
        self.assertFalse([r for r in self.requests if r.method=='POST'])

    async def test_dry_run_missing_mark_waits_without_stopping_or_cooldown(self):
        await self.ready()
        self.bot.settings = replace(self.settings, mode='dry-run')
        self.bot.engine.settings = self.bot.settings
        self.bot.engine.mark = None
        with self.assertNoLogs(self.bot.logger, level='WARNING'):
            await self.bot.tick()
        self.assertFalse(self.bot.qualified)
        self.assertTrue(self.bot.waiting)
        self.assertEqual(self.bot.observed_reason, '等待首个标记价格')
        self.assertEqual(self.bot.guard.state, 'RUNNING')
        self.assertFalse(self.bot.state.stopping)
        self.assertFalse([r for r in self.requests if r.method=='POST'])

    async def test_flock_is_acquired_before_halt_and_ledger_reads(self):
        events = []
        acquire = self.files.acquire_instance
        exists = self.files.exists
        def racing_acquire(path):
            events.append('flock')
            self.files.rows[str(self.settings.halt_path)] = {'reason': '旧实例此时写锁并退出'}
            return acquire(path)
        def exists_after_lock(path):
            events.append('读取')
            self.assertIn('flock', events)
            return exists(path)
        self.files.acquire_instance, self.files.exists = racing_acquire, exists_after_lock
        with self.assertRaisesRegex(RiskError, '熔断锁'):
            await self.bot.startup()
        self.assertEqual(events, ['flock', '读取'])
        self.assertIsNone(self.bot.instance.handle)
        self.assertIsNone(self.bot.ledger)

    async def test_fatal_halt_and_residual_alerts_reach_http_despite_throttle(self):
        await self.ready()
        await self.bot.alerter.alert('普通守卫通知')
        before = len(self.http_calls)
        await self.drive(self.bot.trip_halt('硬上限减仓失败'))
        self.assertGreater(len(self.http_calls), before)
        self.assertTrue(any('硬上限减仓失败' in text for text in self.messages()))

    async def test_window_end_residual_position_urgently_reaches_http(self):
        await self.ready()
        await self.bot.alerter.alert('普通守卫通知')
        self.epoch.epoch = datetime(2026,11,9,6,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        self.bot.state.positions[MARKET] = self.position = D(1)
        await self.drive(self.bot.tick())
        self.assertTrue(any('紧急告警' in text and '窗口结束仍有持仓' in text
                            for text in self.messages()))

    async def test_halt_persistence_failure_seals_and_urgently_calls_http(self):
        await self.ready()
        await self.bot.alerter.alert('普通守卫通知')
        def cannot_write(path, value):
            raise OSError('模拟磁盘故障')
        self.files.create_halt = cannot_write
        await self.drive(self.bot.trip_halt('测试写锁失败'))
        self.assertFalse(self.bot.gate.is_open)
        self.assertEqual(self.bot.exit_code, 76)
        self.assertTrue(self.bot.stop_event.is_set())
        self.assertTrue(any('紧急告警' in text and '熔断锁持久化失败' in text
                            for text in self.messages()))

    async def test_run_stop_failed_exits_76_after_urgent_http_completes(self):
        self.cancel_failure = True
        entered, release, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        http = self.bot.alerter._http
        async def slow_http(method, url, **kwargs):
            if '紧急告警' in kwargs.get('json', {}).get('text', ''):
                entered.set()
                await release.wait()
                completed.set()
            return await http(method, url, **kwargs)
        self.bot.alerter._http = slow_http
        async def trigger_runtime_guard():
            # Wait while the first book is missing; explicitly simulate a running guard failing to stop.
            await self.bot.guard.trigger('模拟运行中盘口过期')
        self.bot.strategy_loop = trigger_runtime_guard
        running = asyncio.create_task(self.bot.run())
        self.background.append(running)
        try:
            await self.drive(entered.wait())
            self.assertFalse(self.bot.gate.is_open)
            self.assertFalse(running.done())
        finally:
            release.set()
        self.assertEqual(await self.drive(running), 76)
        self.assertTrue(completed.is_set())
        self.assertTrue(any('STOP_FAILED' in text for text in self.messages()))
        self.assertIsNone(self.bot.instance.handle)

    async def test_run_repeated_cancellation_finishes_shutdown_and_urgent_flush(self):
        entered, release, flushed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        shutdown = self.bot.shutdown
        async def slow_shutdown():
            entered.set()
            await release.wait()
            await shutdown()
        self.bot.shutdown = slow_shutdown
        flush = self.bot.alerter.flush_urgent
        async def observed_flush(**kwargs):
            flushed.set()
            return await flush(**kwargs)
        self.bot.alerter.flush_urgent = observed_flush
        running = asyncio.create_task(self.bot.run())
        self.background.append(running)
        await settle()
        running.cancel()
        await entered.wait()
        running.cancel()
        await settle()
        try:
            self.assertFalse(running.done())
        finally:
            release.set()
            try:
                await self.drive(running)
            except asyncio.CancelledError:
                pass
        self.assertTrue(flushed.is_set())

    async def test_startup_failure_logs_redacted_reason_to_stderr(self):
        async def failure(**kwargs):
            raise ValueError('自检失败 测试-key 测试-secret 测试-token 测试-chat')
        self.bot.startup = failure
        stderr = io.StringIO()
        with patch('sys.stderr', stderr), self.assertLogs(self.bot.logger, 'ERROR') as logs:
            self.assertEqual(await self.bot.run(), 2)
        for output in (stderr.getvalue(), '\n'.join(logs.output)):
            self.assertIn('自检失败', output)
            for secret in ('测试-key', '测试-secret', '测试-token', '测试-chat'):
                self.assertNotIn(secret, output)
