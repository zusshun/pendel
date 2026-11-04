"""M9 price discovery state requires ongoing confirmation through valid boolean fields, using in-memory dependencies."""

import asyncio
import logging
import unittest
from dataclasses import replace
from datetime import datetime
from decimal import Decimal as D
from zoneinfo import ZoneInfo

from pendle.alerts import Alerter, TelegramCredentials
from pendle.maker import Maker, MarketRules
from pendle.maker_config import MakerSettings
from pendle.maker_orders import Order, OrdersBlocked
from test_maker import MARKET, book
from test_maker_e2e import Alerts, Clock, Exchange, Files


MISSING = object()


class PriceDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock, self.files, self.alerts = Clock(), Files(), Alerts()
        self.exchange = Exchange(self.clock, self.files)
        self.settings = replace(MakerSettings(), mode='sandbox', quiet_s=.01)
        self.bot = Maker(self.settings, api=self.exchange, alerter=self.alerts,
            rules=MarketRules('.01', '.001'), files=self.files,
            monotonic=self.clock, clock=self.clock.wall, sleep=self.clock.sleep,
            logger=logging.getLogger(self.id()))
        self.bot.logger.addHandler(logging.NullHandler())
        self.exchange.on_quote = self.refresh_book
        await self.bot.startup()
        self.bot.state.deadman_ack()
        await self.bot.reconcile_once()
        self.refresh_book()

    async def asyncTearDown(self):
        await self.bot.shutdown()

    def refresh_book(self):
        bids, asks = [('99.9', '10'), ('99', '10')], [('100.1', '10'), ('101', '10')]
        for row in self.exchange.rows.values():
            if row['status'] in ('OPEN', 'PARTIAL'):
                levels = bids if row['side'] == 'buy' else asks
                levels.append((row['price'], D(row['size'])-D(row['filledSize'])))
        self.bot.book = book(bids=bids, asks=asks, at=int(self.clock.wall()*1000))
        self.bot.book_seen = self.clock()

    async def mark(self, discovery=MISSING, price='100'):
        source_time = datetime.fromtimestamp(self.clock.wall(), ZoneInfo('UTC')).isoformat()
        row = {'market': MARKET, 'markPrice': price, 'lastUpdatedTime': source_time}
        if discovery is not MISSING:
            row['inPriceDiscovery'] = discovery
        try:
            await self.bot.on_public({'channel': self.settings.mark_channel,
                                     'timestamp': source_time, 'data': [row]})
        except ValueError as exc:
            self.fail('价格发现字段异常必须停报并告警，不能只抛异常：' + str(exc))

    def decision(self):
        return self.bot.engine.evaluate(self.bot.book, self.bot.state,
                                        self.bot.rules, now=self.clock())

    def assert_both_blocked(self):
        decision = self.decision()
        self.assertFalse(decision.eligible, decision.reason)
        self.assertFalse(decision.quotes)
        self.assertIn('价格发现', decision.reason)
        for side, price in (('buy', '99.88'), ('sell', '100.12')):
            with self.subTest(side=side), self.assertRaisesRegex(OrdersBlocked, '价格发现'):
                self.bot.check_order(Order(side, MARKET, side, D(1), D(price)),
                                     self.bot.state)

    async def test_first_mark_without_discovery_blocks_both_sides(self):
        with self.assertLogs(self.bot.logger, level='WARNING') as logs:
            await self.mark()
            self.assert_both_blocked()
        self.assertIsNone(self.bot.engine.in_price_discovery)
        self.assertTrue(any('inPriceDiscovery' in row for row in logs.output))
        self.assertTrue(any('inPriceDiscovery' in row for row in self.alerts.messages))
        self.assertIn('价格发现', self.bot.last_stop_reason)
        self.assertFalse([call for call in self.exchange.calls if call[0] == 'place'])

    async def test_only_explicit_boolean_false_allows_quotes(self):
        self.assert_both_blocked()
        await self.mark(False)
        decision = self.decision()
        self.assertTrue(decision.eligible, decision.reason)
        self.assertEqual({quote.side for quote in decision.quotes}, {'buy', 'sell'})
        for side, price in (('buy', '99.88'), ('sell', '100.12')):
            self.bot.check_order(Order(side, MARKET, side, D(1), D(price)), self.bot.state)
        await self.bot.tick()
        self.assertEqual(len([call for call in self.exchange.calls if call[0] == 'place']), 2)

    async def test_missing_field_preserves_valid_state_without_refreshing_times(self):
        await self.mark(False)
        confirmed_at, mark_at = self.clock(), self.bot.engine.mark[1]
        self.clock.now += 1
        self.refresh_book()
        await self.mark(price='100.01')
        self.assert_both_blocked()
        self.assertIs(self.bot.engine.in_price_discovery, False)
        self.assertEqual(self.bot.engine.price_discovery_seen, confirmed_at)
        self.assertEqual(self.bot.engine.mark[1], mark_at)

    async def test_missing_field_does_not_extend_discovery_freshness(self):
        await self.mark(False)
        confirmed_at = self.clock()
        for age in (self.settings.discovery_quiet_max_s/2,
                    self.settings.discovery_quiet_max_s+.001):
            self.clock.now = confirmed_at + age
            self.refresh_book()
            await self.mark()
        self.assert_both_blocked()
        self.assertEqual(self.bot.engine.price_discovery_seen, confirmed_at)
        self.assertEqual(self.bot.engine.mark[1], confirmed_at)

    async def test_stale_discovery_blocks_even_when_mark_is_fresh(self):
        await self.mark(False)
        confirmed_at = self.clock()
        self.clock.now = confirmed_at + self.settings.discovery_quiet_max_s
        self.refresh_book()
        self.bot.engine.mark = (D(100), self.clock())
        self.bot.engine.mark_ws_seen = self.clock()
        self.bot.engine.mark_source_times = (self.clock.wall(), self.clock.wall())
        self.assertTrue(self.decision().eligible)
        self.clock.now += .001
        self.refresh_book()
        self.bot.engine.mark = (D(100), self.clock())
        self.bot.engine.mark_ws_seen = self.clock()
        self.bot.engine.mark_source_times = (self.clock.wall(), self.clock.wall())
        self.assert_both_blocked()
        await self.bot.tick()
        self.assertIn('价格发现', self.bot.last_stop_reason)
        await self.mark(False)
        self.assertTrue(self.decision().eligible)

    async def assert_mark_cancels_both(self, discovery):
        await self.mark(False)
        await self.bot.tick()
        self.assertEqual(len(self.bot.state.active_orders()), 2)
        await self.mark(discovery)
        self.assert_both_blocked()
        self.assertFalse(self.bot.state.active_orders())
        self.assertFalse(await self.exchange.get_open_orders(MARKET))
        self.assertEqual(self.bot.guard.state, 'COOLDOWN')

    async def test_true_stops_quotes_and_cancels_both_sides(self):
        await self.assert_mark_cancels_both(True)

    async def test_missing_field_stops_and_cancels_both_sides(self):
        await self.assert_mark_cancels_both(MISSING)

    async def test_invalid_type_stops_and_cancels_both_sides(self):
        await self.assert_mark_cancels_both('false')

    async def test_invalid_types_stop_both_sides_and_alert(self):
        for value in (None, 'false', 'true', 0, 1, [], {}):
            with self.subTest(value=value):
                await self.mark(False)
                confirmed_at, mark_at = self.clock(), self.bot.engine.mark[1]
                alerts_before = len(self.alerts.messages)
                self.clock.now += .1
                self.refresh_book()
                with self.assertLogs(self.bot.logger, level='WARNING') as logs:
                    await self.mark(value)
                self.assert_both_blocked()
                self.assertIs(self.bot.engine.in_price_discovery, False)
                self.assertEqual(self.bot.engine.price_discovery_seen, confirmed_at)
                self.assertEqual(self.bot.engine.mark[1], mark_at)
                self.assertTrue(any('inPriceDiscovery' in row for row in logs.output))
                self.assertTrue(any('inPriceDiscovery' in row
                                    for row in self.alerts.messages[alerts_before:]))

    async def test_missing_field_cannot_clear_previous_true(self):
        confirmed_at = self.clock()
        await self.mark(True)
        self.clock.now += 1
        self.refresh_book()
        await self.mark()
        self.assert_both_blocked()
        self.assertIs(self.bot.engine.in_price_discovery, True)
        self.assertEqual(getattr(self.bot.engine, 'price_discovery_seen', None), confirmed_at)

    async def test_fresh_false_recovers_from_invalid_input(self):
        await self.mark(False)
        await self.mark('false')
        self.assert_both_blocked()
        await self.mark(False)
        self.assertTrue(self.decision().eligible)

    async def test_dry_run_also_requires_fresh_explicit_false(self):
        self.bot.settings = replace(self.settings, mode='dry-run')
        self.bot.engine.settings = self.bot.settings
        self.assertFalse(self.decision().eligible)
        await self.mark(False)
        self.assertTrue(self.decision().eligible)
        self.clock.now += self.settings.discovery_quiet_max_s+.001
        self.refresh_book()
        self.bot.engine.mark = (D(100), self.clock())
        self.bot.engine.mark_ws_seen = self.clock()
        self.bot.engine.mark_source_times = (self.clock.wall(), self.clock.wall())
        self.assertFalse(self.decision().eligible)

    async def test_missing_and_invalid_alerts_share_five_minute_merge(self):
        sent = []
        # Isolate field alerts so successful cancellation notifications do not consume the same global send window.
        self.bot.settings = replace(self.settings, mode='dry-run')
        self.bot.engine.settings = self.bot.settings

        async def http(method, url, **kwargs):
            sent.append(kwargs['json']['text'])
            return {'ok': True}

        alerter = Alerter(TelegramCredentials('测试-token', '测试-chat'),
                          http=http, monotonic=self.clock)
        self.bot.alerter = alerter
        started = self.clock()
        for age, value in ((0, MISSING), (1, 'false'), (299.999, MISSING)):
            self.clock.now = started + age
            await self.mark(value)
            for _ in range(3):
                await asyncio.sleep(0)
        self.assertEqual(len(sent), 1)
        self.assertIn('inPriceDiscovery', sent[0])
        self.assertEqual(len(alerter._pending), 1, '缺失和类型错误应使用同一合并键')
        self.clock.now = started + 300
        await self.mark(0)
        for _ in range(3):
            await asyncio.sleep(0)
        self.assertEqual(len(sent), 2)


if __name__ == '__main__':
    unittest.main()
