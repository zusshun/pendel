"""M16 标记价格按源时间单调接收；迟到消息不能延长有效期。"""

import asyncio
import logging
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal as D
from unittest.mock import patch

from pendle.alerts import Alerter, TelegramCredentials
from pendle.maker import Maker, MarketRules
from pendle.maker_config import MakerSettings
from pendle.maker_orders import OrderStatus
from pendle.ondo_api import HttpResponse
from test_m13_center import MARKET, depth
from test_maker_e2e import Alerts, Clock, Exchange, Files


class M16MarkOrderingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.reset_bot()
        network = patch('socket.socket.connect',
                        side_effect=AssertionError('M16 测试禁止联网'))
        network.start()
        self.addCleanup(network.stop)

    def reset_bot(self, *, sandbox=False):
        self.clock, self.alerts, self.files = Clock(), Alerts(), Files()
        self.exchange = Exchange(self.clock, self.files)
        self.settings = replace(MakerSettings(), mode='sandbox' if sandbox else 'dry-run',
            window_start_day=0, window_start_time='00:00',
            window_end_day=6, window_end_time='23:59')
        logger = logging.getLogger(self.id())
        logger.addHandler(logging.NullHandler())
        self.bot = Maker(self.settings, api=self.exchange if sandbox else None,
            alerter=self.alerts, rules=MarketRules('.01', '.001'), files=self.files,
            public_http=self.http, monotonic=self.clock, clock=self.clock.wall,
            sleep=self.clock.sleep, logger=logger)
        self.rest_row = {}
        self.healthy_book()

    def source_time(self, age=0):
        return datetime.fromtimestamp(self.clock.wall()-age, timezone.utc).isoformat()

    def healthy_book(self):
        self.bot.book = depth(now=self.clock.wall())
        self.bot.book_seen = self.clock()

    async def http(self, method, url, **kwargs):
        return HttpResponse(200, {'success': True, 'result': {MARKET: self.rest_row}})

    async def ws(self, price='100', *, source=None, discovery=False):
        await self.bot.on_public({'channel': self.settings.mark_channel,
            'timestamp': self.source_time(), 'data': [{'market': MARKET,
            'markPrice': price, 'oraclePrice': price, 'inPriceDiscovery': discovery,
            'lastUpdatedTime': source or self.source_time()}]})

    async def rest(self, price='100', *, source=None):
        self.rest_row = {'markPrice': price, 'oraclePrice': price,
                         'lastUpdatedTime': source or self.source_time()}
        return await self.bot.poll_mark_rest_once()

    def snapshot(self):
        fields = ('mark', 'oracle', 'mark_source_times', 'discovery_source_times',
                  'mark_ws_seen', 'mark_rest_seen', 'in_price_discovery',
                  'price_discovery_seen', 'price_discovery_invalid')
        return {field: getattr(self.bot.engine, field) for field in fields}

    async def test_rest_100_14_then_older_ws_100_cannot_send_sell_100_12(self):
        self.reset_bot(sandbox=True)
        await self.bot.startup()
        self.addAsyncCleanup(self.bot.shutdown)
        self.bot.state.deadman_ack()
        await self.bot.reconcile_once()
        old_source = self.source_time()
        await self.ws(source=old_source)
        self.clock.now += 11
        self.assertTrue(await self.rest('100.14'))
        self.clock.now += 1
        self.healthy_book()
        await self.ws('100', source=old_source)

        # 真实订单管理器调用发送前检查，模拟交易所不能受理复审中的卖价。
        order = self.bot.manager.create_intent(MARKET, 'sell', D(1), D('100.12'))
        await self.bot.manager.submit(order)
        self.assertEqual([call for call in self.exchange.calls if call[0] == 'place'], [])
        self.assertEqual(order.status, OrderStatus.REJECTED)
        self.assertEqual(self.bot.engine.mark[0], D('100.14'))
        decision = self.bot.engine.evaluate(self.bot.book, self.bot.state,
                                            self.bot.rules, now=self.clock())
        self.assertTrue(decision.eligible, decision.reason)
        self.assertGreater(next(q.price for q in decision.quotes if q.side == 'sell'),
                           D('100.14'))

    async def test_older_ws_only_counts_without_changing_price_or_any_validity_clock(self):
        for price in ('100', '100.14'):
            with self.subTest(price=price):
                self.reset_bot()
                source = self.source_time()
                await self.ws()
                self.clock.now += 11
                self.assertTrue(await self.rest('100.14'))
                before, alerts = self.snapshot(), len(self.alerts.messages)
                self.clock.now += 1
                await self.ws(price, source=source, discovery=True)
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(len(self.alerts.messages), alerts)
                self.assertEqual(getattr(self.bot, 'mark_older_counts', {}).get('ws', 0), 1)

    async def test_older_rest_only_counts_even_when_price_matches_current_confirmation(self):
        for price in ('100', '100.14'):
            with self.subTest(price=price):
                self.reset_bot()
                source = self.source_time()
                await self.ws()
                self.clock.now += 11
                self.assertTrue(await self.rest('100.14'))
                before, alerts = self.snapshot(), len(self.alerts.messages)
                self.clock.now += 11
                self.assertFalse(await self.rest(price, source=source))
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(len(self.alerts.messages), alerts)
                self.assertEqual(getattr(self.bot, 'mark_older_counts', {}).get('rest', 0), 1)

    async def test_newer_ws_cannot_be_replaced_or_confirmed_by_older_rest(self):
        source = self.source_time()
        await self.ws()
        self.clock.now += 1
        await self.ws('100.14')
        before = self.snapshot()
        self.clock.now += 11
        self.assertFalse(await self.rest('100', source=source))
        self.assertEqual(self.snapshot(), before)
        self.assertIsNone(self.bot.engine.mark_rest_seen)

    async def test_older_ws_cannot_extend_rule_a_after_sixty_seconds(self):
        await self.ws()
        self.clock.now += 61
        self.healthy_book()
        await self.ws(source=self.source_time(62))
        self.assertFalse(self.bot.engine.mark_is_fresh(self.clock()))
        self.assertEqual(self.bot.engine.mark_ws_seen, 100)

    async def test_older_rest_cannot_extend_rule_b_after_fifteen_seconds(self):
        source = self.source_time()
        await self.ws()
        self.clock.now += 61
        self.assertTrue(await self.rest('100.14'))
        self.clock.now += 16
        self.healthy_book()
        self.assertFalse(await self.rest('100.14', source=source))
        self.assertFalse(self.bot.engine.mark_is_fresh(self.clock()))
        self.assertEqual(self.bot.engine.mark_rest_seen, 161)

    async def test_older_discovery_cannot_change_state_or_refresh_confirmation(self):
        for current in (False, True):
            with self.subTest(current=current):
                self.reset_bot()
                await self.ws(discovery=current)
                before, alerts = self.snapshot(), len(self.alerts.messages)
                self.clock.now += 1
                await self.ws(source=self.source_time(2), discovery=not current)
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(len(self.alerts.messages), alerts)

    async def test_older_discovery_cannot_clear_invalid_state(self):
        await self.ws()
        await self.ws(discovery=None)
        before = self.snapshot()
        self.clock.now += 1
        await self.ws(source=self.source_time(2))
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(self.bot.engine.price_discovery_invalid)

    async def test_invalid_time_does_not_erase_accepted_source_high_water(self):
        for origin in ('ws', 'rest'):
            with self.subTest(origin=origin):
                self.reset_bot()
                await self.ws('100.14')
                await self.ws(source='无法解析')
                before = self.snapshot()
                self.clock.now += 11
                old_source = self.source_time(12)
                if origin == 'ws':
                    await self.ws('100', source=old_source)
                else:
                    self.assertFalse(await self.rest('100', source=old_source))
                self.assertEqual(self.snapshot(), before)
                self.assertFalse(self.bot.engine.mark_is_fresh(self.clock()))

    async def test_equal_source_different_price_uses_last_arrival_for_both_origins(self):
        for first in ('ws', 'rest'):
            for second in ('ws', 'rest'):
                with self.subTest(first=first, second=second):
                    self.reset_bot()
                    source = self.source_time()
                    self.clock.now += 11
                    await getattr(self, first)('100', source=source)
                    self.clock.now += 11
                    await getattr(self, second)('100.01', source=source)
                    self.assertEqual(self.bot.engine.mark[0], D('100.01'))
                    self.assertEqual(self.bot.engine.mark[1], self.clock())
                    self.assertEqual(len(self.alerts.messages), 1)
                    self.assertIn('源时间相同', self.alerts.messages[0])
                    if second == 'ws':
                        self.assertIsNone(self.bot.engine.mark_rest_seen)
                    else:
                        self.assertEqual(self.bot.engine.mark_rest_seen, self.clock())

    async def test_equal_source_same_price_refreshes_without_conflict_alert(self):
        source = self.source_time()
        await self.ws(source=source)
        self.clock.now += 61
        self.assertTrue(await self.rest(source=source))
        self.clock.now += 1
        await self.ws(source=source)
        self.assertEqual(self.bot.engine.mark_ws_seen, self.clock())
        self.assertEqual(self.bot.engine.mark_rest_seen, self.clock()-1)
        self.assertEqual(self.bot.engine.price_discovery_seen, self.clock())
        self.assertEqual(self.alerts.messages, [])

    async def test_equal_source_discovery_uses_last_arrival(self):
        source = self.source_time()
        await self.ws(source=source)
        self.clock.now += 1
        await self.ws(source=source, discovery=True)
        self.assertTrue(self.bot.engine.in_price_discovery)
        self.assertEqual(self.bot.engine.price_discovery_seen, self.clock())
        self.assertIn('价格发现', self.bot.guard.reason)

    async def test_equal_source_conflicts_from_ws_and_rest_share_one_merged_alert(self):
        sent = []

        async def http(method, url, **kwargs):
            sent.append(kwargs['json']['text'])
            return {'ok': True}

        alerter = Alerter(TelegramCredentials('测试-token', '测试-chat'),
                          http=http, monotonic=self.clock)
        self.bot.alerter = alerter
        source = self.source_time()
        await self.ws(source=source)
        for origin, price in (('rest', '100.01'), ('ws', '100.02'), ('rest', '100.03')):
            self.clock.now += 11
            await getattr(self, origin)(price, source=source)
            for _ in range(3):
                await asyncio.sleep(0)
        self.assertEqual(self.bot.engine.mark[0], D('100.03'))
        self.assertEqual(len(sent), 1)
        self.assertEqual(len(alerter._pending), 1)
        self.assertIn('源时间相同', sent[0])


if __name__ == '__main__':
    unittest.main()
