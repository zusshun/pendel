"""M20 私有查询与快照中的空记录语义；全部使用本地模拟传输。"""

import copy
import json
import unittest
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path

from pendle.maker import Maker, MarketRules
from pendle.maker_config import MakerSettings
from pendle.maker_orders import Order
from pendle.maker_risk import RiskError
from pendle.ondo_api import ApiError, Credentials, HttpResponse, OndoAPI
from test_maker import MARKET, book
from test_maker_e2e import Alerts, Clock, Exchange, Files


SAMPLES = Path(__file__).parent / 'fixtures' / 'live_samples'


class M20EmptyQueryTests(unittest.IsolatedAsyncioTestCase):
    def client(self, payload):
        async def transport(request):
            return HttpResponse(200, payload)

        return OndoAPI(Credentials('测试-key', '测试-secret'), transport=transport,
                       rate=1_000_000)

    async def test_rest_empty_orders_and_fills_are_empty_lists(self):
        for method in ('get_open_orders', 'get_fills'):
            with self.subTest(method=method):
                api = self.client({'success': True, 'result': []})
                self.assertEqual(await getattr(api, method)(MARKET), [])

    async def test_rest_empty_balance_is_zero_without_invented_margin_ratio(self):
        api = self.client({'success': True, 'result': []})
        balance = await api.get_balances()
        for key in ('equity', 'available', 'used_margin'):
            self.assertEqual(D(balance[key]), 0)
        self.assertIs(balance['under_liquidation'], False)
        self.assertIsNone(balance['margin_ratio'])
        self.assertNotIn('time_ms', balance)

    async def test_rest_incomplete_balance_stays_invalid(self):
        for payload in ({'success': True, 'result': None},
                        {'success': True, 'result': {}},
                        {'success': True, 'result': {'equity': '300'}},
                        {'success': True, 'result': [{'equity': '300', 'available': '300'}]}):
            with self.subTest(payload=payload), self.assertRaises(ApiError):
                await self.client(payload).get_balances()

    async def test_successful_empty_single_order_is_absence(self):
        for result in ([], None):
            with self.subTest(result=result):
                self.assertIsNone(await self.client(
                    {'success': True, 'result': result}).get_order('missing'))

    async def test_single_order_missing_result_is_absence(self):
        self.assertIsNone(await self.client({'success': True}).get_order('missing'))


class M20EmptyLeverageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.metadata = json.loads((SAMPLES / 'v1_markets_ibm.json').read_text())
        self.leverage = {'success': True, 'result': []}
        clock, files = Clock(), Files()

        async def transport(request):
            payload = self.metadata if request.path_query == '/v1/markets' else self.leverage
            return HttpResponse(200, payload)

        api = OndoAPI(Credentials('测试-key', '测试-secret'), transport=transport,
                      rate=1_000_000)
        self.bot = Maker(replace(MakerSettings(), mode='sandbox'), api=api,
                         alerter=Alerts(), files=files, monotonic=clock,
                         clock=clock.wall, sleep=clock.sleep)

    async def test_empty_account_leverage_uses_market_default(self):
        await self.bot.load_rules()
        self.assertEqual(self.bot.initial_margin_rate, D('.1'))

    async def test_other_market_leverage_uses_target_market_default(self):
        self.leverage['result'] = [{'market': 'OTHER-USD.P', 'leverage': '4'}]
        await self.bot.load_rules()
        self.assertEqual(self.bot.initial_margin_rate, D('.1'))

    async def test_unset_leverage_without_default_uses_one_and_clears_previous_rate(self):
        self.metadata = {'success': True, 'result': [
            {'market': MARKET, 'quoteIncrement': '.01', 'baseIncrement': '.01'}]}
        self.bot.initial_margin_rate = D('.25')
        await self.bot.load_rules()
        self.assertEqual(self.bot.initial_margin_rate, D(1))

    async def test_present_account_leverage_still_overrides_market_default(self):
        self.leverage['result'] = [{'market': MARKET, 'leverage': '4'}]
        await self.bot.load_rules()
        self.assertEqual(self.bot.initial_margin_rate, D('.25'))

    async def test_malformed_or_duplicate_leverage_does_not_become_unset(self):
        original = copy.deepcopy(self.leverage)
        for payload in ({'success': True, 'result': None},
                        {'success': True, 'result': {}},
                        {'success': True, 'result': [None]},
                        {'success': True, 'result': [{'market': None, 'leverage': '2'}]},
                        {'success': True, 'result': [{'market': '', 'leverage': '2'}]},
                        {'success': True, 'result': [{'market': MARKET}]},
                        {'success': True, 'result': [{'market': MARKET, 'leverage': '0'}]},
                        {'success': True, 'result': [{'market': MARKET, 'leverage': 'NaN'}]},
                        {'success': True, 'result': [{'market': MARKET, 'leverage': '2'}] * 2}):
            self.leverage = payload
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                await self.bot.load_rules()
        self.leverage = original


class M20EmptyPrivateSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock, self.files, self.alerts = Clock(), Files(), Alerts()
        self.api = Exchange(self.clock, self.files)
        self.bot = Maker(replace(MakerSettings(), mode='sandbox', quiet_s=.01),
                         api=self.api, alerter=self.alerts,
                         rules=MarketRules(D('.01'), D('.001')), files=self.files,
                         monotonic=self.clock, clock=self.clock.wall, sleep=self.clock.sleep)
        await self.bot.startup()
        self.bot.state.deadman_ack()
        self.assertTrue(await self.bot.reconcile_once())
        self.bot.book = book(at=int(self.clock.wall()*1000))
        self.bot.book_seen = self.clock()
        self.bot.engine.mark = (D(100), self.clock())
        self.bot.engine.in_price_discovery = False
        self.bot.engine.price_discovery_seen = self.clock()

    async def test_empty_private_balance_clears_previous_budget_and_blocks_new_order(self):
        order = Order('proof', MARKET, 'buy', D('.1'), D('99.88'))
        self.bot.check_order(order, self.bot.state)
        await self.bot.on_private({'type': 'update', 'channel': 'balancePerps', 'data': []})
        for key in ('equity', 'available', 'used_margin'):
            self.assertEqual(D(self.bot.state.balances[key]), 0)
        self.assertIs(self.bot.state.balances['under_liquidation'], False)
        self.assertIsNone(self.bot.state.balances['margin_ratio'])
        self.assertIsNone(self.bot.current_margin_ratio())
        with self.assertRaises(RiskError):
            self.bot.check_order(order, self.bot.state)

    async def test_only_other_private_market_means_target_position_zero(self):
        self.bot.state.positions[MARKET] = D(2)
        account_positions = list(self.bot.state.account_positions)
        await self.bot.on_private({'type': 'update', 'channel': 'positionsPerps', 'data': [
            {'market': 'OTHER-USD.P', 'direction': 'long', 'netQuantity': '3'}]})
        self.assertEqual(self.bot.state.positions[MARKET], 0)
        self.assertEqual(self.bot.state.market_mode(MARKET), 'SYNCING')
        self.assertTrue(self.bot.reconcile_event.is_set())
        self.assertEqual(self.bot.state.account_positions, account_positions)

    async def test_invalid_nonempty_private_position_list_does_not_prove_zero(self):
        for rows in ([None], [{}], [{'market': ''}],
                     [{'market': 'OTHER-USD.P'}],
                     [{'market': 'OTHER-USD.P', 'quantity': 'NaN'}],
                     [{'market': 'OTHER-USD.P'}, {}]):
            self.bot.state.positions[MARKET] = D(2)
            self.alerts.messages.clear()
            with self.subTest(rows=rows):
                await self.bot.on_private({'type': 'update', 'channel': 'positionsPerps',
                                           'data': rows})
                self.assertEqual(self.bot.state.positions[MARKET], 2)
                self.assertTrue(self.alerts.messages)

    async def test_empty_private_orders_and_fills_are_no_events(self):
        revision = self.bot.state.revision
        self.alerts.messages.clear()
        for channel in ('ordersPerps', 'fillsPerps'):
            await self.bot.on_private({'type': 'update', 'channel': channel, 'data': []})
        self.assertEqual(self.bot.state.revision, revision)
        self.assertEqual(self.bot.state.market_mode(MARKET), 'RUNNING')
        self.assertFalse(self.alerts.messages)
