"""M20: the real live builder starts successfully with empty account samples and begins two-sided quoting."""

import asyncio
import copy
import json
import logging
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal as D
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import patch

from pendle.maker_config import MakerSettings
from pendle.maker_transport import build_maker
from pendle.market import timestamp_ms
from pendle.ondo_api import HttpResponse
from test_maker_e2e import Files
from test_ondo_api import FakeClock, settle


SAMPLES = Path(__file__).parent / 'fixtures' / 'live_samples'
MARKET = 'IBM-USD.P'


def sample(name):
    return json.loads((SAMPLES / (name + '.json')).read_text())


class SimulatedWS:
    """Deliver exchange messages only through the WS consumer loop, preserving the full send and ACK sequence."""

    def __init__(self, exchange, *, private):
        self.exchange, self.private = exchange, private
        self.sent, self.incoming, self.closed = [], asyncio.Queue(), False

    async def send(self, raw):
        message = json.loads(raw)
        self.sent.append(message)
        fixtures = self.exchange.private_samples
        if message.get('op') == 'login':
            await self.incoming.put(copy.deepcopy(fixtures['loggedIn']))
        elif message.get('op') == 'ping':
            await self.incoming.put(copy.deepcopy(fixtures['pong']))
        elif self.private and message.get('channel') == 'cancelAllOrdersAfterPerps':
            timeout = message['timeout_seconds']
            self.exchange.events.append(('死人开关请求', timeout))
            # Deliver the enable ACK explicitly to verify that quoting cannot bypass the ACK barrier.
            if timeout == 0:
                await self.incoming.put(copy.deepcopy(fixtures['deadman_ack_disable']))
        elif self.private and message.get('op') == 'subscribe':
            channel = message['channel']
            key = {'ordersPerps': 'subscribed_orders', 'fillsPerps': 'subscribed_fills',
                   'positionsPerps': 'subscribed_positions', 'balancePerps': 'subscribed_balance'}[channel]
            await self.incoming.put(copy.deepcopy(fixtures[key]))
            if channel == 'positionsPerps':
                await self.incoming.put(copy.deepcopy(fixtures['positions_update_empty']))
            elif channel == 'balancePerps':
                await self.incoming.put(copy.deepcopy(fixtures['balance_update']))
        elif not self.private and message.get('op') == 'subscribe':
            await self.incoming.put({'type': 'subscribed', 'channel': message['channel']})
            if message['channel'] == 'markPricesPerps':
                await self.exchange.publish_market()

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self.incoming.get()
        if self.private and message == self.exchange.private_samples['deadman_ack_enable']:
            self.exchange.events.append(('死人开关开启ACK', 30))
        return json.dumps(message)

    async def close(self):
        self.closed = True


class LiveStartupExchange:
    """Mock the network boundary while preserving real REST response shapes for empty lists, balances, and metadata."""

    def __init__(self, clock):
        self.clock, self.private_samples = clock, sample('ws_private_samples')
        self.epoch = timestamp_ms(self.private_samples['loggedIn']['timestamp']) / 1000
        self.requests, self.http_calls, self.events, self.remote = [], [], [], {}
        self.public_ws, self.private_ws = SimulatedWS(self, private=False), SimulatedWS(self, private=True)
        self.connections, self.ntp_checks, self.bot = [], 0, None

    def wall(self):
        return self.epoch + self.clock.monotonic()

    def ntp_check(self):
        self.ntp_checks += 1
        return True

    async def connect(self, url, **kwargs):
        self.connections.append((url, kwargs))
        return self.public_ws if len(self.connections) == 1 else self.private_ws

    async def http(self, method, url, **kwargs):
        self.http_calls.append((method, url, kwargs))
        return HttpResponse(200, {'ok': True})

    async def publish_market(self):
        """Market data still uses the WS receiver; a shallow book reduces competing volume and includes the mock exchange's own resting orders."""
        source_time = datetime.fromtimestamp(self.wall(), timezone.utc).isoformat()
        depth, mark = sample('ws_depthBooksPerps'), sample('ws_markPricesPerps')
        depth['timestamp'] = depth['data'][0]['time'] = source_time
        depth['data'][0]['bids'] = [['247.80', '.01'], ['247.00', '.01']]
        depth['data'][0]['asks'] = [['247.84', '.01'], ['248.70', '.01']]
        for row in self.remote.values():
            side = 'bids' if row['side'] == 'buy' else 'asks'
            depth['data'][0][side].append([row['price'], row['size']])
        mark['timestamp'] = mark['data'][0]['lastUpdatedTime'] = source_time
        await self.public_ws.incoming.put(depth)
        await self.public_ws.incoming.put(mark)

    async def transport(self, request):
        self.requests.append(request)
        path = request.path_query.partition('?')[0]
        if path == '/v1/markets':
            payload = sample('v1_markets_ibm')
        elif request.method == 'POST':
            body = json.loads(request.body)
            if not self.bot.state.deadman_acked or self.bot.last_reconcile_at is None:
                raise AssertionError('未收到死人开关 ACK 或未完成对账时发送了报价')
            self.events.append(('报价', body['side']))
            row = {**body, 'orderId': body['clientOrderId'], 'filledSize': '0',
                   'status': 'OPEN', 'time_ms': int(self.wall() * 1000)}
            self.remote[body['clientOrderId']] = row
            await self.publish_market()
            await settle()
            payload = {'success': True, 'result': row}
        elif request.method == 'DELETE':
            self.remote.clear()
            await self.publish_market()
            payload = {'success': True, 'result': {}}
        else:
            if path == '/v1/perps/leverage':
                result = [{'market': MARKET, 'leverage': '10'}]
            elif path == '/v1/perps/positions':
                self.events.append(('空持仓REST', None))
                result = []
            elif path == '/v1/perps/orders':
                result = list(self.remote.values())
            elif path == '/v1/perps/fills':
                result = []
            elif path == '/v1/perps/balance':
                result = copy.deepcopy(self.private_samples['balance_update']['data'])
            else:
                raise AssertionError('出现未模拟的 REST 路径：' + path)
            payload = {'success': True, 'result': result}
        headers = {'Date': format_datetime(datetime.fromtimestamp(self.wall(), timezone.utc), usegmt=True)}
        return HttpResponse(200, payload, headers)


class M20LiveStartupTests(unittest.IsolatedAsyncioTestCase):
    async def drive_until(self, predicate, *, seconds=6):
        for _ in range(int(seconds / .01)):
            await settle()
            if predicate():
                return
            self.clock.advance(.01)

    async def test_live_empty_account_starts_reconciles_and_quotes_after_real_deadman_ack(self):
        self.clock, files = FakeClock(), Files()
        exchange = LiveStartupExchange(self.clock)
        settings = replace(MakerSettings(), mode='live', guards_calibrated=True,
            sandbox_verified=True, healthcheck_url='https://health.invalid/m20',
            quote_notional=D('10'), quiet_s=.02, rate_per_second=1000.,
            strategy_interval_s=.05, window_start_day=2, window_start_time='00:00',
            window_end_day=3, window_end_time='23:59')
        bot = build_maker(settings, env={'ONDO_API_KEY': 'm20-key',
            'ONDO_API_SECRET': 'm20-secret', 'TELEGRAM_BOT_TOKEN': '123:m20-token',
            'TELEGRAM_CHAT_ID': '123'}, files=files, transport=exchange.transport,
            connect=exchange.connect, http=exchange.http, ntp_check=exchange.ntp_check,
            logger=logging.getLogger('test.m20.live'), clock=exchange.wall,
            monotonic=self.clock.monotonic, sleep=self.clock.sleep)
        exchange.bot = bot
        with patch('socket.socket.connect', side_effect=AssertionError('测试禁止联网')):
            task = asyncio.create_task(bot.run(live=True))
            try:
                await self.drive_until(lambda: bot.deadman_requested and bot.engine.mark is not None)
                # Let the normal run loop evaluate a complete market snapshot, then verify that the ACK barrier still blocks orders.
                market_ready_at = self.clock.monotonic()
                await self.drive_until(lambda: bot.decision_seen is not None
                    and bot.decision_seen > market_ready_at, seconds=1)
                self.assertIsNotNone(bot.last_decision, '有效行情必须进入真实策略节拍')
                self.assertTrue(bot.last_decision.eligible, bot.last_decision.reason)
                self.assertEqual({quote.side for quote in bot.last_decision.quotes}, {'buy', 'sell'})
                self.assertTrue(bot.started)
                self.assertTrue(bot.api.clock_verified)
                self.assertEqual(exchange.ntp_checks, 1)
                self.assertTrue(bot.private_logged_in)
                self.assertTrue(bot.deadman_requested)
                self.assertFalse(bot.state.deadman_acked)
                self.assertNotEqual(bot.state.market_mode(MARKET), 'RUNNING')
                self.assertFalse([r for r in exchange.requests if r.method == 'POST'])
                self.assertEqual(D(bot.state.balances['equity']), 300)
                self.assertEqual(exchange.events, [('死人开关请求', 30)])
                await exchange.private_ws.incoming.put(
                    copy.deepcopy(exchange.private_samples['deadman_ack_enable']))
                await self.drive_until(lambda: len(exchange.remote) == 2 or task.done())
                self.assertIsNotNone(bot.last_reconcile_at,
                    '真实 REST positions result: [] 必须完成启动对账：' +
                    str(bot.state.sync_reasons.get(MARKET)))
                self.assertEqual(bot.state.positions[MARKET], 0)
                self.assertEqual(bot.state.position_details[MARKET]['direction'], 'neutral')
                self.assertEqual(bot.state.market_mode(MARKET), 'RUNNING')
                self.assertTrue(bot.state.deadman_acked)
                self.assertEqual({row['side'] for row in exchange.remote.values()}, {'buy', 'sell'})
                enabled = exchange.events.index(('死人开关请求', 30))
                acked = exchange.events.index(('死人开关开启ACK', 30))
                quotes = [i for i, row in enumerate(exchange.events) if row[0] == '报价']
                self.assertTrue(all(enabled < acked < i for i in quotes))
                self.assertFalse(files.exists(settings.halt_path))
                self.assertFalse(bot._stop_failed)
                self.assertEqual(len(exchange.connections), 2)
                self.assertTrue(all(url == settings.prod_ws_url for url, _ in exchange.connections))
                self.assertTrue(all(r.url.startswith(settings.prod_rest_url) for r in exchange.requests))
            finally:
                bot.request_stop()
                await self.drive_until(task.done)
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self.assertEqual(task.result(), 0)
            self.assertTrue(bot.deadman_disabled)
            self.assertFalse(exchange.remote)
            self.assertFalse(files.exists(settings.halt_path))
