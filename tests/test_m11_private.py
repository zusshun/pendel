"""M11: real private samples, strict dead man's switch acknowledgments, and read-only heartbeats."""

import asyncio
import copy
import json
import logging
import unittest
from dataclasses import replace
from pathlib import Path

from pendle.maker import Maker, MarketRules
from pendle.maker_config import MakerSettings
from test_maker import MARKET
from test_maker_e2e import Alerts, Clock, Exchange, Files


SAMPLES = json.loads((Path(__file__).parent / 'fixtures/live_samples/ws_private_samples.json').read_text())


def deadman_ack(timeout):
    message = copy.deepcopy(SAMPLES['deadman_ack_enable'])
    message['data']['timeout_seconds'] = timeout
    return message


class PrivateSampleTests(unittest.IsolatedAsyncioTestCase):
    def make_bot(self, *, mode='sandbox', timeout=30):
        self.clock, self.files, self.alerts = Clock(), Files(), Alerts()
        self.api = Exchange(self.clock, self.files)
        self.bot = Maker(replace(MakerSettings(), mode=mode, deadman_timeout_s=timeout),
            api=self.api, alerter=self.alerts, rules=MarketRules('.01', '.01'),
            files=self.files, monotonic=self.clock, clock=self.clock.wall,
            sleep=self.clock.sleep, logger=logging.Logger('m11.private'))
        self.sent = []
        owner = self

        class WS:
            async def send(self, raw):
                owner.sent.append(json.loads(raw))
        self.bot.private_ws = WS()
        return self.bot

    async def test_real_logged_in_subscriptions_balance_and_pong(self):
        bot = self.make_bot()
        await bot.on_private(SAMPLES['loggedIn'])
        self.assertTrue(bot.private_logged_in)
        self.assertEqual([row['channel'] for row in self.sent if row['op'] == 'subscribe'],
            ['ordersPerps', 'fillsPerps', 'positionsPerps', 'balancePerps', 'cancelAllOrdersAfterPerps'])
        bot.reconcile_event.clear()
        for name in ('orders', 'fills', 'positions', 'balance'):
            await bot.on_private(SAMPLES['subscribed_' + name])
        self.assertFalse(bot.reconcile_event.is_set())
        await bot.on_private(SAMPLES['balance_update'])
        self.assertEqual(bot.state.balances['equity'], '300')
        self.assertEqual(bot.state.balances['available'], '300')
        self.assertEqual(bot.state.balances['margin_ratio'], '0')
        self.assertIs(bot.state.balances['under_liquidation'], False)
        self.clock.now += 1
        await bot.on_private(SAMPLES['pong'])
        self.assertEqual(bot.watchdog.last_seen['PrivateFeed'], self.clock())
        self.assertEqual(self.alerts.messages, [])

    async def test_deadman_ack_requires_real_type_channel_and_exact_configured_timeout(self):
        bot = self.make_bot(timeout=20)
        await bot.on_private(SAMPLES['loggedIn'])
        self.assertEqual(dict(bot.settings.deadman_ack_match), {
            'type': 'subscribed', 'channel': 'cancelAllOrdersAfterPerps', 'data.timeout_seconds': 20})
        bad = [dict(type='subscribed', channel='cancelAllOrdersAfterPerps'),
            deadman_ack(30), deadman_ack(0), deadman_ack('20'), deadman_ack(True)]
        for key in ('type', 'channel'):
            message = deadman_ack(20)
            message[key] = 'wrong'
            bad.append(message)
        for message in bad:
            await bot.on_private(message)
            self.assertFalse(bot.state.deadman_acked, message)
        await bot.on_private(deadman_ack(20))
        self.assertTrue(bot.state.deadman_acked)
        self.assertIsNone(bot.ack_wait_started)

    async def test_custom_deadman_match_cannot_bypass_timeout_confirmation(self):
        bot = self.make_bot()
        bot.settings = replace(bot.settings, deadman_ack_match={'ok': True})
        await bot.on_private(SAMPLES['loggedIn'])
        await bot.on_private({'ok': True})
        self.assertFalse(bot.state.deadman_acked)
        good = deadman_ack(30)
        good['ok'] = True
        await bot.on_private(good)
        self.assertTrue(bot.state.deadman_acked)

    async def test_shutdown_waits_for_matching_zero_ack_and_records_success(self):
        bot = self.make_bot()
        bot.private_logged_in = bot.deadman_requested = True
        bot.state.deadman_ack()
        sent_disable, release_ack = asyncio.Event(), asyncio.Event()
        owner = self

        class WS:
            async def send(self, raw):
                message = json.loads(raw)
                owner.sent.append(message)
                if message.get('timeout_seconds') == 0:
                    sent_disable.set()
        bot.private_ws = WS()

        async def never_timeout(delay):
            await release_ack.wait()
        bot.sleep = never_timeout
        shutdown = asyncio.create_task(bot.shutdown())
        try:
            await sent_disable.wait()
            for message in (deadman_ack(30), {'type': 'subscribed',
                    'channel': 'cancelAllOrdersAfterPerps'}, deadman_ack('0'), deadman_ack(False)):
                await bot.on_private(message)
                self.assertFalse(shutdown.done())
            await bot.on_private(SAMPLES['deadman_ack_disable'])
            await asyncio.wait_for(shutdown, 1)
            self.assertTrue(bot.deadman_disabled)
            self.assertFalse(bot.state.deadman_acked)
            self.assertEqual(bot.exit_code, 0)
        finally:
            shutdown.cancel()
            await asyncio.gather(shutdown, return_exceptions=True)

    async def test_shutdown_missing_disable_ack_is_failure(self):
        bot = self.make_bot()
        await bot.shutdown()
        self.assertEqual(bot.exit_code, 76)
        self.assertFalse(bot.deadman_disabled)
        self.assertTrue(any('关闭' in text and 'ACK' in text for text in self.alerts.messages))

    async def test_shutdown_keeps_private_feed_consuming_until_disable_ack(self):
        bot = self.make_bot()
        queue, disabled_request = asyncio.Queue(), asyncio.Event()

        class WS:
            async def send(self, raw):
                if json.loads(raw).get('timeout_seconds') == 0:
                    disabled_request.set()

            def __aiter__(self):
                return self

            async def __anext__(self):
                return await queue.get()

            async def close(self):
                pass

        async def connect(*args, **kwargs):
            return WS()

        async def no_timeout(delay):
            await asyncio.Event().wait()

        bot.connect, bot.sleep = connect, no_timeout
        feed = asyncio.create_task(bot.feed(True))
        shutdown = None
        try:
            await queue.put(SAMPLES['loggedIn'])
            for _ in range(5):
                await asyncio.sleep(0)
            bot.request_stop()
            shutdown = asyncio.create_task(bot.shutdown())
            await asyncio.wait_for(disabled_request.wait(), 1)
            self.assertFalse(feed.done())
            await queue.put(SAMPLES['deadman_ack_disable'])
            await asyncio.wait_for(shutdown, 1)
            self.assertTrue(bot.deadman_disabled)
            self.assertEqual(bot.exit_code, 0)
        finally:
            feed.cancel()
            if shutdown is not None:
                shutdown.cancel()
            await asyncio.gather(feed, *([shutdown] if shutdown is not None else []),
                                 return_exceptions=True)

    async def test_dry_run_ping_repeats_beyond_private_stale_threshold_without_deadman(self):
        bot = self.make_bot(mode='dry-run')
        await bot.on_private(SAMPLES['loggedIn'])
        self.assertIsNone(bot.ack_wait_started)
        original_send = bot.private_ws.send

        async def pong_on_ping(raw):
            await original_send(raw)
            if json.loads(raw).get('op') == 'ping':
                await bot.on_private(SAMPLES['pong'])
        bot.private_ws.send = pong_on_ping
        await bot.on_private(SAMPLES['pong'])

        async def tick(delay):
            self.clock.now += delay
            bot.watchdog.beat('Strategy')
            bot.watchdog.beat('Reconciler')
            if self.clock.now >= 136:
                bot.stop_event.set()
            await asyncio.sleep(0)
        bot.sleep = tick
        await bot.watchdog_loop()
        self.assertGreaterEqual(sum(row['op'] == 'ping' for row in self.sent), 4)
        self.assertEqual(bot.watchdog.unhealthy_tasks(), ())
        self.assertFalse(bot.watchdog.tripped)
        self.assertFalse(bot.state.deadman_acked)
        self.assertFalse([row for row in self.sent if row.get('channel') == 'cancelAllOrdersAfterPerps'])

    async def test_dry_run_run_schedules_private_watchdog(self):
        bot = self.make_bot(mode='dry-run')
        watchdog_started = asyncio.Event()

        async def startup(**kwargs):
            bot.started = True
        async def blocked(*args):
            await asyncio.Event().wait()
        async def watch():
            watchdog_started.set()
            bot.request_stop()
        async def stop_when_missing():
            for _ in range(5):
                await asyncio.sleep(0)
            bot.request_stop()
        bot.startup = startup
        bot.feed = bot.strategy_loop = bot.reconcile_loop = blocked
        bot.monitor_loop = stop_when_missing
        bot.watchdog_loop = watch
        await bot.run()
        self.assertTrue(watchdog_started.is_set())

    def test_example_deadman_rule_matches_real_sample_and_configured_timeout(self):
        settings = MakerSettings.from_file(Path(__file__).parents[1] / 'maker.example.toml')
        self.assertEqual(dict(settings.deadman_ack_match), {
            'type': 'subscribed', 'channel': 'cancelAllOrdersAfterPerps', 'data.timeout_seconds': 30})

    def test_default_deadman_rule_tracks_reconfigured_timeout(self):
        settings = MakerSettings(deadman_timeout_s=20)
        changed = replace(settings, deadman_timeout_s=15)
        self.assertEqual(changed.deadman_ack_match['data.timeout_seconds'], 15)

    async def test_dry_run_private_ping_is_independent_of_failed_public_socket(self):
        bot = self.make_bot(mode='dry-run')

        class FailedPublic:
            async def send(self, message):
                raise OSError('HTTP 429 公开连接已关闭')
        bot.public_ws = FailedPublic()
        await bot.on_private(SAMPLES['loggedIn'])
        self.assertTrue(bot.private_logged_in)
        self.assertEqual(self.sent[-1], {'op': 'ping'})

    async def test_dry_run_ping_failure_does_not_abort_reconnect_wait(self):
        bot = self.make_bot(mode='dry-run')
        await bot.on_private(SAMPLES['loggedIn'])

        async def failed(raw):
            raise OSError('HTTP 429 私有连接已断开')
        bot.private_ws.send = failed

        async def tick(delay):
            self.clock.now += delay
            if self.clock.now >= 102:
                bot.stop_event.set()
        bot.sleep = tick
        await bot.watchdog_loop()
        self.assertTrue(bot.gate.is_open)
        self.assertEqual(bot.exit_code, 0)

    async def test_dry_run_ping_stall_is_bounded_without_closing_gate(self):
        bot = self.make_bot(mode='dry-run')
        await bot.on_private(SAMPLES['loggedIn'])
        bot.watchdog.ping_timeout_s = .001

        async def stalled(raw):
            await asyncio.Event().wait()
        bot.private_ws.send = stalled

        async def tick(delay):
            self.clock.now += delay
            if self.clock.now >= 102:
                bot.stop_event.set()
        bot.sleep = tick
        await asyncio.wait_for(bot.watchdog_loop(), .1)
        self.assertTrue(bot.gate.is_open)
