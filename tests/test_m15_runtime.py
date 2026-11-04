"""M15 公开健康状态和独立 REST 轮询生命周期；只用内存依赖。"""

import asyncio
import unittest
from datetime import datetime, timezone
from decimal import Decimal as D

from pendle.maker import Maker, MarketRules
from pendle.maker_config import MakerSettings
from pendle.ondo_api import HttpResponse
from test_maker import MARKET, book
from test_maker_e2e import Alerts, Clock, Files


class M15RuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock, self.alerts = Clock(), Alerts()
        self.calls = []
        self.bot = Maker(MakerSettings(), api=None, alerter=self.alerts,
            rules=MarketRules('.01', '.001'), files=Files(), monotonic=self.clock,
            clock=self.clock.wall, sleep=self.clock.sleep, public_http=self.http)

    def source(self, age=0):
        return datetime.fromtimestamp(self.clock.wall()-age, timezone.utc).isoformat()

    async def http(self, method, url, **kwargs):
        self.calls.append((self.clock(), method, url))
        return HttpResponse(200, {'success': True, 'result': {
            MARKET: {'markPrice': '100', 'lastUpdatedTime': self.source()}}})

    def refresh_book(self):
        self.bot.book, self.bot.book_seen = book(at=int(self.clock.wall()*1000)), self.clock()

    async def publish_mark(self):
        self.refresh_book()
        await self.bot.on_public({'channel': 'markPricesPerps', 'timestamp': self.source(),
            'data': [{'market': MARKET, 'markPrice': '100',
                      'lastUpdatedTime': self.source(), 'inPriceDiscovery': False}]})

    async def test_unchanged_ws_mark_can_quote_at_sixty_second_boundary(self):
        await self.publish_mark()
        self.clock.now += 60
        self.refresh_book()
        await self.bot.tick()
        self.assertTrue(self.bot.qualified, self.bot.last_stop_reason)
        self.assertEqual(self.bot.last_decision.center, D(100))

    async def test_rest_polling_loop_uses_independent_schedule_and_stops(self):
        self.assertTrue(callable(getattr(self.bot, 'mark_rest_loop', None)))
        await self.publish_mark()

        async def sleep(delay):
            self.clock.now += delay
            self.refresh_book()
            if self.clock.now >= 131:
                self.bot.request_stop()
            await asyncio.sleep(0)

        self.bot.sleep = sleep
        await self.bot.mark_rest_loop()
        self.assertEqual([at for at, _, _ in self.calls], [110, 120, 130])

    async def test_concurrent_rest_poll_attempts_share_one_inflight_request(self):
        self.assertTrue(callable(getattr(self.bot, 'poll_mark_rest_once', None)))
        await self.publish_mark()
        self.clock.now += 10
        entered, release = asyncio.Event(), asyncio.Event()

        async def blocked_http(*args, **kwargs):
            entered.set()
            await release.wait()
            return await self.http(*args, **kwargs)

        self.bot.public_http = blocked_http
        task = asyncio.create_task(self.bot.poll_mark_rest_once())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            self.clock.now += 20
            self.assertFalse(await self.bot.poll_mark_rest_once())
        finally:
            release.set()
            await task
        self.assertEqual(len(self.calls), 1)

    async def test_rest_completion_after_stop_cannot_refresh_mark(self):
        self.assertTrue(callable(getattr(self.bot, 'poll_mark_rest_once', None)))
        await self.publish_mark()
        self.clock.now += 10

        async def stopping_http(*args, **kwargs):
            self.bot.request_stop()
            return await self.http(*args, **kwargs)

        self.bot.public_http = stopping_http
        self.assertFalse(await self.bot.poll_mark_rest_once())
        self.assertIsNone(self.bot.engine.mark_rest_seen)
