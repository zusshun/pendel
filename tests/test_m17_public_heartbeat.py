"""M17: test public heartbeats using in-memory connections and a controllable clock without network access."""

import asyncio
import json
import unittest
from unittest.mock import patch

from pendle.maker import Maker
from pendle.maker_config import MakerSettings
from pendle.maker_reconnect import reconnect_failures
from test_maker_e2e import Alerts, Exchange, Files
from test_ondo_api import FakeClock, settle


class PublicSocket:
    """Receiving may block or disconnect; optionally return pong after a ping is sent."""

    def __init__(self, clock, *, auto_pong=True):
        self.clock, self.auto_pong = clock, auto_pong
        self.incoming, self.sent = asyncio.Queue(), []
        self.closed, self.reading = False, False
        self.send_error = None

    async def send(self, raw):
        message = json.loads(raw)
        if message['op'] == 'ping' and self.send_error is not None:
            raise self.send_error
        self.sent.append((self.clock.now, message))
        if message['op'] == 'ping' and self.auto_pong:
            self.incoming.put_nowait({'type': 'pong'})

    @property
    def ping_times(self):
        return [at for at, message in self.sent if message['op'] == 'ping']

    def __aiter__(self):
        return self

    async def __anext__(self):
        self.reading = True
        try:
            message = await self.incoming.get()
            if isinstance(message, Exception):
                raise message
            return json.dumps(message)
        finally:
            self.reading = False

    async def close(self):
        self.closed = True


class M17PublicHeartbeatTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock, self.files = FakeClock(), Files()
        self.tasks, self.bots = [], []

    async def asyncTearDown(self):
        for bot in self.bots:
            bot.gate.close()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    def make_bot(self, *, mode='dry-run', api=None, auto_pong=True, **settings):
        sockets = []

        async def connect(url, **kwargs):
            ws = PublicSocket(self.clock, auto_pong=auto_pong)
            sockets.append(ws)
            return ws

        bot = Maker(MakerSettings(mode=mode, guards_calibrated=True,
                    sandbox_verified=True, healthcheck_waived=True, **settings),
            api=api, alerter=Alerts(), files=self.files,
            monotonic=self.clock.monotonic, clock=self.clock.time,
            sleep=self.clock.sleep, connect=connect)
        self.bots.append(bot)
        return bot, sockets

    async def start_feed(self, bot):
        task = asyncio.create_task(bot.feed())
        self.tasks.append(task)
        await settle()
        return task

    async def advance(self, seconds):
        self.clock.advance(seconds)
        await settle()

    async def test_dry_run_public_ping_every_thirty_seconds_without_login(self):
        bot, sockets = self.make_bot()
        await self.start_feed(bot)
        await self.advance(29)
        self.assertEqual(sockets[0].ping_times, [])
        await self.advance(1)
        self.assertEqual(sockets[0].ping_times, [30])
        await self.advance(30)
        await self.advance(30)
        self.assertEqual(sockets[0].ping_times, [30, 60, 90])
        self.assertTrue(all(message['op'] in ('subscribe', 'ping')
                            for _, message in sockets[0].sent))
        self.assertNotIn('PrivateFeed', bot.watchdog.last_seen)

    async def test_public_heartbeat_runs_in_every_mode(self):
        for mode in ('dry-run', 'sandbox', 'live'):
            with self.subTest(mode=mode):
                bot, sockets = self.make_bot(mode=mode)
                task = await self.start_feed(bot)
                started = self.clock.now
                await self.advance(30)
                self.assertEqual(sockets[0].ping_times, [started + 30])
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_closed_gate_blocks_public_ping_close_and_reconnect(self):
        bot, sockets = self.make_bot()
        await self.start_feed(bot)
        await self.advance(30)
        self.assertEqual(sockets[0].ping_times, [30])
        before = list(sockets[0].sent)
        bot.gate.close()
        await self.advance(60)
        self.assertEqual(sockets[0].sent, before)
        self.assertEqual(len(sockets), 1)
        self.assertFalse(sockets[0].closed)

    async def test_public_heartbeat_resumes_after_reconnect_and_cleans_old_tasks(self):
        bot, sockets = self.make_bot()
        await self.start_feed(bot)
        await self.advance(30)
        sockets[0].incoming.put_nowait(OSError('测试公开断线'))
        with patch('random.uniform', return_value=0):
            await settle()
        self.assertTrue(sockets[0].closed)
        self.assertFalse(sockets[0].reading)
        await self.advance(3)
        self.assertEqual(len(sockets), 2)
        self.assertIsNone(getattr(bot, 'public_pong_seen', None))
        await self.advance(30)
        self.assertEqual(sockets[0].ping_times, [30])
        self.assertEqual(sockets[1].ping_times, [63])
        self.assertEqual(bot.public_pong_seen, 63)

    async def test_two_missing_pongs_close_public_connection_and_back_off(self):
        bot, sockets = self.make_bot(auto_pong=False)
        await self.start_feed(bot)
        await self.advance(30)
        await self.advance(10)
        self.assertFalse(sockets[0].closed)
        await self.advance(20)
        self.assertEqual(sockets[0].ping_times, [30, 60])
        await self.advance(9)
        self.assertFalse(sockets[0].closed)
        with patch('random.uniform', return_value=0):
            await self.advance(1)
        self.assertTrue(bot.public_disconnected)
        self.assertTrue(sockets[0].closed)
        self.assertFalse(sockets[0].reading)
        self.assertEqual(reconnect_failures(bot).backoffs['公开'].failures, 1)
        await self.advance(2.9)
        self.assertEqual(len(sockets), 1)
        await self.advance(.1)
        self.assertEqual(len(sockets), 2)
        sockets[1].auto_pong = True
        await self.advance(30)
        self.assertEqual(sockets[1].ping_times, [103])
        await self.advance(10)
        self.assertFalse(sockets[1].closed)

    async def test_successful_pong_resets_consecutive_timeout_count(self):
        bot, sockets = self.make_bot(auto_pong=False)
        await self.start_feed(bot)
        await self.advance(30)
        await self.advance(10)
        sockets[0].auto_pong = True
        await self.advance(20)
        sockets[0].auto_pong = False
        await self.advance(30)
        await self.advance(10)
        self.assertFalse(sockets[0].closed)
        self.assertEqual(sockets[0].ping_times, [30, 60, 90])

    async def test_late_pongs_do_not_reset_timeouts_when_both_tasks_are_ready(self):
        bot, sockets = self.make_bot(auto_pong=False)
        await self.start_feed(bot)
        await self.advance(30)
        # When timeout and receive become ready in the same scheduling round, still check the actual pong receive time.
        self.clock.advance(10.1)
        sockets[0].incoming.put_nowait({'type': 'pong'})
        await settle()
        await self.advance(19.9)
        self.clock.advance(10.1)
        sockets[0].incoming.put_nowait({'type': 'pong'})
        with patch('random.uniform', return_value=0):
            await settle()
        self.assertEqual(bot.public_pong_seen, 70.1)
        self.assertTrue(sockets[0].closed)
        self.assertEqual(reconnect_failures(bot).backoffs['公开'].failures, 1)

    async def test_pong_before_next_ping_send_cannot_acknowledge_that_ping(self):
        bot, sockets = self.make_bot(auto_pong=False)
        await self.start_feed(bot)
        await self.advance(30)
        await self.advance(10)
        self.clock.advance(20)
        sockets[0].incoming.put_nowait({'type': 'pong'})
        await settle()
        self.assertEqual(sockets[0].ping_times, [30, 60])
        with patch('random.uniform', return_value=0):
            await self.advance(10)
        self.assertTrue(sockets[0].closed)

    async def test_public_pong_records_liveness_without_refreshing_prices_or_private(self):
        bot, _ = self.make_bot()
        bot.public_disconnected = True
        bot.book_seen = 1
        bot.engine.mark_ws_seen = 2
        bot.watchdog.pong()
        await self.advance(7)
        await bot.on_public({'type': 'pong'})
        self.assertEqual(getattr(bot, 'public_pong_seen', None), 7)
        self.assertEqual(bot.watchdog.last_seen['PrivateFeed'], 0)
        self.assertEqual(bot.book_seen, 1)
        self.assertEqual(bot.engine.mark_ws_seen, 2)
        self.assertTrue(bot.public_disconnected)

    async def test_public_ping_send_failure_reconnects_without_waiting_for_messages(self):
        bot, sockets = self.make_bot()
        await self.start_feed(bot)
        sockets[0].send_error = OSError('测试公开发送失败')
        with patch('random.uniform', return_value=0):
            await self.advance(30)
        self.assertTrue(sockets[0].closed)
        await self.advance(3)
        self.assertEqual(len(sockets), 2)

    async def test_feed_cancellation_reclaims_public_heartbeat(self):
        bot, sockets = self.make_bot(auto_pong=False)
        task = await self.start_feed(bot)
        await self.advance(30)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self.advance(60)
        self.assertEqual(sockets[0].ping_times, [30])
        self.assertTrue(sockets[0].closed)
        self.assertFalse(sockets[0].reading)
        self.assertEqual(len(sockets), 1)

    async def test_private_renewal_is_independent_of_public_socket_in_every_mode(self):
        for mode in ('dry-run', 'sandbox', 'live'):
            with self.subTest(mode=mode):
                api = Exchange(self.clock, self.files)
                bot, _ = self.make_bot(mode=mode, api=api)
                bot.private_ws = PublicSocket(self.clock, auto_pong=False)
                bot.public_ws = PublicSocket(self.clock, auto_pong=False)
                bot.public_ws.send_error = OSError('测试公开连接失效')
                for name in bot.watchdog.thresholds:
                    if name == 'PrivateFeed':
                        bot.watchdog.pong()
                    else:
                        bot.watchdog.beat(name)
                self.assertTrue(await bot.watchdog.check_once())
                self.assertEqual(bot.private_ws.ping_times, [self.clock.now])
                self.assertEqual(bot.public_ws.ping_times, [])
                self.assertTrue(bot.gate.is_open)


class M17PublicHeartbeatSettingsTests(unittest.TestCase):
    def test_public_ping_defaults_to_thirty_seconds(self):
        self.assertEqual(getattr(MakerSettings(), 'public_ping_s', None), 30)

    def test_public_ping_interval_parses_and_rejects_invalid_values(self):
        self.assertTrue(hasattr(MakerSettings, 'public_ping_s'))
        self.assertEqual(MakerSettings.from_dict({'public_ping_s': '12.5'}).public_ping_s, 12.5)
        for value in (0, -1, True, 'NaN', 'Infinity'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                MakerSettings.from_dict({'public_ping_s': value})


if __name__ == '__main__':
    unittest.main()
