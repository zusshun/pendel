"""Exercise sibling safeguards through the real builder, signed queue, and REST gate."""

import asyncio
import unittest
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import test_m3_wiring as wiring
from pendle.maker_transport import build_maker
from pendle.ondo_api import HttpResponse
from test_maker import MARKET
from test_ondo_api import settle


PEER = 'RDDT-USD.P'


class SiblingFlowTests(unittest.IsolatedAsyncioTestCase):
    wall = wiring.M3WiringTests.wall
    drive = wiring.M3WiringTests.drive
    ready = wiring.M3WiringTests.ready
    publish = wiring.M3WiringTests.publish
    asyncTearDown = wiring.M3WiringTests.asyncTearDown

    async def asyncSetUp(self):
        await wiring.M3WiringTests.asyncSetUp(self)
        original = self.bot
        self.settings = replace(self.settings, sibling_markets=(PEER,),
                                peer_heartbeat_paths={PEER: 'peer/heartbeat.json'})
        self.peer_cancel_failure = False
        self.peer_orders = [{'market': PEER, 'orderId': 'peer-order',
            'clientOrderId': 'peer-order', 'side': 'buy', 'size': '1', 'price': '100',
            'filledSize': '0', 'status': 'OPEN', 'time_ms': int(self.wall()*1000)}]
        self.peer_requests, self.post_heartbeat = [], []

        async def network(request):
            if self.request_market(request) == PEER:
                self.requests.append(request)
                self.peer_requests.append(request)
                if request.method == 'DELETE':
                    if self.peer_cancel_failure:
                        return HttpResponse(503, {'success': False})
                    self.peer_orders.clear()
                    result = {}
                else:
                    result = list(self.peer_orders)
                return HttpResponse(200, {'success': True, 'result': result})
            if request.method == 'POST':
                self.post_heartbeat.append(self.files.exists(self.settings.heartbeat_path))
            return await original.api.transport(request)

        self.bot = build_maker(self.settings, env={
            'ONDO_API_KEY': '测试-key', 'ONDO_API_SECRET': '测试-secret',
            'TELEGRAM_BOT_TOKEN': '测试-token', 'TELEGRAM_CHAT_ID': '测试-chat'},
            transport=network, http=original.alerter._http, connect=original.connect,
            files=self.files, rules=original.rules, logger=original.logger,
            monotonic=self.clock.monotonic, clock=self.wall, sleep=self.clock.sleep)
        self.ws.on_send = self.bot.on_private

    @staticmethod
    def request_market(request):
        return parse_qs(urlsplit(request.path_query).query).get('market', [None])[0]

    async def test_peer_cancel_failure_still_cancels_and_verifies_own_orders_before_permanent_close(self):
        await self.ready()
        self.bot._heartbeat_ready = True
        await self.drive(self.bot.tick())
        self.assertEqual(len(self.remote), 2)
        self.peer_cancel_failure = True
        self.assertFalse(await self.drive(self.bot.check_peer_once(PEER)))
        self.clock.advance(self.settings.peer_cancel_timeout_s)
        self.requests.clear()

        self.assertFalse(await self.drive(self.bot.check_peer_once(PEER)))

        own_requests = [request.method for request in self.requests
                        if self.request_market(request) == MARKET]
        self.assertIn('DELETE', own_requests)
        self.assertIn('GET', own_requests)
        self.assertEqual(own_requests[:2], ['DELETE', 'GET'])
        self.assertFalse(self.remote)
        self.assertTrue(self.bot.halt_result.stopped)
        self.assertEqual(self.bot.exit_code, 76)
        self.assertFalse(self.bot.gate.is_open)
        self.assertTrue(self.bot.stop_event.is_set())
        sent = list(self.ws.sent)
        await self.drive(self.bot.watchdog_loop())
        self.assertEqual(self.ws.sent, sent)
        await self.drive(self.bot.shutdown())
        self.assertFalse(any(row.get('timeout_seconds') == 0 for row in self.ws.sent))

    async def test_peer_kill_cancels_and_verifies_only_peer_market_through_signed_transport(self):
        await self.ready()
        self.bot._heartbeat_ready = True
        await self.drive(self.bot.tick())
        self.assertEqual(len(self.remote), 2)
        own_orders = set(self.remote)

        self.assertTrue(await self.drive(self.bot.check_peer_once(PEER)))

        self.assertEqual([request.method for request in self.peer_requests], ['DELETE', 'GET'])
        self.assertFalse(self.peer_orders)
        self.assertEqual(set(self.remote), own_orders)
        self.assertEqual(self.bot.state.markets, (MARKET,))
        self.assertEqual(set(self.bot.state.orders), own_orders)
        self.assertTrue(self.bot.gate.is_open)
        self.assertIsNone(self.bot.halt_result)

    async def test_healthy_watchdog_publishes_first_heartbeat_before_any_post(self):
        await self.ready()
        await self.drive(self.bot.tick())
        self.assertFalse(self.post_heartbeat)
        self.assertFalse(self.files.exists(self.settings.heartbeat_path))
        for name in self.bot.watchdog.thresholds:
            self.bot.watchdog.last_seen[name] = self.clock.monotonic()
        watchdog = asyncio.create_task(self.bot.watchdog_loop())
        self.background.append(watchdog)

        async def wait_heartbeat():
            while not self.files.exists(self.settings.heartbeat_path):
                await settle()

        await self.drive(wait_heartbeat())
        watchdog.cancel()
        await asyncio.gather(watchdog, return_exceptions=True)
        self.publish()
        await self.drive(self.bot.tick())

        self.assertEqual(self.post_heartbeat, [True, True])
        self.assertEqual(len(self.remote), 2)

    async def test_gate_closing_during_renewal_does_not_publish_healthy_heartbeat(self):
        await self.ready()
        for name in self.bot.watchdog.thresholds:
            self.bot.watchdog.last_seen[name] = self.clock.monotonic()
        ping = self.bot.watchdog.ping

        async def completed_ping_then_closed_gate():
            await ping()
            self.bot.gate.close()

        self.bot.watchdog.ping = completed_ping_then_closed_gate
        await self.drive(self.bot.watchdog_loop())

        self.assertFalse(self.files.exists(self.settings.heartbeat_path))
        self.assertFalse(self.bot._heartbeat_ready)
