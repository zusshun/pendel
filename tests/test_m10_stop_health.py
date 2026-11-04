"""M10: test local stop recovery in dry-run mode and task-based health checks without network access."""

import json
import logging
import unittest
from dataclasses import replace
from decimal import Decimal as D

from pendle.maker import Maker, MarketRules
from pendle.maker_config import MakerSettings
from pendle.maker_orders import OrdersBlocked
from pendle.ondo_api import SendGate
from test_maker import MARKET, book
from test_maker_e2e import Alerts, Clock, Exchange, Files


class NoNetworkAPI:
    """Record and immediately fail every API call, including reads, writes, and stop endpoints."""

    def __init__(self):
        self.gate, self.calls, self.in_flight_writes = SendGate(), [], 0

    def __getattr__(self, name):
        async def forbidden(*args, **kwargs):
            self.calls.append(name)
            raise AssertionError('dry-run 停止不得调用网络接口：' + name)
        return forbidden


class LogRows(logging.Handler):
    def __init__(self):
        super().__init__()
        self.rows = []

    def emit(self, record):
        self.rows.append(json.loads(record.getMessage()))


class M10StopHealthTests(unittest.IsolatedAsyncioTestCase):
    def make_bot(self, *, api=None, mode='dry-run'):
        self.clock, self.files, self.alerts = Clock(), Files(), Alerts()
        settings = replace(MakerSettings(), mode=mode,
            guards_calibrated=mode == 'live', sandbox_verified=mode == 'live',
            healthcheck_waived=mode == 'live', cooldown_s=2, stable_s=1)
        self.logs = LogRows()
        logger = logging.Logger('m10-stop-health', level=logging.INFO)
        logger.addHandler(self.logs)
        self.bot = Maker(settings, api=api, alerter=self.alerts,
            rules=MarketRules(D('.01'), D('.001')), files=self.files,
            monotonic=self.clock, clock=self.clock.wall, sleep=self.clock.sleep,
            logger=logger)
        self.addCleanup(self.bot.instance.release)
        return self.bot

    def refresh(self):
        self.bot.book = book(at=int(self.clock.wall() * 1000))
        self.bot.book_seen = self.clock()
        self.bot.engine.mark = (D(100), self.clock())
        self.bot.engine.in_price_discovery = False
        self.bot.engine.price_discovery_seen = self.clock()

    async def health_once(self):
        async def finish_iteration(delay):
            self.bot.stop_event.set()

        self.bot.stop_event.clear()
        self.bot.sleep = finish_iteration
        await self.bot.monitor_loop()
        self.bot.stop_event.clear()
        return next(row for row in reversed(self.logs.rows) if row['事件'] == '健康状态')

    async def test_dry_run_guard_cooldown_resume_repeats_without_network(self):
        for with_api in (False, True):
            with self.subTest(with_api=with_api):
                api = NoNetworkAPI() if with_api else None
                bot = self.make_bot(api=api)
                await bot.startup()
                self.refresh()
                await bot.tick()
                self.assertTrue(bot.qualified)
                for cycle in range(3):
                    self.clock.now += bot.settings.max_book_age_s + .01
                    await bot.tick()
                    self.assertEqual(bot.guard.state, 'COOLDOWN')
                    self.assertTrue(bot.state.stopping)
                    self.clock.now += bot.settings.cooldown_s + .01
                    self.refresh()
                    await bot.tick()
                    self.assertEqual(bot.guard.state, 'STABILIZING')
                    self.clock.now += bot.settings.stable_s + .01
                    self.refresh()
                    await bot.tick()
                    self.assertEqual(bot.guard.state, 'RUNNING', cycle)
                    self.assertFalse(bot.state.stopping)
                    self.assertTrue(bot.qualified)
                    self.assertFalse(bot.stop_event.is_set())
                self.assertFalse(bot.state.deadman_acked)
                self.assertFalse(bot._stop_failed)
                self.assertEqual(bot.exit_code, 0)
                self.assertEqual(sum(row['事件'] == '已确认停止' for row in self.logs.rows), 3)
                if api is not None:
                    self.assertEqual(api.calls, [])

    async def test_dry_run_stop_records_local_confirmation_and_reason(self):
        api = NoNetworkAPI()
        bot = self.make_bot(api=api)
        self.assertTrue(await bot.stop_orders('公开盘口过期'))
        self.assertTrue(bot.state.stopping)
        self.assertEqual(bot.state.market_mode(MARKET), 'STOPPING')
        self.assertIsNotNone(bot.manager._stop_task)
        self.assertTrue(bot.manager._stop_task.done())
        self.assertIs(bot.manager._stop_task.result(), True)
        self.assertIn({'事件': '已确认停止', '模式': 'dry-run', '原因': '公开盘口过期'}, self.logs.rows)
        self.assertEqual(api.calls, [])
        self.assertEqual(self.alerts.messages, [])

    async def test_sandbox_and_live_stop_still_require_remote_confirmation_and_ack(self):
        for mode in ('sandbox', 'live'):
            with self.subTest(mode=mode):
                bot = self.make_bot(mode=mode)
                exchange = Exchange(self.clock, self.files)
                bot.api = bot.manager.api = exchange
                bot.gate = exchange.gate
                self.assertTrue(await bot.stop_orders('回归停止确认'))
                self.assertIn(('cancel_all',), exchange.calls)
                with self.assertRaises(OrdersBlocked):
                    bot.manager.prepare_resume()
                bot.state.deadman_ack()
                bot.manager.prepare_resume()
                self.assertFalse(bot.state.stopping)
                self.assertEqual(bot.state.market_mode(MARKET), 'SYNCING')

    async def test_dry_run_without_private_api_only_requires_running_strategy(self):
        bot = self.make_bot()
        await bot.startup()
        self.clock.now += 90
        bot.watchdog.beat('Strategy')
        self.assertEqual(bot.watchdog.unhealthy_tasks(), ())
        self.clock.now += 15.01
        self.assertEqual(bot.watchdog.unhealthy_tasks(), ('Strategy',))

    async def test_dry_run_with_api_keeps_private_feed_and_reconciler_required(self):
        bot = self.make_bot(api=NoNetworkAPI())
        await bot.startup()
        self.clock.now += 90
        bot.watchdog.beat('Strategy')
        self.assertEqual(set(bot.watchdog.unhealthy_tasks()), {'PrivateFeed', 'Reconciler'})
        bot.watchdog.pong()
        bot.watchdog.beat('Reconciler')
        self.assertEqual(bot.watchdog.unhealthy_tasks(), ())

    async def test_sandbox_and_live_keep_all_four_tasks_required(self):
        expected = {'Strategy', 'OrderManager', 'PrivateFeed', 'Reconciler'}
        for mode in ('sandbox', 'live'):
            with self.subTest(mode=mode):
                bot = self.make_bot(api=NoNetworkAPI(), mode=mode)
                self.assertEqual(set(bot.watchdog.unhealthy_tasks()), expected)

    async def test_health_log_lists_unhealthy_tasks_and_clears_on_recovery(self):
        bot = self.make_bot()
        await bot.startup()
        self.clock.now += 90
        row = await self.health_once()
        self.assertEqual(row.get('不健康任务'), ['Strategy'])
        self.assertFalse(row['关键任务健康'])
        bot.watchdog.beat('Strategy')
        bot.last_health = None
        row = await self.health_once()
        self.assertEqual(row['不健康任务'], [])
        self.assertTrue(row['关键任务健康'])
