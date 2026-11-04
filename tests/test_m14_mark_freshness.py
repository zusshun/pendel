"""M14 offline tests for receiving mark prices and price discovery status and validating source timestamps."""

import asyncio
import json
import logging
import unittest
from datetime import datetime
from decimal import Decimal as D
from pathlib import Path
from zoneinfo import ZoneInfo

from pendle.alerts import Alerter, TelegramCredentials
from pendle.maker import Maker, MarketRules
from pendle.maker_config import MakerSettings
from pendle.market import timestamp_ms
from test_maker import MARKET, book
from test_maker_e2e import Alerts, Clock, Files


MISSING = object()


class MarkSourceFreshnessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock, self.alerts = Clock(), Alerts()
        self.bot = Maker(MakerSettings(), api=None, alerter=self.alerts,
            rules=MarketRules('.01', '.001'), files=Files(),
            monotonic=self.clock, clock=self.clock.wall, sleep=self.clock.sleep,
            logger=logging.getLogger(self.id()))
        self.bot.logger.addHandler(logging.NullHandler())
        self.refresh_book()

    def refresh_book(self):
        self.bot.book = book(at=int(self.clock.wall()*1000))
        self.bot.book_seen = self.clock()

    def source_time(self, age=0):
        return datetime.fromtimestamp(self.clock.wall()-age, ZoneInfo('UTC')).isoformat()

    def message(self, *, update_age=0, push_age=0):
        return {'channel': self.bot.settings.mark_channel,
                'timestamp': self.source_time(push_age),
                'data': [{'market': MARKET, 'markPrice': '100',
                          'lastUpdatedTime': self.source_time(update_age),
                          'inPriceDiscovery': False}]}

    async def publish(self, message=None):
        self.refresh_book()
        await self.bot.on_public(self.message() if message is None else message)

    def assert_freshness(self, expected):
        self.assertIs(self.bot.engine.mark_is_fresh(self.clock()), expected)
        self.assertIs(self.bot.engine.discovery_is_fresh(self.clock()), expected)

    async def test_year_2000_source_time_triggers_guard_and_alert(self):
        message = self.message()
        message['data'][0]['lastUpdatedTime'] = '2000-01-01T00:00:00Z'
        await self.publish(message)
        self.assertEqual(self.bot.guard.state, 'COOLDOWN')
        self.assertTrue(any('lastUpdatedTime' in reason for reason in self.alerts.messages))
        self.assert_freshness(False)

    async def test_each_source_time_obeys_wall_clock_age_and_future_boundaries(self):
        for field in ('lastUpdatedTime', 'timestamp'):
            for age, fresh in ((3600, True), (3600.001, False), (-2, True), (-2.001, False)):
                with self.subTest(field=field, age=age):
                    # Keep boundaries independent so a future source timestamp accepted in one subtest does not affect later ordering.
                    self.setUp()
                    message = self.message()
                    target = message['data'][0] if field == 'lastUpdatedTime' else message
                    target[field] = self.source_time(age)
                    await self.publish(message)
                    self.assert_freshness(fresh)

    async def test_submillisecond_future_excess_is_rejected_for_each_source_time(self):
        for field in ('lastUpdatedTime', 'timestamp'):
            with self.subTest(field=field):
                message = self.message()
                target = message['data'][0] if field == 'lastUpdatedTime' else message
                target[field] = self.source_time(-2.0001)
                await self.publish(message)
                self.assert_freshness(False)

    async def test_invalid_prices_cannot_refresh_previous_source_times_or_receipts(self):
        initial = self.message()
        initial['data'][0]['oraclePrice'] = '100'
        await self.publish(initial)
        fields = ('mark', 'oracle', 'mark_ws_seen', 'mark_rest_seen',
                  'mark_source_times', 'discovery_source_times',
                  'in_price_discovery', 'price_discovery_seen', 'price_discovery_invalid')
        previous = {field: getattr(self.bot.engine, field) for field in fields}
        for field in ('markPrice', 'oraclePrice'):
            for value in ('NaN', '0', None):
                with self.subTest(field=field, value=value):
                    self.clock.now += 1
                    message = self.message()
                    message['data'][0][field] = value
                    with self.assertRaises(ValueError):
                        await self.publish(message)
                    current = {key: getattr(self.bot.engine, key) for key in fields}
                    self.assertEqual(current, previous)

    async def test_missing_or_unparseable_source_times_invalidate_both_states(self):
        for field in ('lastUpdatedTime', 'timestamp'):
            for value in (MISSING, None, '无法解析', 123, True, {}, '2026-11-07T02:00:00'):
                with self.subTest(field=field, value=value):
                    await self.publish()
                    mark, discovery_at = self.bot.engine.mark, self.bot.engine.price_discovery_seen
                    message = self.message()
                    target = message['data'][0] if field == 'lastUpdatedTime' else message
                    if value is MISSING:
                        target.pop(field)
                    else:
                        target[field] = value
                    await self.publish(message)
                    self.assert_freshness(False)
                    self.assertEqual(self.bot.engine.mark, mark)
                    self.assertEqual(self.bot.engine.price_discovery_seen, discovery_at)
                    self.assertIn(field, self.alerts.messages[-1])

    async def test_mark_receipt_expires_before_discovery_with_fresh_source_wall_clock(self):
        await self.publish()
        age = self.bot.settings.mark_quiet_max_s+.001
        self.clock.now += age
        self.clock.epoch -= age
        self.refresh_book()
        self.assertFalse(self.bot.engine.mark_is_fresh(self.clock()))
        self.assertTrue(self.bot.engine.discovery_is_fresh(self.clock()))

    async def test_discovery_receipt_expires_even_with_fresh_mark_and_source_wall_clock(self):
        await self.publish()
        age = self.bot.settings.discovery_quiet_max_s+.001
        self.clock.now += age
        self.clock.epoch -= age
        self.bot.engine.mark = (D(100), self.clock())
        self.bot.engine.mark_ws_seen = self.clock()
        self.refresh_book()
        self.assertTrue(self.bot.engine.mark_is_fresh(self.clock()))
        self.assertFalse(self.bot.engine.discovery_is_fresh(self.clock()))

    async def test_source_age_is_rechecked_after_receipt_is_refreshed(self):
        await self.publish()
        self.clock.now += self.bot.settings.mark_source_max_age_s+.001
        self.bot.engine.mark = (D(100), self.clock())
        self.bot.engine.mark_ws_seen = self.clock()
        self.bot.engine.price_discovery_seen = self.clock()
        self.refresh_book()
        self.assert_freshness(False)

    async def test_source_future_is_rechecked_after_wall_clock_moves_backwards(self):
        await self.publish()
        self.clock.epoch -= 2.001
        self.assert_freshness(False)

    async def test_last_updated_can_follow_push_timestamp_by_three_seconds(self):
        await self.publish(self.message(update_age=1, push_age=4))
        self.assert_freshness(True)

    async def test_nanosecond_live_sample_source_times_are_accepted(self):
        path = Path(__file__).parent / 'fixtures' / 'live_samples' / 'ws_markPricesPerps.json'
        message = json.loads(path.read_text(encoding='utf-8'))
        self.clock.epoch = timestamp_ms(message['timestamp'])/1000
        await self.publish(message)
        self.assert_freshness(True)
        self.assertEqual(self.bot.engine.mark[0], D('247.81'))

    async def test_new_valid_message_recovers_source_freshness(self):
        await self.publish(self.message(update_age=3600.001))
        self.assert_freshness(False)
        await self.publish()
        self.assert_freshness(True)

    async def test_missing_and_invalid_source_time_alerts_use_same_merge_key(self):
        sent = []

        async def http(method, url, **kwargs):
            sent.append(kwargs['json']['text'])
            return {'ok': True}

        alerter = Alerter(TelegramCredentials('测试-token', '测试-chat'),
                          http=http, monotonic=self.clock)
        self.bot.alerter = alerter
        started = self.clock()
        for age, field, value in ((0, 'lastUpdatedTime', MISSING),
                                  (1, 'timestamp', '无法解析'),
                                  (1799.999, 'lastUpdatedTime', None)):
            self.clock.now = started+age
            message = self.message()
            target = message['data'][0] if field == 'lastUpdatedTime' else message
            if value is MISSING:
                target.pop(field)
            else:
                target[field] = value
            await self.publish(message)
            for _ in range(3):
                await asyncio.sleep(0)
        self.assertEqual(len(sent), 1)
        self.assertEqual(len(alerter._pending), 1)
        self.assertIn('标记价格时间异常', alerter._pending)
        self.clock.now = started+1800
        await self.publish(self.message(update_age=3600.001))
        for _ in range(3):
            await asyncio.sleep(0)
        self.assertEqual(len(sent), 2)


if __name__ == '__main__':
    unittest.main()
