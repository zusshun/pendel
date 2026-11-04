"""M8 real public sample replay; inject only transport, retaining Maker and its business components."""

import copy
import json
import logging
import unittest
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import patch

from pendle.maker import MarketRules
from pendle.maker_config import MakerSettings
from pendle.maker_orders import Order, OrderStatus, OrdersBlocked
from pendle.maker_risk import RiskRejected
from pendle.maker_transport import build_maker
from pendle.market import timestamp_ms
from pendle.ondo_api import HttpResponse
from test_maker import MARKET, book
from test_maker_e2e import Alerts, Clock, Files


SAMPLES = Path(__file__).parent / 'fixtures' / 'live_samples'


def sample(name):
    return json.loads((SAMPLES / (name + '.json')).read_text())


class LiveSampleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock, self.alerts, self.files = Clock(), Alerts(), Files()
        self.clock.epoch = timestamp_ms(sample('ws_depthBooksPerps')['timestamp']) / 1000
        self.requests = []
        self.metadata = sample('v1_markets_ibm')
        self.settings = replace(MakerSettings(), window_start_day=2,
            window_start_time='00:00', window_end_day=3, window_end_time='23:59')

        async def http(method, url, **kwargs):
            self.requests.append((method, url))
            self.assertEqual(method, 'GET', 'dry-run 不得发送写请求')
            self.assertTrue(url.endswith('/v1/markets'))
            return HttpResponse(200, self.metadata)

        self.bot = build_maker(self.settings, env={}, http=http, alerter=self.alerts,
            files=self.files, logger=logging.getLogger('test.m8.samples'),
            monotonic=self.clock, clock=self.clock.wall, sleep=self.clock.sleep)

    async def asyncTearDown(self):
        await self.bot.shutdown()

    async def test_live_market_rules_and_dry_run_margin_estimate(self):
        with self.assertLogs(self.bot.logger, level='INFO') as logs:
            await self.bot.startup()
        self.assertEqual(self.bot.rules.tick, D('.01'))
        self.assertEqual(self.bot.rules.increment, D('.01'))
        self.assertEqual(self.bot.rules.min_notional, 0)
        self.assertEqual(self.bot.rules.max_position_base_size, D('4300'))
        self.assertEqual(self.bot.rules.default_leverage, D(10))
        self.assertEqual(self.bot.rules.max_leverage, D(10))
        self.assertEqual(self.bot.initial_margin_rate, D('.1'))
        self.assertIn('估算', '\n'.join(logs.output))
        self.assertEqual(len(self.requests), 1)

    async def test_dry_run_estimate_uses_actual_default_leverage(self):
        self.metadata['result']['perps']['tradingPairs'][0]['defaultLeverage'] = '5'
        await self.bot.load_rules()
        self.assertEqual(self.bot.initial_margin_rate, D('.2'))

    async def test_authenticated_rules_keep_account_leverage_endpoint(self):
        class API:
            async def request(api, method, path, **kwargs):
                self.requests.append((method, path, kwargs))
                if path == '/v1/markets':
                    return self.metadata
                return {'success': True, 'result': [{'market': MARKET, 'leverage': '4'}]}

        self.bot.api = API()
        await self.bot.load_rules()
        self.assertEqual(self.bot.initial_margin_rate, D('.25'))
        self.assertEqual(self.requests[-1], ('GET', '/v1/perps/leverage', {'params': {'market': MARKET}}))

    async def test_invalid_live_rules_fail_closed(self):
        original = copy.deepcopy(self.metadata)
        for field, value in (('baseIncrement', '0'), ('quoteIncrement', None),
                             ('defaultLeverage', '0'), ('maxPositionBaseSize', '-1'),
                             ('marginInfo', [{'maxLeverage': 'NaN'}])):
            with self.subTest(field=field):
                self.metadata = copy.deepcopy(original)
                self.metadata['result']['perps']['tradingPairs'][0][field] = value
                with self.assertRaises(ValueError):
                    await self.bot.load_rules()

    async def test_live_depth_and_mark_paths_use_sample_fields(self):
        await self.bot.on_public(json.dumps(sample('ws_depthBooksPerps')))
        await self.bot.on_public(json.dumps(sample('ws_markPricesPerps')))
        self.assertEqual(self.bot.book.mid, D('247.82'))
        self.assertEqual(self.bot.engine.mark[0], D('247.81'))
        self.assertEqual(self.bot.engine.oracle[0], D('248.1146357291905471'))
        self.assertFalse(self.bot.engine.in_price_discovery)

    async def test_all_live_control_confirmations_are_handled_before_data(self):
        for message in sample('ws_control'):
            if message['type'] == 'subscribed':
                await self.bot.on_public(json.dumps(message))
                self.assertIn(message['channel'], self.bot.public_subscriptions)
        self.assertIsNone(self.bot.book)
        self.assertFalse(self.alerts.messages)

    async def test_live_trade_top_and_funding_do_not_replace_depth_or_mark(self):
        await self.bot.on_public(sample('ws_depthBooksPerps'))
        await self.bot.on_public(sample('ws_markPricesPerps'))
        previous = (self.bot.book, self.bot.engine.mark)
        with self.assertLogs(self.bot.logger, level='DEBUG') as logs:
            for channel in ('tradesPerps', 'topOfBooksPerps', 'fundingRatesPerps'):
                await self.bot.on_public(sample('ws_' + channel))
        self.assertEqual((self.bot.book, self.bot.engine.mark), previous)
        trade = next(json.loads(row.split(':', 2)[-1]) for row in logs.output if '公开成交' in row)
        self.assertEqual(trade['价格'], '247.92')
        self.assertEqual(trade['数量'], '1.21')
        self.assertEqual(trade['主动方向'], 'sell')

    async def test_discovery_stops_both_sides_and_records_oracle(self):
        await self.bot.load_rules()
        await self.bot.on_public(sample('ws_depthBooksPerps'))
        message = sample('ws_markPricesPerps')
        message['data'][0]['inPriceDiscovery'] = True
        await self.bot.on_public(message)
        result = self.bot.engine.evaluate(self.bot.book, self.bot.state, self.bot.rules, now=self.clock())
        self.assertFalse(result.eligible)
        self.assertFalse(result.quotes)
        self.assertIn('价格发现', result.reason)
        self.assertIn('价格发现', self.bot.last_stop_reason)
        self.assertEqual(self.bot.engine.oracle[0], D(message['data'][0]['oraclePrice']))

    async def test_incomplete_mark_cannot_clear_price_discovery_guard(self):
        message = sample('ws_markPricesPerps')
        message['data'][0]['inPriceDiscovery'] = True
        await self.bot.on_public(message)
        del message['data'][0]['inPriceDiscovery']
        await self.bot.on_public(message)
        self.assertTrue(self.bot.engine.in_price_discovery)

    async def test_healthy_live_sample_replay_computes_strategy_center(self):
        await self.bot.startup()
        for message in sample('ws_control'):
            if message['type'] == 'subscribed':
                await self.bot.on_public(message)
        for channel in ('depthBooksPerps', 'tradesPerps', 'markPricesPerps',
                        'topOfBooksPerps', 'fundingRatesPerps'):
            await self.bot.on_public(sample('ws_' + channel))
        await self.bot.tick()
        self.assertEqual(self.bot.last_decision.center, D('247.82'))
        self.assertEqual(self.bot.engine.anchor, D('247.82'))
        self.assertEqual(self.bot.engine.mark[0], D('247.81'))
        self.assertEqual(self.bot.last_decision.reason, '竞争量超过判据')
        self.assertEqual([method for method, url in self.requests], ['GET'])

    async def test_unknown_subscription_error_blocks_quotes_until_all_critical_acks(self):
        await self.bot.load_rules()
        await self.bot.on_public(sample('ws_depthBooksPerps'))
        await self.bot.on_public(sample('ws_markPricesPerps'))
        error = next(row for row in sample('ws_control') if row['type'] == 'error')
        await self.bot.on_public(error)
        decision = self.bot.engine.evaluate(self.bot.book, self.bot.state, self.bot.rules, now=self.clock())
        self.assertFalse(decision.eligible)
        self.assertIn('订阅失败', decision.reason)
        self.assertTrue(any('订阅失败' in text for text in self.alerts.messages))
        await self.bot.on_public({'type': 'subscribed', 'channel': 'tradesPerps'})
        self.assertFalse(self.bot.engine.evaluate(self.bot.book, self.bot.state, self.bot.rules,
                                                now=self.clock()).eligible)
        for channel in ('depthBooksPerps', self.settings.mark_channel):
            await self.bot.on_public({'type': 'subscribed', 'channel': channel})
        self.assertFalse(self.bot.engine.subscription_failures)

    async def test_critical_subscription_failure_blocks_queued_order(self):
        self.bot.rules = MarketRules('.01', '.01')
        self.bot.book, self.bot.book_seen = book(at=int(self.clock.wall()*1000)), self.clock()
        self.bot.engine.mark = (D(100), self.clock())
        order = Order('queued', MARKET, 'buy', D(1), D('99.88'))
        await self.bot.on_public({'type': 'error', 'channel': 'markPricesPerps', 'msg': 'channel unsupported'})
        try:
            self.bot.settings = replace(self.settings, mode='sandbox')
            self.bot.engine.settings = self.bot.settings
            with self.assertRaisesRegex(OrdersBlocked, '订阅失败'):
                self.bot.check_order(order, self.bot.state)
        finally:
            self.bot.settings = self.settings

    async def test_noncritical_subscription_error_alerts_without_poisoning_quotes(self):
        await self.bot.on_public({'type': 'error', 'channel': 'fundingRatesPerps', 'msg': 'channel unsupported'})
        self.assertTrue(any('订阅失败' in text for text in self.alerts.messages))
        self.assertFalse(self.bot.engine.subscription_failures)

    async def test_real_sample_replay_through_default_dry_run_assembly(self):
        with patch('socket.socket.connect', side_effect=AssertionError('禁止联网')), \
                patch('socket.socket.bind', side_effect=AssertionError('禁止绑定端口')):
            await self.bot.startup()
            for message in sample('ws_control'):
                await self.bot.on_public(json.dumps(message))
            for channel in ('depthBooksPerps', 'tradesPerps', 'markPricesPerps',
                            'topOfBooksPerps', 'fundingRatesPerps'):
                await self.bot.on_public(json.dumps(sample('ws_' + channel)))
            await self.bot.tick()
            self.assertEqual((self.bot.rules.tick, self.bot.rules.increment), (D('.01'), D('.01')))
            self.assertEqual(self.bot.book.mid, D('247.82'))
            self.assertEqual(self.bot.engine.mark[0], D('247.81'))
            self.assertIsNotNone(self.bot.last_decision)
            if self.bot.last_decision.eligible:
                self.assertEqual({q.side for q in self.bot.last_decision.quotes}, {'buy', 'sell'})
            else:
                self.assertTrue(self.bot.last_decision.reason)
                self.assertEqual(self.bot.last_stop_reason, self.bot.last_decision.reason)
            await self.bot.shutdown()
        self.assertEqual(self.requests, [('GET', self.settings.rest_url + '/v1/markets')])
        self.assertIsNone(self.bot.api)
        self.assertFalse(self.bot.state.orders)

    def test_default_mark_channel_matches_live_confirmation(self):
        self.assertEqual(MakerSettings().mark_channel, 'markPricesPerps')


class ExchangeBaseLimitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock, self.alerts = Clock(), Alerts()
        self.bot = build_maker(replace(MakerSettings(), mode='sandbox'), api=None,
            alerter=self.alerts, files=Files(), rules=MarketRules('.01', '.01'),
            logger=logging.getLogger('test.m8.base'), monotonic=self.clock, clock=self.clock.wall)
        self.bot.rules = replace(self.bot.rules, max_position_base_size=D('1.5'))
        self.bot.book, self.bot.book_seen = book(at=int(self.clock.wall()*1000)), self.clock()
        self.bot.engine.mark = (D(100), self.clock())
        self.bot.engine.in_price_discovery = False
        self.bot.engine.price_discovery_seen = self.clock()
        self.bot.state.balances = dict(equity='10000', available='9000',
                                      under_liquidation=False, margin_ratio='.01')

    def decision(self, **kwargs):
        return self.bot.engine.evaluate(self.bot.book, self.bot.state, self.bot.rules,
                                       now=self.clock(), **kwargs)

    def test_planned_quotes_respect_exchange_base_cap_in_both_directions(self):
        for position in (D('1.49'), D('-1.49')):
            with self.subTest(position=position):
                self.bot.state.positions[MARKET] = position
                result = self.decision()
                self.assertTrue(result.eligible, result.reason)
                for quote in result.quotes:
                    endpoint = position + (quote.quantity if quote.side == 'buy' else -quote.quantity)
                    self.assertLessEqual(abs(endpoint), D('1.5'))

    def test_pending_same_side_orders_count_toward_exchange_base_cap(self):
        self.bot.state.orders['pending'] = Order('pending', MARKET, 'buy', D('1.49'),
            D('99.88'), status=OrderStatus.OPEN, exchange_ms=self.bot.book.source_ms)
        self.bot.book = book(bids=[('99.9', '10'), ('99.88', '1.49'), ('99', '10')],
                             at=self.bot.book.source_ms)
        result = self.decision()
        self.assertTrue(result.eligible, result.reason)
        self.assertEqual(result.quotes[0].quantity, D('.01'))

    def test_exhausted_base_capacity_gives_explicit_bilateral_pause(self):
        self.bot.state.positions[MARKET] = D('1.5')
        result = self.decision()
        self.assertFalse(result.eligible)
        self.assertFalse(result.quotes)
        self.assertIn('基础持仓', result.reason)

    def test_queued_order_cap_checks_all_nonterminal_same_side_orders(self):
        for side, position in (('buy', D('.5')), ('sell', D('-.5'))):
            for status in (OrderStatus.INTENT, OrderStatus.SENT, OrderStatus.ACKED,
                           OrderStatus.OPEN, OrderStatus.PARTIAL, OrderStatus.UNKNOWN,
                           OrderStatus.CANCEL_SENT):
                with self.subTest(side=side, status=status):
                    self.bot.state.orders.clear()
                    self.bot.state.positions[MARKET] = position
                    self.bot.state.orders['old'] = Order('old', MARKET, side, D('.5'),
                        D('99.88' if side == 'buy' else '100.12'), status=status)
                    order = Order('queued', MARKET, side, D('.51'),
                                  D('99.88' if side == 'buy' else '100.12'))
                    self.bot.state.orders['queued'] = order
                    with self.assertRaisesRegex(RiskRejected, '基础持仓'):
                        self.bot.check_order(order, self.bot.state)

    def test_queued_order_equal_to_base_cap_is_not_double_counted(self):
        order = Order('queued', MARKET, 'buy', D('1.5'), D('99.88'))
        self.bot.state.orders['queued'] = order
        self.bot.check_order(order, self.bot.state)

    def test_existing_order_at_base_cap_stays_valid_without_phantom_new_order(self):
        order = Order('existing', MARKET, 'buy', D('1.5'), D('99.88'),
                      status=OrderStatus.OPEN, exchange_ms=self.bot.book.source_ms)
        self.bot.state.orders['existing'] = order
        self.bot.book = book(bids=[('99.9', '10'), ('99.88', '1.5'), ('99', '10')],
                             at=self.bot.book.source_ms)
        self.bot.check_order(order, self.bot.state)
        decision = self.decision(guard_only=True, existing_prices=True)
        self.assertTrue(decision.eligible, decision.reason)
        self.assertLessEqual(decision.quotes[0].quantity, D('1.5'))
