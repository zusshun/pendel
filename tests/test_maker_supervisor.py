"""监督循环通过内存交易所与内存 WS 验证，不连接网络或端口。"""

import asyncio
import json
import logging
import unittest
from dataclasses import replace
from datetime import datetime
from decimal import Decimal as D
from zoneinfo import ZoneInfo

from pendle.maker import Maker, MarketRules
from pendle.maker_config import MakerSettings
from pendle.maker_transport import build_maker
from pendle.ondo_api import Credentials, HttpResponse, OndoAPI
from test_maker import MARKET, book
from test_maker_e2e import live_deadman_ack, Alerts, Clock, Exchange, Files, publish_exchange_book
from test_ondo_api import FakeClock, settle


class WS:
    def __init__(self):
        self.sent, self.incoming, self.closed = [], asyncio.Queue(), False
        self.on_send = None

    async def send(self, message):
        self.sent.append(json.loads(message))
        if self.on_send is not None and self.sent[-1].get('timeout_seconds') == 0:
            await self.on_send(live_deadman_ack(0))

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.incoming.get()

    async def close(self):
        self.closed = True


class MakerSupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock, self.files, self.alerts = Clock(), Files(), Alerts()
        self.exchange = Exchange(self.clock, self.files)
        self.settings = replace(MakerSettings(), mode='sandbox', quiet_s=.01)
        self.public_ws, self.private_ws, self.connections = WS(), WS(), []

        async def connect(url, **kwargs):
            self.connections.append((url, kwargs))
            return self.public_ws if len(self.connections) == 1 else self.private_ws

        self.bot = Maker(self.settings, api=self.exchange, alerter=self.alerts,
            rules=MarketRules(D('.01'), D('.001')), files=self.files,
            monotonic=self.clock, clock=self.clock.wall, sleep=self.clock.sleep, connect=connect)
        self.private_ws.on_send = self.bot.on_private
        self.exchange.on_quote = self.publish_own_orders
        await self.bot.startup()
        self.feeds = []

    def publish_own_orders(self):
        publish_exchange_book(self.bot,self.exchange,self.clock)

    async def asyncTearDown(self):
        self.bot.request_stop()
        for task in self.feeds:
            task.cancel()
        await asyncio.gather(*self.feeds, return_exceptions=True)
        await self.bot.shutdown()

    async def start_feeds(self):
        # 消息顺序测试不推进定时器，避免后台心跳自行快进共享时钟。
        self.bot.sleep = FakeClock().sleep
        self.feeds.append(asyncio.create_task(self.bot.feed()))
        await settle()
        self.feeds.append(asyncio.create_task(self.bot.feed(True)))
        await settle()

    def snapshot(self, market=MARKET, price='100'):
        center = D(price)
        return {'channel': 'depthBooksPerps', 'data': [{
            'market': market, 'time': datetime.fromtimestamp(
                self.clock.wall(), ZoneInfo('UTC')).isoformat(),
            'bids': [[str(center-D('.1')), '10'], [str(center-1), '10']],
            'asks': [[str(center+D('.1')), '10'], [str(center+1), '10']]}]}

    async def publish_snapshot(self, market=MARKET, price='100'):
        """有效行情同时推送盘口和实际标记价格频道。"""
        await self.public_ws.incoming.put(self.snapshot(market, price))
        source_time = datetime.fromtimestamp(self.clock.wall(), ZoneInfo('UTC')).isoformat()
        await self.public_ws.incoming.put({'channel': self.settings.mark_channel,
            'timestamp': source_time,
            'data': [{'market': market, 'markPrice': price, 'inPriceDiscovery': False,
                      'lastUpdatedTime': source_time}]})

    async def test_login_subscriptions_and_ack_gate_real_feed_ordering(self):
        await self.start_feeds()
        self.assertEqual(self.private_ws.sent, [self.exchange.ws_login()])
        await self.private_ws.incoming.put({'op': 'loggedIn'})
        await settle()
        channels = [row['channel'] for row in self.private_ws.sent if row.get('op') == 'subscribe']
        self.assertEqual(channels, ['ordersPerps', 'fillsPerps', 'positionsPerps',
                                    'balancePerps', 'cancelAllOrdersAfterPerps'])
        self.assertEqual(next(row['timeout_seconds'] for row in self.private_ws.sent
                              if row.get('channel') == 'cancelAllOrdersAfterPerps'), 30)
        await self.publish_snapshot()
        await settle()
        await self.bot.tick()
        self.assertFalse(self.bot.state.deadman_acked)
        self.assertFalse([call for call in self.exchange.calls if call[0] == 'place'])
        await self.private_ws.incoming.put(live_deadman_ack(self.settings.deadman_timeout_s))
        await settle()
        self.assertTrue(self.bot.state.deadman_acked)
        self.assertTrue(await self.bot.reconcile_once())
        self.bot.book_seen = self.clock()
        await self.bot.tick()
        self.assertEqual(len([call for call in self.exchange.calls if call[0] == 'place']), 2)

    async def test_one_public_one_private_and_all_market_filtering(self):
        await self.start_feeds()
        self.assertEqual(len(self.connections), 2)
        self.assertTrue(all(url == self.settings.ws_url for url, _ in self.connections))
        subscriptions = [row for row in self.public_ws.sent if row.get('op') == 'subscribe']
        self.assertEqual({row['channel'] for row in subscriptions},
                         {'depthBooksPerps', 'tradesPerps', self.settings.mark_channel})
        self.assertTrue(all('market' not in row for row in subscriptions))
        await self.publish_snapshot('AAPL-USD.P', '50')
        await settle()
        self.assertIsNone(self.bot.book)
        await self.publish_snapshot()
        await settle()
        self.assertEqual(self.bot.book.mid, 100)
        await self.publish_snapshot('AAPL-USD.P', '50')
        await settle()
        self.assertEqual(self.bot.book.mid, 100)
        self.assertEqual(len(self.connections), 2)

    async def test_ack_does_not_block_following_private_balance_push(self):
        blocked = asyncio.Event()

        async def blocked_reconciliation(*args, **kwargs):
            await blocked.wait()
            return []

        self.exchange.get_fills = blocked_reconciliation
        await self.start_feeds()
        await self.private_ws.incoming.put({'op': 'loggedIn'})
        await self.private_ws.incoming.put(live_deadman_ack(self.settings.deadman_timeout_s))
        await self.private_ws.incoming.put({'channel': 'balancePerps', 'data': [{
            'marginBalance': '5000', 'availableMargin': '4000', 'usedMargin': '100',
            'marginRatio': '.02', 'underLiquidation': False}]})
        await settle()
        self.assertEqual(self.bot.state.balances.get('equity'), '5000')
        self.assertTrue(self.bot.reconcile_event.is_set())
        self.assertFalse([call for call in self.exchange.calls if call[0] == 'place'])


class RealAPISupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock, self.files, self.sent = FakeClock(), Files(), []
        self.weekend = Clock()
        self.wall = lambda: self.weekend.wall() + self.clock.monotonic()

    async def drive(self, task, budget=40):
        for _ in range(budget):
            await settle()
            if task.done():
                return await task
            self.clock.advance(.25)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.fail('内存请求未在测试预算内结束')

    def make_api(self, network):
        return OndoAPI(Credentials('key', 'secret'), transport=network,
            clock=self.wall, monotonic=self.clock.monotonic, sleep=self.clock.sleep)

    async def test_real_api_429_prioritizes_maker_guard_cancel_before_queued_reads(self):
        async def network(request):
            self.sent.append(request)
            if request.method == 'POST':
                return HttpResponse(429, {'error_code': 'rate_limited'}, {'Retry-After': '3'})
            return HttpResponse(200, {'success': True, 'result': []})

        api = self.make_api(network)
        bot = Maker(replace(MakerSettings(), mode='sandbox'), api=api, alerter=Alerts(),
            files=self.files, rules=MarketRules(D('.01'), D('.001')),
            clock=self.wall, monotonic=self.clock.monotonic, sleep=self.clock.sleep)
        await bot.startup()
        bot.state.deadman_ack()
        bot.state.modes[MARKET] = 'RUNNING'
        bot.state.apply_balance_snapshot(dict(equity='10000', available='9000',
            margin_ratio='.01', under_liquidation=False, time_ms=int(self.wall()*1000)))
        bot.book, bot.book_seen = book(at=int(self.wall()*1000)), self.clock.monotonic()
        bot.engine.mark = (D(100), self.clock.monotonic())
        bot.engine.in_price_discovery = False
        bot.engine.price_discovery_seen = self.clock.monotonic()
        tick = asyncio.create_task(bot.tick())
        read = None
        try:
            await settle()
            self.assertEqual([row.method for row in self.sent], ['POST'])
            read = asyncio.create_task(api.request('GET', '/v1/markets'))
            await settle()
            self.clock.advance(1)
            await settle()
            self.assertEqual([row.method for row in self.sent], ['POST', 'DELETE'])
            self.assertFalse(read.done())
            self.assertEqual(self.sent[-1].path_query, '/v1/perps/orders?market=IBM-USD.P')
            await self.drive(tick)
            await self.drive(read)
            self.assertEqual(sum(row.method == 'POST' for row in self.sent), 1)
            self.assertEqual(bot.guard.state, 'COOLDOWN')
            self.assertFalse(bot.state.active_orders())
        finally:
            for task in (tick, read):
                if task is not None:
                    task.cancel()
            await asyncio.gather(*(task for task in (tick, read) if task is not None),
                                 return_exceptions=True)
            await self.drive(asyncio.create_task(bot.shutdown()))

    async def test_default_builder_startup_loads_precision_leverage_and_stops_safely(self):
        async def network(request):
            self.sent.append(request)
            path = request.path_query.partition('?')[0]
            if path == '/v1/markets':
                result = [dict(market=MARKET, quoteIncrement='.05', baseIncrement='.002', minNotional='2')]
            elif path == '/v1/perps/leverage':
                result = [dict(market=MARKET, leverage='10')]
            else:
                result = []
            return HttpResponse(200, {'success': True, 'result': result})

        bot = build_maker(replace(MakerSettings(), mode='sandbox'),
            env={'ONDO_API_KEY': 'key', 'ONDO_API_SECRET': 'secret'},
            alerter=Alerts(), transport=network, files=self.files,
            logger=logging.getLogger('test.maker.supervisor'), clock=self.wall,
            monotonic=self.clock.monotonic, sleep=self.clock.sleep)
        await self.drive(asyncio.create_task(bot.startup()))
        self.assertEqual(bot.rules.tick, D('.05'))
        self.assertEqual(bot.rules.increment, D('.002'))
        self.assertEqual(bot.rules.min_notional, D(2))
        self.assertEqual(bot.initial_margin_rate, D('.1'))
        self.assertEqual([row.path_query for row in self.sent],
                         ['/v1/markets', '/v1/perps/leverage?market=IBM-USD.P'])
        bot.request_stop(15)
        await self.drive(asyncio.create_task(bot.shutdown()))
        self.assertFalse(bot.gate.is_open)
        self.assertIsNone(bot.instance.handle)
        self.assertEqual([row.method for row in self.sent], ['GET', 'GET', 'DELETE', 'GET'])
