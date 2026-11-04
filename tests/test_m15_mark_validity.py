"""M15 按价变推送的标记价格有效性、公开 REST 确认与真实样本回放。"""

import asyncio
import copy
import json
import logging
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import patch

from pendle import maker_transport
from pendle.maker import Maker, MarketRules
from pendle.maker_config import MakerSettings
from pendle.market import timestamp_ms
from pendle.ondo_api import Credentials, HttpResponse, OndoAPI
from test_m13_center import MARKET, depth
from test_maker_e2e import Alerts, Clock, Files
from test_maker_transport import Opener, Response


SAMPLES = Path(__file__).parent / 'fixtures' / 'live_samples'
MISSING = object()


class M15MarkValidityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock, self.alerts, self.requests = Clock(), Alerts(), []
        self.rest_value, self.rest_error = '100', None
        self.rest_source_age = 0
        self.rest_extra = {}
        self.settings = replace(MakerSettings(), cooldown_s=.001, stable_s=.001,
            window_start_day=0, window_start_time='00:00',
            window_end_day=6, window_end_time='23:59')
        self.bot = self.make_bot()
        socket_patch = patch('socket.socket.connect', side_effect=AssertionError('M15 回放禁止联网'))
        socket_patch.start()
        self.addCleanup(socket_patch.stop)

    def make_bot(self, *, api=None, public_http=None):
        logger = logging.getLogger(self.id())
        logger.addHandler(logging.NullHandler())
        return Maker(self.settings, api=api, alerter=self.alerts,
            rules=MarketRules('.000001', '.001'), files=Files(),
            public_http=public_http or self.http, monotonic=self.clock,
            clock=self.clock.wall, sleep=self.clock.sleep, logger=logger)

    def source_time(self, age=0):
        return datetime.fromtimestamp(self.clock.wall()-age, timezone.utc).isoformat()

    def mark_message(self, *, value='100', age=0, discovery=False):
        return {'channel': self.settings.mark_channel, 'timestamp': self.source_time(),
                'data': [{'market': MARKET, 'markPrice': value,
                          'lastUpdatedTime': self.source_time(age),
                          'inPriceDiscovery': discovery}]}

    async def http(self, method, url, **kwargs):
        self.requests.append((self.clock(), method, url, kwargs))
        if self.rest_error is not None:
            raise self.rest_error
        row = {'markPrice': self.rest_value, 'oraclePrice': self.rest_value,
               'lastExternalPrice': self.rest_value,
               'lastUpdatedTime': self.source_time(self.rest_source_age)}
        row.update(self.rest_extra)
        if row.get('lastUpdatedTime') is MISSING:
            row.pop('lastUpdatedTime')
        return HttpResponse(200, {'success': True, 'result': {MARKET: row}})

    def healthy_book(self):
        self.bot.book = depth(now=self.clock.wall())
        self.bot.book_seen = self.clock()

    async def ready(self):
        self.healthy_book()
        await self.bot.on_public(self.mark_message())
        await self.bot.tick()
        self.assertTrue(self.bot.last_decision.eligible, self.bot.last_decision.reason)
        self.assertTrue(self.bot.qualified)

    async def poll(self):
        poll = getattr(self.bot, 'poll_mark_rest_once', None)
        self.assertTrue(callable(poll), '必须提供独立的公开 REST 标记价格条件轮询')
        await poll()

    def decision(self):
        return self.bot.engine.evaluate(self.bot.book, self.bot.state, self.bot.rules,
                                        now=self.clock())

    async def test_unchanged_mark_quotes_through_sixty_seconds_with_healthy_book(self):
        await self.ready()
        for elapsed in (10.001, 14, 30, 59.999, 60):
            self.clock.now = 100+elapsed
            self.healthy_book()
            await self.bot.tick()
            self.assertTrue(self.bot.qualified, (elapsed, self.bot.guard.reason))
            self.assertTrue(self.bot.last_decision.eligible)
            self.assertEqual(self.bot.last_decision.center, D(100))

    async def test_more_than_sixty_seconds_without_ws_and_failed_rest_stops_both_sides(self):
        await self.ready()
        self.rest_error = TimeoutError('离线 REST 超时')
        self.clock.now += 60.001
        self.healthy_book()
        await self.poll()
        await self.bot.tick()
        self.assertFalse(self.bot.engine.mark_is_fresh(self.clock()))
        self.assertFalse(self.bot.qualified)
        self.assertIn('新鲜标记价格', self.bot.guard.reason)

    async def test_recent_rest_confirmation_allows_quotes_after_ws_quiet_limit(self):
        await self.ready()
        self.clock.now += 61
        self.healthy_book()
        # REST 确认当前值时，价未变的 lastUpdatedTime 仍可等于原 WS 源时间。
        self.rest_source_age = 61
        await self.poll()
        await self.bot.tick()
        self.assertTrue(self.bot.engine.mark_is_fresh(self.clock()))
        self.assertTrue(self.bot.qualified, self.bot.guard.reason)
        self.assertEqual(self.bot.last_decision.center, D(100))
        self.assertEqual(self.bot.engine.price_discovery_seen, 100)

    async def test_rest_confirmation_expires_after_fifteen_seconds(self):
        await self.ready()
        self.clock.now += 61
        self.healthy_book()
        await self.poll()
        self.clock.now += 15
        self.healthy_book()
        self.assertTrue(self.bot.engine.mark_is_fresh(self.clock()))
        self.clock.now += .001
        self.assertFalse(self.bot.engine.mark_is_fresh(self.clock()))

    async def test_rest_confirmation_remains_valid_when_ws_quiet_time_crosses_sixty_seconds(self):
        await self.ready()
        self.clock.now += 50
        self.healthy_book()
        await self.poll()
        self.clock.now += 11
        self.healthy_book()
        await self.bot.tick()
        self.assertTrue(self.bot.qualified, self.bot.guard.reason)

    async def test_same_value_ws_push_retains_rest_confirmation_for_that_current_value(self):
        await self.ready()
        self.clock.now += 61
        self.healthy_book()
        await self.poll()
        self.clock.now += 1
        await self.bot.on_public(self.mark_message())
        self.clock.now += self.settings.max_book_age_s+.001
        self.assertTrue(self.bot.engine.mark_is_fresh(self.clock()),
                        '相同当前值仍有 15 秒内独立 REST 确认')
        self.assertFalse(self.bot.engine.discovery_is_fresh(self.clock()))

    async def test_changed_ws_value_cannot_borrow_rest_confirmation_for_previous_value(self):
        await self.ready()
        self.clock.now += 61
        self.healthy_book()
        await self.poll()
        self.clock.now += 1
        await self.bot.on_public(self.mark_message(value='100.01'))
        self.clock.now += self.settings.max_book_age_s+.001
        self.assertFalse(self.bot.engine.mark_is_fresh(self.clock()),
                         '不同值没有本次 REST 确认，且当前公开连接不健康')

    async def test_late_rest_snapshot_cannot_replace_newer_ws_source_value(self):
        await self.ready()
        self.clock.now += 10
        entered, release = asyncio.Event(), asyncio.Event()
        response = HttpResponse(200, {'success': True, 'result': {MARKET: {
            'markPrice': '100', 'lastUpdatedTime': self.source_time()}}})

        async def blocked_http(*args, **kwargs):
            entered.set()
            await release.wait()
            return response

        self.bot.public_http = blocked_http
        task = asyncio.create_task(self.bot.poll_mark_rest_once())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            self.clock.now += 1
            self.healthy_book()
            await self.bot.on_public(self.mark_message(value='100.01'))
            self.clock.now += 1
        finally:
            release.set()
            result = await task
        self.assertFalse(result, '迟到且源时间较早的 REST 快照不代表当前值')
        self.assertEqual(self.bot.engine.mark[0], D('100.01'))
        self.assertIsNone(self.bot.engine.mark_rest_seen)

    async def test_inflight_rest_with_equal_or_newer_source_remains_authoritative(self):
        for rest_source_ahead_s in (1, 2):
            with self.subTest(rest_source_ahead_s=rest_source_ahead_s):
                self.bot = self.make_bot()
                await self.ready()
                self.clock.now += 10
                entered, release = asyncio.Event(), asyncio.Event()
                response = HttpResponse(200, {'success': True, 'result': {MARKET: {
                    'markPrice': '100.02',
                    'lastUpdatedTime': self.source_time(-rest_source_ahead_s)}}})

                async def blocked_http(*args, **kwargs):
                    entered.set()
                    await release.wait()
                    return response

                self.bot.public_http = blocked_http
                task = asyncio.create_task(self.bot.poll_mark_rest_once())
                try:
                    await asyncio.wait_for(entered.wait(), 1)
                    self.clock.now += 1
                    await self.bot.on_public(self.mark_message(value='100.01'))
                    self.clock.now += 1
                finally:
                    release.set()
                    result = await task
                self.assertTrue(result)
                self.assertEqual(self.bot.engine.mark[0], D('100.02'))
                self.assertEqual(self.bot.engine.mark_rest_seen, self.clock())

    async def test_rest_changed_value_updates_mark_center_and_quote_band(self):
        await self.ready()
        self.clock.now += 11
        self.healthy_book()
        self.rest_value = '100.01'
        await self.poll()
        result = self.decision()
        self.assertTrue(result.eligible, result.reason)
        self.assertEqual(self.bot.engine.mark[0], D('100.01'))
        self.assertEqual(result.center, D('100.01'))
        for quote in result.quotes:
            distance = abs(quote.price/D('100.01')-1)*10000
            self.assertGreaterEqual(distance, self.settings.min_offset_bps)
            self.assertLessEqual(distance, self.settings.band_bps-self.settings.band_margin_bps)

    async def test_large_rest_price_change_runs_existing_center_jump_guard(self):
        await self.ready()
        self.clock.now += 11
        self.healthy_book()
        self.rest_value = '101'
        await self.poll()
        result = self.decision()
        self.assertEqual(result.center, D(101))
        self.assertFalse(result.eligible)
        self.assertFalse(result.quotes)
        self.assertIn('偏离独立慢锚', result.reason)

    async def test_rest_poll_waits_ten_seconds_and_has_independent_ten_second_interval(self):
        await self.ready()
        for elapsed, expected_calls in ((0, 0), (9.999, 0), (10, 1),
                                        (10.1, 1), (19.999, 1), (20, 2)):
            self.clock.now = 100+elapsed
            self.healthy_book()
            await self.poll()
            self.assertEqual(len(self.requests), expected_calls, elapsed)
        self.assertTrue(all(method == 'GET' and url == self.settings.rest_url+'/v1/perps/mark_prices'
                            for _, method, url, _ in self.requests))

    async def test_rest_failures_are_throttled_by_same_independent_poll_interval(self):
        await self.ready()
        self.rest_error = TimeoutError('离线失败')
        for elapsed, expected_calls in ((10, 1), (10.001, 1), (19.999, 1), (20, 2)):
            self.clock.now = 100+elapsed
            await self.poll()
            self.assertEqual(len(self.requests), expected_calls)

    async def test_public_rest_poll_does_not_take_trading_rate_limit_token(self):
        signed_requests = []

        async def signed_transport(request):
            signed_requests.append(request)
            self.fail('公开 mark REST 不应进入已签名交易请求传输')

        api = OndoAPI(Credentials('测试-key', '测试-secret'), transport=signed_transport,
            monotonic=self.clock, clock=self.clock.wall, sleep=self.clock.sleep)
        api._tokens = 0
        self.bot = self.make_bot(api=api)
        await self.ready()
        self.clock.now += 11
        before = (api._tokens, api._last_refill, len(api._pending))
        await self.poll()
        self.assertEqual((api._tokens, api._last_refill, len(api._pending)), before)
        self.assertEqual(signed_requests, [])
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.bot.engine.mark[0], D(100))

    async def test_rest_market_dictionary_without_discovery_retains_ws_boolean_and_time(self):
        await self.ready()
        self.clock.now += 61
        self.healthy_book()
        await self.poll()
        self.assertIs(self.bot.engine.in_price_discovery, False)
        self.assertEqual(self.bot.engine.price_discovery_seen, 100)
        self.assertTrue(self.bot.engine.discovery_is_fresh(self.clock()))
        self.assertTrue(self.decision().eligible)

    async def test_source_time_is_sanity_checked_to_one_hour_instead_of_ten_seconds(self):
        self.healthy_book()
        for age in (11, 1800, 3600):
            with self.subTest(age=age):
                await self.bot.on_public(self.mark_message(age=age))
                self.assertTrue(self.bot.engine.mark_is_fresh(self.clock()))
                self.assertTrue(self.decision().eligible)

    async def test_invalid_ws_source_time_rejects_with_alert_including_year_2000(self):
        for value in ('2000-01-01T00:00:00Z', self.source_time(-2.001),
                      self.source_time(3600.001), MISSING, None, '无法解析'):
            with self.subTest(value=value):
                await self.bot.on_public(self.mark_message())
                message = self.mark_message()
                if value is MISSING:
                    message['data'][0].pop('lastUpdatedTime')
                else:
                    message['data'][0]['lastUpdatedTime'] = value
                await self.bot.on_public(message)
                self.assertFalse(self.bot.engine.mark_is_fresh(self.clock()))
                self.assertIn('lastUpdatedTime', self.alerts.messages[-1])

    async def test_invalid_rest_source_time_cannot_confirm_or_replace_mark(self):
        for value in ('2000-01-01T00:00:00Z', None, '无法解析'):
            with self.subTest(value=value):
                self.bot = self.make_bot()
                self.rest_extra = {}
                await self.ready()
                self.clock.now += 61
                self.rest_value = '101'
                self.rest_extra = {'lastUpdatedTime': value}
                await self.poll()
                self.assertEqual(self.bot.engine.mark[0], D(100))
                self.assertFalse(self.bot.engine.mark_is_fresh(self.clock()))
                self.assertTrue(any('lastUpdatedTime' in text for text in self.alerts.messages))

    async def test_invalid_rest_source_preserves_still_valid_ws_rule_and_quotes(self):
        for source_case in ('缺失', '异常过旧', '超前'):
            with self.subTest(source_case=source_case):
                self.bot = self.make_bot()
                self.rest_value, self.rest_extra = '100', {}
                await self.ready()
                self.clock.now += 11
                self.healthy_book()
                value = (MISSING if source_case == '缺失' else
                         self.source_time(3600.001 if source_case == '异常过旧' else -2.001))
                self.rest_value = '101'
                self.rest_extra = {'lastUpdatedTime': value}
                before = (self.bot.engine.mark, self.bot.engine.mark_source_times,
                          self.bot.engine.mark_ws_seen, self.bot.engine.mark_rest_seen)
                self.assertFalse(await self.bot.poll_mark_rest_once())
                self.assertEqual((self.bot.engine.mark, self.bot.engine.mark_source_times,
                                  self.bot.engine.mark_ws_seen, self.bot.engine.mark_rest_seen), before)
                self.assertIn('lastUpdatedTime', self.alerts.messages[-1])
                await self.bot.tick()
                self.assertTrue(self.bot.qualified, '异常 REST 不能破坏仍然成立的规则 A')

    async def test_invalid_rest_source_does_not_shorten_previous_rest_confirmation(self):
        for source_case in ('缺失', '异常过旧', '超前'):
            with self.subTest(source_case=source_case):
                self.bot = self.make_bot()
                self.rest_value, self.rest_extra = '100', {}
                await self.ready()
                self.clock.now += 61
                self.healthy_book()
                await self.poll()
                self.clock.now += 10
                self.healthy_book()
                before = (self.bot.engine.mark, self.bot.engine.mark_source_times,
                          self.bot.engine.mark_rest_seen)
                value = (MISSING if source_case == '缺失' else
                         self.source_time(3600.001 if source_case == '异常过旧' else -2.001))
                self.rest_extra = {'lastUpdatedTime': value}
                self.assertFalse(await self.bot.poll_mark_rest_once())
                self.assertEqual((self.bot.engine.mark, self.bot.engine.mark_source_times,
                                  self.bot.engine.mark_rest_seen), before)
                self.assertIn('lastUpdatedTime', self.alerts.messages[-1])
                await self.bot.tick()
                self.assertTrue(self.bot.qualified, '异常 REST 不能提前取消仍然成立的规则 B')

    async def test_discovery_boolean_expires_after_three_hundred_seconds_even_with_rest(self):
        await self.ready()
        self.clock.now += 300
        self.healthy_book()
        await self.poll()
        self.assertTrue(self.bot.engine.discovery_is_fresh(self.clock()))
        self.clock.now += .001
        self.healthy_book()
        self.assertFalse(self.bot.engine.discovery_is_fresh(self.clock()))
        result = self.decision()
        self.assertFalse(result.eligible)
        self.assertIn('价格发现', result.reason)

    async def test_unhealthy_public_book_disables_ws_validity_and_discovery(self):
        await self.ready()
        self.clock.now += self.settings.max_book_age_s+.001
        self.assertFalse(self.bot.engine.mark_is_fresh(self.clock()))
        self.assertFalse(self.bot.engine.discovery_is_fresh(self.clock()))
        await self.bot.tick()
        self.assertFalse(self.bot.qualified)
        self.assertIn('盘口过期', self.bot.guard.reason)

    async def test_rest_can_confirm_mark_but_cannot_refresh_discovery_on_unhealthy_public_feed(self):
        await self.ready()
        self.clock.now += 11
        await self.poll()
        self.assertTrue(self.bot.engine.mark_is_fresh(self.clock()))
        self.assertFalse(self.bot.engine.discovery_is_fresh(self.clock()))

    async def test_public_disconnect_disables_ws_rule_even_before_book_ages_out(self):
        await self.ready()

        class DisconnectedSocket:
            async def send(self, message):
                pass

            def __aiter__(self):
                return self

            async def __anext__(self):
                raise OSError('离线模拟公开连接刚刚断开')

            async def close(self):
                pass

        async def connect(*args, **kwargs):
            return DisconnectedSocket()

        async def stop_before_reconnect(delay):
            self.bot.request_stop()
            await asyncio.sleep(0)

        self.bot.connect, self.bot.sleep = connect, stop_before_reconnect
        await self.bot.feed()
        self.assertEqual(self.clock()-self.bot.book_seen, 0)
        self.assertFalse(self.bot.engine.mark_is_fresh(self.clock()))
        self.assertFalse(self.bot.engine.discovery_is_fresh(self.clock()))

    async def test_run_starts_independent_rest_polling_loop(self):
        await self.ready()
        lifecycle = []

        async def startup(*, live=False):
            lifecycle.append('启动')
            self.bot.started = True

        async def shutdown():
            lifecycle.append('关闭')

        async def idle():
            await self.bot.stop_event.wait()

        async def sleep(delay):
            self.clock.now += delay
            self.healthy_book()
            if self.clock.now >= 111:
                self.bot.request_stop()
            await asyncio.sleep(0)

        self.bot.startup, self.bot.shutdown = startup, shutdown
        self.bot.feed = self.bot.strategy_loop = self.bot.monitor_loop = idle
        self.bot.sleep = sleep
        loop = asyncio.get_running_loop()
        with patch.object(loop, 'add_signal_handler'), patch.object(loop, 'remove_signal_handler'):
            exit_code = await asyncio.wait_for(self.bot.run(), 1)
        self.assertEqual(exit_code, 0)
        self.assertEqual(lifecycle, ['启动', '关闭'])
        self.assertEqual([at for at, _, _, _ in self.requests], [110])

    async def test_price_discovery_true_still_stops_immediately(self):
        await self.ready()
        await self.bot.on_public(self.mark_message(discovery=True))
        self.assertIn('价格发现', self.bot.guard.reason)
        self.assertFalse(self.decision().eligible)

    async def test_real_ws_sample_replay_keeps_mark_during_fifty_nine_second_price_quiet(self):
        message = json.loads((SAMPLES/'ws_markPricesPerps.json').read_text(encoding='utf-8'))
        book_message = json.loads((SAMPLES/'ws_depthBooksPerps.json').read_text(encoding='utf-8'))
        # 删除近档模拟稀疏盘口，真实样本的价格和量均保留。
        book_message['data'][0]['bids'] = book_message['data'][0]['bids'][2:]
        book_message['data'][0]['asks'] = book_message['data'][0]['asks'][2:]
        self.clock.epoch = timestamp_ms(message['timestamp'])/1000
        await self.bot.on_public(json.dumps(book_message))
        await self.bot.on_public(json.dumps(message))
        await self.bot.tick()
        self.assertEqual(self.bot.last_decision.center, D('247.81'))
        self.clock.now += 59
        next_book = copy.deepcopy(book_message)
        # 回放健康盘口心跳；价和量沿用样本，只推进消息时间。
        next_book['timestamp'] = next_book['data'][0]['time'] = self.source_time()
        await self.bot.on_public(json.dumps(next_book))
        await self.bot.tick()
        self.assertTrue(self.bot.qualified, self.bot.guard.reason)
        self.assertEqual(self.bot.last_decision.center, D('247.81'))

    async def test_public_mark_rest_transport_sends_user_agent_without_authentication(self):
        payload = {'success': True, 'result': {MARKET: {
            'markPrice': '100', 'oraclePrice': '100', 'lastExternalPrice': '100',
            'lastUpdatedTime': self.source_time()}}}
        opener = Opener(Response(json.dumps(payload).encode()))
        response = await maker_transport.public_http('GET',
            self.settings.rest_url+'/v1/perps/mark_prices', opener=opener)
        self.assertEqual(response.payload, payload)
        request = opener.calls[0][0]
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(headers.get('user-agent'), 'pendle-maker/0.1')
        self.assertFalse(any(key.startswith('ondo-') or key == 'authorization' for key in headers))
        self.assertEqual(request.get_method(), 'GET')
        self.assertIsNone(request.data)


if __name__ == '__main__':
    unittest.main()
