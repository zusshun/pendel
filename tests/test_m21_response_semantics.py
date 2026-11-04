"""M21：成功空结果按接口语义处理，格式异常保留真实 HTTP 状态。"""

import unittest

from pendle import ondo_api as api
import test_m20_empty_queries as empty_queries
from test_maker import MARKET


class M21ResponseSemanticsTests(unittest.IsolatedAsyncioTestCase):
    def client(self, payload, status=200):
        async def transport(request):
            return api.HttpResponse(status, payload)

        return api.OndoAPI(api.Credentials('测试-key', '测试-secret'),
                           transport=transport, rate=1_000_000)

    async def assert_format_error(self, awaitable, status=200, code=None):
        with self.assertRaises(api.ApiError) as caught:
            await awaitable
        error = caught.exception
        self.assertEqual(error.status, status)
        self.assertIn(f'HTTP {status}', str(error))
        self.assertIn('响应格式不符', str(error))
        self.assertEqual(error.category, api.classify_error(status, code))

    async def test_cancel_wrappers_accept_success_without_result(self):
        for method, argument in (('cancel_all', MARKET), ('cancel_order', 'client-one')):
            with self.subTest(method=method):
                self.assertIsNone(await getattr(self.client({'success': True}), method)(argument))

    async def test_all_four_cancel_endpoints_accept_success_without_result(self):
        for suffix in ('?market=' + MARKET, '/one', '/client:client-one', '/batch?orderIDs=one%2Ctwo'):
            with self.subTest(suffix=suffix):
                payload = await self.client({'success': True}).request('DELETE', api.ORDERS_PATH + suffix)
                self.assertIs(payload['success'], True)

    async def test_all_list_queries_default_missing_result_to_empty(self):
        for path in (api.ORDERS_PATH, api.POSITIONS_PATH, api.FILLS_PATH,
                     api.LEVERAGE_PATH, api.MARKETS_PATH):
            with self.subTest(path=path):
                result = await self.client({'success': True}).request('GET', path)
                self.assertEqual(result['result'], [])
        for method in ('get_open_orders', 'get_positions', 'get_fills'):
            arguments = () if method == 'get_positions' else (MARKET,)
            with self.subTest(method=method):
                self.assertEqual(await getattr(self.client({'success': True}), method)(*arguments), [])

    async def test_single_queries_default_missing_result_to_absence(self):
        for suffix in ('/one', '/client:client-one'):
            with self.subTest(suffix=suffix):
                result = await self.client({'success': True}).request('GET', api.ORDERS_PATH + suffix)
                self.assertIsNone(result['result'])
        self.assertIsNone(await self.client({'success': True}).get_order('client-one'))
        self.assertIsNone(await self.client({'success': True}).get_balances())

    async def test_order_missing_or_invalid_result_reports_real_status(self):
        for status in (200, 201):
            for extra in ({}, {'result': None}, {'result': []}, {'result': {}},
                          {'result': {'clientOrderId': 'one'}}):
                with self.subTest(status=status, extra=extra):
                    await self.assert_format_error(self.client({'success': True, **extra}, status)
                                                   .place_order({'market': MARKET}), status)

    async def test_malformed_envelopes_report_real_status_and_error_code(self):
        for payload in (None, [], {}, {'success': 1}, {'success': 'true'},
                        {'success': 1, 'error_code': 'insufficient_margin'}):
            for status in (200, 202, 401, 503):
                code = payload.get('error_code') if isinstance(payload, dict) else None
                with self.subTest(payload=payload, status=status):
                    await self.assert_format_error(self.client(payload, status).get_positions(), status, code)

    async def test_malformed_query_results_report_real_status(self):
        cases = (
            ('get_order', ('one',), ['bad']),
            ('get_open_orders', (MARKET,), [None]),
            ('get_open_orders', (MARKET,), [{'market': MARKET}]),
            ('get_positions', (), {}),
            ('get_positions', (), [None]),
            ('get_positions', (), [{'market': MARKET, 'quantity': 'bad'}]),
            ('get_balances', (), None),
            ('get_balances', (), {}),
            ('get_fills', (MARKET,), [{'id': 'one'}]),
            ('get_fills', (MARKET,), [{'id': 'one', 'orderId': 'o', 'clientOrderId': 'c',
                                     'price': '100', 'size': '1', 'side': 'buy', 'fee': '0',
                                     'pnl': '0', 'time': 'bad', 'isMaker': True}]),
        )
        for method, arguments, result in cases:
            with self.subTest(method=method, result=result):
                await self.assert_format_error(getattr(self.client(
                    {'success': True, 'result': result}, 202), method)(*arguments), 202)

    async def test_missing_result_does_not_hide_invalid_pagination(self):
        for metadata in ({'pageInfo': []}, {'pageInfo': {'nextCursor': 0}},
                         {'complete': False}, {'hasMore': True}):
            with self.subTest(metadata=metadata):
                await self.assert_format_error(self.client({'success': True, **metadata}, 202)
                                               .get_open_orders(MARKET), 202)

    async def test_single_order_does_not_hide_format_errors_as_absence(self):
        for payload in (None, [], {}, {'success': 'false', 'error_code': 'order_not_found'}):
            status = 400 if isinstance(payload, dict) and 'error_code' in payload else 404
            code = payload.get('error_code') if isinstance(payload, dict) else None
            with self.subTest(payload=payload, status=status):
                await self.assert_format_error(self.client(payload, status).get_order('one'), status, code)

    async def test_unknown_transport_response_has_no_invented_http_status(self):
        async def transport(request):
            return object()

        client = self.client({})
        client.transport = transport
        with self.assertRaises(api.ApiError) as caught:
            await client.get_positions()
        self.assertEqual(caught.exception.status, 0)
        self.assertIn('响应格式不符', str(caught.exception))
        self.assertNotIn('HTTP 502', str(caught.exception))


class M21MissingLeverageTests(unittest.IsolatedAsyncioTestCase):
    setUp = empty_queries.M20EmptyLeverageTests.setUp

    async def test_missing_leverage_result_uses_market_default(self):
        self.leverage = {'success': True}
        await self.bot.load_rules()
        self.assertEqual(str(self.bot.initial_margin_rate), '0.1')
