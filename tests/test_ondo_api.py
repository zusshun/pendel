"""In-memory behavior tests for live integration without opening connections or listening on ports."""

import asyncio
import json
import logging
import stat
import unittest

from pendle import ondo_api as api


SECRET = 'ondoApiSecret_documentation_example'
MARKET = 'IBM-USD.P'


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.waiters = []

    def monotonic(self):
        return self.now

    def time(self):
        return 1667472000.123 + self.now

    async def sleep(self, seconds):
        future = asyncio.get_running_loop().create_future()
        self.waiters.append((self.now + seconds, future))
        await future

    def advance(self, seconds):
        self.now += seconds
        for deadline, future in self.waiters:
            if deadline <= self.now and not future.done():
                future.set_result(None)


async def settle():
    for _ in range(15):
        await asyncio.sleep(0)


class SignatureTests(unittest.TestCase):
    def test_documented_rest_prehash_includes_secret_prefix_and_exact_body(self):
        body = '{"market":"IBM-USD.P","side":"buy","quantity":"2","price":"100"}'
        self.assertEqual(api.rest_signature(SECRET, 1667472000123, 'POST',
                         '/v1/perps/orders', body),
                         '5350d04ed9a1e39848952e613153c0b88f534f40415cfdd8aac6d1ca47f8d23f')

    def test_documented_query_is_signed_and_ws_uses_login_suffix(self):
        self.assertEqual(api.rest_signature(SECRET, 1667472000123, 'GET',
                         '/v1/perps/orders?market=IBM-USD.P&status=open'),
                         'af2343c28d8eb1ed59b26a0e90c921f730af438a772c9ce904f3fdf4d3226953')
        self.assertEqual(api.ws_login_signature(SECRET, 1667472000123),
                         'a4af11cda15148db969a848b58992a79ebaf3a1a099712088bec4353b85cd040')

    def test_credentials_environment_and_exact_file_permission(self):
        creds = api.load_credentials(env={'ONDO_API_KEY': 'key', 'ONDO_API_SECRET': SECRET})
        self.assertEqual(creds.secret, SECRET)
        self.assertNotIn(SECRET, repr(creds))
        content = json.dumps({'key': 'key', 'secret': SECRET})
        for mode in (0o400, 0o640, 0o660, 0o777):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                api.load_credentials(env={}, secret_file='fake',
                                     file_reader=lambda p: (stat.S_IFREG | mode, content))
        self.assertEqual(api.load_credentials(env={}, secret_file='fake',
                         file_reader=lambda p: (stat.S_IFREG | 0o600, content)).key, 'key')

    def test_log_filter_removes_credentials_and_signatures(self):
        record = logging.LogRecord('maker', logging.ERROR, '', 0,
                                   'key=%s secret=%s ONDO-SIGN=%s',
                                   ('key123', SECRET, 'a' * 64), None)
        api.SecretFilter(api.Credentials('key123', SECRET)).filter(record)
        text = record.getMessage()
        for value in ('key123', SECRET, 'a' * 64):
            self.assertNotIn(value, text)

    def test_error_classification_matches_design_and_collision_context(self):
        groups = {'recompute': ['post_only_has_match'],
                  'guard': ['order_price_outside_safe_bounds', 'mark_price_not_available',
                            'insufficient_liquidity', 'must_use_delayed_path',
                            'trading_disabled', 'something_new'],
                  'halt': ['ip_not_permitted', 'key_doesnt_have_scope',
                           'account_in_liquidation', 'insufficient_margin',
                           'net_position_too_large', 'reduce_only_flipping_position',
                           'reduce_only_increasing_position', 'timestamp_too_far'],
                  'reconcile': ['reduce_only_no_open_position'],
                  'reduce_only_fallback': ['reduce_only_invalid_tif']}
        for category, codes in groups.items():
            for code in codes:
                with self.subTest(code=code):
                    self.assertEqual(api.classify_error(400, code), category)
        self.assertEqual(api.classify_error(401, 'unknown'), 'halt')
        self.assertEqual(api.classify_error(503, None), 'guard')
        self.assertEqual(api.classify_error(400, 'clientOrderID_collision'), 'reconcile')
        self.assertEqual(api.classify_error(400, 'clientOrderID_collision', order_found=False), 'halt')
        self.assertEqual(api.classify_error(404, None, cancelling=True), 'reconcile')
        self.assertEqual(api.classify_error(400, 'order_already_filled', cancelling=True), 'reconcile')
        self.assertEqual(api.classify_error(404, None), 'guard')


class ClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.sent = []
        self.replies = []
        self.gate = api.SendGate()

        async def transport(request):
            self.sent.append(request)
            result = {'orderId': 'one'} if request.method == 'POST' else []
            return self.replies.pop(0) if self.replies else api.HttpResponse(200, {'success': True, 'result': result})
        self.client = api.OndoAPI(api.Credentials('key', SECRET), transport=transport,
                                 gate=self.gate, clock=self.clock.time,
                                 monotonic=self.clock.monotonic, sleep=self.clock.sleep)

    async def test_non_whitelisted_paths_never_reach_transport(self):
        paths = [('GET', '/v1/time'), ('POST', '/v1/withdrawals'), ('GET', '/v1/wallet'),
                 ('POST', '/v1/perps/positions'), ('DELETE', '/v1/perps/orders'),
                 ('PUT', '/v1/perps/orders'), ('GET', 'https://evil/v1/markets'),
                 ('GET', '/v1/perps/orders/../withdrawals'), ('GET', '/v1/markets/'),
                 ('DELETE', '/v1/perps/orders/client:abc/extra'),
                 ('DELETE', '/v1/perps/orders/batch'), ('GET', '/v1/api_keys'),
                 ('POST', '/v1/perps/orders?x=1'), ('GET', '/v1/%6darkets')]
        for method, path in paths:
            with self.subTest(path=path), self.assertRaises(api.EndpointNotAllowed):
                await self.client.request(method, path)
        self.assertEqual(self.sent, [])

    async def test_actual_wire_bytes_and_query_match_signature(self):
        await self.client.request('GET', api.ORDERS_PATH,
                                  params={'market': MARKET, 'status': 'open'})
        request = self.sent[0]
        self.assertTrue(request.url.startswith('https://api.ondoperps-sandbox.xyz/'))
        self.assertEqual(request.headers['ONDO-SIGN'],
                         'af2343c28d8eb1ed59b26a0e90c921f730af438a772c9ce904f3fdf4d3226953')
        self.assertEqual(request.body, '')
        self.assertNotIn(SECRET, repr(request))

    async def test_closed_gate_exception_budget_is_global_and_cannot_reset(self):
        self.gate.close()
        for _ in range(3):
            await self.client.cancel_all(MARKET, stop=True)
            task = asyncio.create_task(self.client.get_open_orders(MARKET, stop=True))
            await settle()
            self.clock.advance(1)
            await settle()
            await task
            self.clock.advance(1)
        self.gate.close()
        for call in (self.client.cancel_all('OTHER', stop=True),
                     self.client.get_open_orders('OTHER', stop=True),
                     self.client.request('GET', api.ORDERS_PATH + '/client:abc', stop=True)):
            with self.assertRaises(api.GateClosed):
                await call
        self.gate.finish_stop()
        with self.assertRaises(api.GateClosed):
            await self.client.cancel_all(MARKET, stop=True)

    async def test_rate_queue_priorities_include_reduce_only_and_cancels(self):
        calls = [self.client.request('GET', api.MARKETS_PATH),
                 self.client.place_order({'market': MARKET}),
                 self.client.cancel_order('one'),
                 self.client.place_order({'market': MARKET, 'reduceOnly': True}),
                 self.client.cancel_all(MARKET)]
        tasks = [asyncio.create_task(call) for call in calls]
        for _ in range(5):
            await settle()
            self.clock.advance(1)
        await asyncio.gather(*tasks)
        self.assertEqual([(row.method, row.path_query.partition('?')[0]) for row in self.sent],
                         [('DELETE', api.ORDERS_PATH), ('POST', api.ORDERS_PATH),
                          ('DELETE', api.ORDERS_PATH + '/client:one'),
                          ('POST', api.ORDERS_PATH), ('GET', api.MARKETS_PATH)])
        self.assertTrue(json.loads(self.sent[1].body)['reduceOnly'])
        self.assertNotIn('reduceOnly', json.loads(self.sent[3].body))

    async def test_429_wait_is_interrupted_by_cancel_and_other_requests_wait_full_delay(self):
        self.replies.append(api.HttpResponse(429, {'error': 'limited'}, {'Retry-After': '10'}))
        with self.assertRaises(api.ApiError):
            await self.client.request('GET', api.MARKETS_PATH)
        quote = asyncio.create_task(self.client.place_order({'market': MARKET}))
        await settle()
        cancel = asyncio.create_task(self.client.cancel_all(MARKET))
        await settle()
        self.clock.advance(1)
        await settle()
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.sent[-1].method, 'DELETE')
        await cancel
        self.clock.advance(8)
        await settle()
        self.assertFalse(quote.done())
        self.clock.advance(1)
        await settle()
        await quote
        self.assertEqual(len(self.sent), 3)

    async def test_gate_closes_while_waiting_for_token_without_sending_or_callback(self):
        await self.client.request('GET', api.MARKETS_PATH)
        callbacks = []
        task = asyncio.create_task(self.client.place_order({'market': MARKET},
                                    before_send=lambda: callbacks.append('sent')))
        await settle()
        self.gate.close()
        await settle()
        with self.assertRaises(api.GateClosed):
            await task
        self.clock.advance(10)
        await settle()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(callbacks, [])

    async def test_cancelled_queued_request_never_reaches_transport(self):
        await self.client.request('GET', api.MARKETS_PATH)
        task = asyncio.create_task(self.client.place_order({'market': MARKET}))
        await settle()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.clock.advance(10)
        await settle()
        self.assertEqual(len(self.sent), 1)

    async def test_sync_callback_runs_at_wire_start_and_can_refuse_order(self):
        def refuse():
            raise ValueError('风控拒绝')
        with self.assertRaisesRegex(ValueError, '风控拒绝'):
            await self.client.place_order({'market': MARKET}, before_send=refuse)
        self.assertEqual(self.sent, [])

    async def test_hung_write_does_not_block_stop_and_times_out_with_fake_clock(self):
        hold = asyncio.Event()
        async def transport(request):
            self.sent.append(request)
            if request.method == 'POST':
                await hold.wait()
            return api.HttpResponse(200, {'success': True, 'result': []})
        self.client.transport = transport
        quote = asyncio.create_task(self.client.place_order({'market': MARKET}))
        await settle()
        self.gate.close()
        cancel = asyncio.create_task(self.client.cancel_all(MARKET, stop=True))
        self.clock.advance(1)
        await settle()
        self.assertEqual([row.method for row in self.sent], ['POST', 'DELETE'])
        await cancel
        self.clock.advance(4)
        await settle()
        with self.assertRaises(api.TransportError):
            await quote

    async def test_empty_post_body_is_rejected_before_queue_and_does_not_poison_cancel(self):
        task = asyncio.create_task(self.client.request('POST', api.ORDERS_PATH))
        await settle()
        self.assertTrue(task.done())
        with self.assertRaises(ValueError):
            await task
        await self.client.cancel_all(MARKET)
        self.assertEqual(len(self.sent), 1)

    async def test_gate_blocks_public_private_ws_and_reconnections(self):
        seen = []
        async def send(value):
            seen.append(value)
        await self.gate.send_ws(send, 'ping')
        self.gate.close()
        for call in (self.gate.send_ws(send, 'private'), self.gate.send_ws(send, 'public'),
                     self.gate.connect(send, 'reconnect')):
            with self.assertRaises(api.GateClosed):
                await call
        self.assertEqual(seen, ['ping'])

    async def test_complete_open_orders_required_and_404_is_none(self):
        self.replies.extend([api.HttpResponse(200, {'success': True, 'result': [], 'hasMore': True}),
                             api.HttpResponse(404, {'error': 'not_found'})])
        with self.assertRaises(api.ApiError):
            await self.client.get_open_orders(MARKET)
        task = asyncio.create_task(self.client.get_order('unknown'))
        await settle()
        self.clock.advance(1)
        await settle()
        self.assertIsNone(await task)

    async def test_error_delivery_does_not_corrupt_active_dispatch_coroutine(self):
        errors = []
        loop = asyncio.get_running_loop()
        previous = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: errors.append(context))
        try:
            self.replies.append(api.HttpResponse(429, {'error': 'limited'}))
            with self.assertRaises(api.ApiError):
                await self.client.request('GET', api.MARKETS_PATH)
            await settle()
            self.assertEqual(errors, [])
        finally:
            loop.set_exception_handler(previous)

    async def test_time_check_excludes_queue_delay(self):
        from email.utils import formatdate
        await self.client.request('GET', api.MARKETS_PATH)
        async def transport(request):
            self.sent.append(request)
            return api.HttpResponse(200, {'success': True, 'result': []},
                {'Date': formatdate(self.clock.time(), usegmt=True)})
        self.client.transport = transport
        self.client.ntp_check = lambda: True
        task = asyncio.create_task(self.client.sync_time())
        await settle()
        self.clock.advance(10)
        await settle()
        self.assertEqual(await task, 0)
        self.assertTrue(self.client.clock_verified)

    async def test_timestamp_error_does_not_retry_body_or_before_send(self):
        callbacks = []
        self.replies.append(api.HttpResponse(400, {'error': 'timestamp_too_far'}))
        with self.assertRaises(api.ApiError) as caught:
            await self.client.place_order({'market': MARKET}, before_send=lambda: callbacks.append('send'))
        self.assertEqual(caught.exception.category, 'halt')
        self.assertEqual(callbacks, ['send'])
        self.assertEqual(len(self.sent), 1)

    def test_linux_ntp_status_parser_fails_closed(self):
        from types import SimpleNamespace
        calls = []
        def run(command, **kwargs):
            calls.append(command)
            return SimpleNamespace(returncode=0, stdout='NTPSynchronized=yes\n')
        self.assertTrue(api.check_ntp_synchronized(run=run))
        self.assertEqual(calls, [['timedatectl', 'show', '-p', 'NTPSynchronized']])
        self.assertFalse(api.check_ntp_synchronized(run=lambda *a, **k:
            SimpleNamespace(returncode=0, stdout='NTPSynchronized=no')))
        self.assertFalse(api.check_ntp_synchronized(run=lambda *a, **k:
            SimpleNamespace(returncode=1, stdout='NTPSynchronized=yes')))

    async def test_all_allowlisted_endpoints_and_production_addresses(self):
        self.assertEqual(api.ENVIRONMENTS['prod'],
                         ('https://api.ondoperps.xyz', 'wss://api.ondoperps.xyz/ws'))
        endpoints = [('POST', api.ORDERS_PATH, {'market': MARKET}),
                     ('DELETE', api.ORDERS_PATH + '?market=' + MARKET, None),
                     ('DELETE', api.ORDERS_PATH + '/123', None),
                     ('DELETE', api.ORDERS_PATH + '/client:abc', None),
                     ('DELETE', api.ORDERS_PATH + '/batch?orderIDs=1%2C2', None),
                     *[('GET', path, None) for path in
                       (api.ORDERS_PATH, api.ORDERS_PATH + '/123',
                        api.ORDERS_PATH + '/client:abc', api.POSITIONS_PATH,
                        api.BALANCES_PATH, api.LEVERAGE_PATH, api.MARKETS_PATH,
                        api.FILLS_PATH)]]
        for method, path, body in endpoints:
            task = asyncio.create_task(self.client.request(method, path, body=body))
            await settle()
            self.clock.advance(1)
            await settle()
            await task
        self.assertEqual(len(self.sent), len(endpoints))

    async def test_body_serialization_is_exactly_the_signed_wire_body(self):
        body = {'market': MARKET, 'side': 'buy', 'quantity': '2', 'price': '100'}
        await self.client.place_order(body)
        request = self.sent[0]
        self.assertEqual(request.body,
                         '{"market":"IBM-USD.P","side":"buy","quantity":"2","price":"100"}')
        self.assertEqual(request.headers['ONDO-SIGN'],
                         '5350d04ed9a1e39848952e613153c0b88f534f40415cfdd8aac6d1ca47f8d23f')

    async def test_gate_final_check_blocks_callback_that_closes_gate(self):
        with self.assertRaises(api.GateClosed):
            await self.client.place_order({'market': MARKET}, before_send=self.gate.close)
        self.assertEqual(self.sent, [])

    async def test_server_timestamp_error_halts_while_write_result_stays_uncertain(self):
        self.replies.append(api.HttpResponse(503, {'error': 'timestamp_too_far'}))
        task = asyncio.create_task(self.client.place_order({'market': MARKET}))
        await settle()
        self.assertTrue(task.done())
        with self.assertRaises(api.ApiError) as caught:
            await task
        self.assertEqual(caught.exception.category, 'halt')
        self.assertEqual(len(self.sent), 1)

    async def test_auth_error_cannot_trigger_timestamp_retry(self):
        self.replies.append(api.HttpResponse(401, {'error': 'timestamp_too_far'}))
        task = asyncio.create_task(self.client.place_order({'market': MARKET}))
        await settle()
        self.assertTrue(task.done())
        with self.assertRaises(api.ApiError) as caught:
            await task
        self.assertEqual(caught.exception.category, 'halt')
        self.assertEqual(len(self.sent), 1)

    async def test_cancellation_resistant_write_remains_visible_after_local_timeout(self):
        hold = asyncio.Event()
        async def transport(request):
            self.sent.append(request)
            try:
                await hold.wait()
            except asyncio.CancelledError:
                await hold.wait()
            return api.HttpResponse(200, {'success': True, 'result': {'orderId': 'late'}})
        self.client.transport = transport
        task = asyncio.create_task(self.client.place_order({'market': MARKET}))
        await settle()
        self.assertEqual(self.client.in_flight_writes, 1)
        self.clock.advance(5)
        await settle()
        with self.assertRaises(api.TransportError):
            await task
        self.assertEqual(self.client.in_flight_writes, 1)
        hold.set()
        await settle()
        self.assertEqual(self.client.in_flight_writes, 0)

    async def test_429_with_non_json_body_still_pauses_non_cancel_requests(self):
        self.replies.append(api.HttpResponse(429, None, {'Retry-After': '60'}))
        with self.assertRaises(api.ApiError):
            await self.client.request('GET', api.MARKETS_PATH)
        task = asyncio.create_task(self.client.place_order({'market': MARKET}))
        await settle()
        self.clock.advance(1)
        await settle()
        self.assertEqual(len(self.sent), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_retry_after_is_checked_again_at_actual_transport_start(self):
        callbacks = []
        quote = asyncio.create_task(self.client.place_order({'market': MARKET},
                                    before_send=lambda: callbacks.append('sent')))
        # Pause exactly where the queue has selected a request but the transport task has not run.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.client._blocked_until = 10
        await settle()
        self.assertEqual(self.sent, [])
        self.assertEqual(callbacks, [])
        cancel = asyncio.create_task(self.client.cancel_all(MARKET))
        await settle()
        self.clock.advance(1)
        await settle()
        await cancel
        self.clock.advance(9)
        await settle()
        await quote
        self.assertEqual([row.method for row in self.sent], ['DELETE', 'POST'])

    async def test_transport_library_errors_never_expose_request_authentication(self):
        async def transport(request):
            raise RuntimeError(SECRET + ' ' + request.headers['ONDO-SIGN'])
        self.client.transport = transport
        with self.assertRaises(api.TransportError) as caught:
            await self.client.place_order({'market': MARKET})
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertNotIn(SECRET, repr(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    async def test_authentication_code_on_404_cannot_be_hidden_as_order_absence(self):
        self.replies.append(api.HttpResponse(404, {'error': 'ip_not_permitted'}))
        with self.assertRaises(api.ApiError) as caught:
            await self.client.get_order('unknown')
        self.assertEqual(caught.exception.category, 'halt')

    async def test_cursor_zero_and_non_boolean_completeness_never_prove_empty_book(self):
        for metadata in ({'cursor': 0}, {'hasMore': 0}, {'truncated': 'false'}, {'complete': False}):
            self.replies.append(api.HttpResponse(200, {'success': True, 'result': [], **metadata}))
            task = asyncio.create_task(self.client.get_open_orders(MARKET))
            await settle()
            self.clock.advance(1)
            await settle()
            with self.assertRaises(api.ApiError):
                await task

    async def test_transport_self_cancellation_resolves_as_unknown_transport_error(self):
        async def transport(request):
            self.sent.append(request)
            raise asyncio.CancelledError()
        self.client.transport = transport
        task = asyncio.create_task(self.client.place_order({'market': MARKET}))
        await settle()
        self.assertTrue(task.done())
        with self.assertRaises(api.TransportError) as caught:
            await task
        self.assertEqual(caught.exception.status, 0)
        self.assertEqual(self.client.in_flight_writes, 0)
        self.assertEqual(len(self.sent), 1)

    async def test_caller_cancellation_preserves_cancel_semantics_and_never_retries_write(self):
        hold = asyncio.Event()
        async def transport(request):
            self.sent.append(request)
            await hold.wait()
            return api.HttpResponse(200, {'success': True, 'result': {'orderId': 'late'}})
        self.client.transport = transport
        task = asyncio.create_task(self.client.place_order({'market': MARKET}))
        await settle()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.client.in_flight_writes, 1)
        hold.set()
        await settle()
        self.assertEqual(self.client.in_flight_writes, 0)
        self.assertEqual(len(self.sent), 1)


class OfficialInterfaceRegressionTests(unittest.IsolatedAsyncioTestCase):
    setUp = ClientTests.setUp
    async def drive(self, awaitable, steps=12):
        task = asyncio.create_task(awaitable)
        for _ in range(steps):
            await settle()
            if task.done():
                return await task
            self.clock.advance(1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.fail('假时钟预算耗尽')

    async def test_official_authentication_headers(self):
        await self.client.get_open_orders(MARKET)
        headers = self.sent[0].headers
        self.assertEqual(headers.get('ONDO-KEY-ID'), 'key')
        self.assertEqual(headers.get('ONDO-TIMESTAMP'), '1667472000123')
        self.assertNotIn('ONDO-KEY', headers)
        self.assertNotIn('ONDO-TS', headers)

    async def test_official_balance_mapping_keeps_exchange_margin_ratio(self):
        self.replies.append(api.HttpResponse(200, {'success': True, 'result': {
            'marginBalance': '1000', 'availableMargin': '750', 'usedMargin': '250',
            'marginRatio': '0.27', 'underLiquidation': False}}))
        balance = await self.client.get_balances()
        self.assertEqual(self.sent[0].path_query, '/v1/perps/balance')
        self.assertEqual(balance['equity'], '1000')
        self.assertEqual(balance['available'], '750')
        self.assertEqual(balance['margin_ratio'], '0.27')
        self.assertEqual(balance['used_margin'], '250')
        self.assertFalse(balance['under_liquidation'])

    async def test_official_order_not_found_400_is_absence(self):
        self.replies.append(api.HttpResponse(400, {'success': False, 'error_code': 'order_not_found'}))
        self.assertIsNone(await self.client.get_order('missing'))

    async def test_paginated_open_orders_reads_all_pages(self):
        self.replies.extend([
            api.HttpResponse(200, {'success': True, 'result': [], 'pageInfo': {'nextCursor': 'next page'}}),
            api.HttpResponse(200, {'success': True, 'result': [
                {'market': MARKET, 'orderId': 'one', 'clientOrderId': 'client-one'}],
                'pageInfo': {'nextCursor': None}})])
        rows = await self.drive(self.client.get_open_orders(MARKET))
        self.assertEqual(len(rows), 1)
        self.assertIn('cursor=next+page', self.sent[1].path_query)

    async def test_second_open_order_page_failure_never_proves_empty(self):
        self.replies.extend([
            api.HttpResponse(200, {'success': True, 'result': [], 'pageInfo': {'nextCursor': 'next'}}),
            api.HttpResponse(503, {'error_code': 'unavailable'})])
        with self.assertRaises(api.ApiError):
            await self.drive(self.client.get_open_orders(MARKET))

    async def test_repeated_cursor_never_proves_empty(self):
        self.replies.extend([api.HttpResponse(200, {'success': True, 'result': [],
                             'pageInfo': {'nextCursor': 'same'}})] * 2)
        with self.assertRaises(api.ApiError):
            await self.drive(self.client.get_open_orders(MARKET))

    async def test_position_direction_is_signed_and_has_no_invented_timestamp(self):
        self.replies.append(api.HttpResponse(200, {'success': True, 'result': [
            {'market': MARKET, 'direction': 'short', 'netQuantity': '2.5',
             'markPrice': '100', 'unrealizedPnl': '-3'},
            {'market': 'NEUTRAL', 'direction': 'neutral', 'netQuantity': '9'}]}))
        positions = await self.client.get_positions()
        self.assertEqual(str(positions[0]['quantity']), '-2.5')
        self.assertEqual(positions[0]['mark_price'], '100')
        self.assertEqual(positions[0]['unrealized_pnl'], '-3')
        self.assertEqual(positions[1]['quantity'], 0)
        self.assertNotIn('time_ms', positions[0])

    async def test_fill_fields_are_normalized_and_paginated(self):
        fill = {'id': 'f1', 'orderId': 'o1', 'clientOrderId': 'c1', 'price': '101',
                'size': '2', 'side': 'sell', 'fee': '.1', 'pnl': '3',
                'time': 1667472000000, 'isMaker': True}
        self.replies.extend([
            api.HttpResponse(200, {'success': True, 'result': [fill], 'pageInfo': {'nextCursor': 'n'}}),
            api.HttpResponse(200, {'success': True, 'result': [], 'pageInfo': {'nextCursor': None}})])
        fills = await self.drive(self.client.get_fills(MARKET, start_time=123, end_time=456))
        self.assertEqual(fills[0], {'fill_id': 'f1', 'market': MARKET, 'order_id': 'o1',
            'client_order_id': 'c1', 'price': '101', 'quantity': '2', 'side': 'sell',
            'fee': '.1', 'realized_pnl': '3', 'time_ms': 1667472000000, 'is_maker': True})
        self.assertIn('startTime=123&endTime=456', self.sent[0].path_query)
        self.assertIn('cursor=n', self.sent[1].path_query)

    async def test_time_check_requires_ntp_and_date_without_server_time_endpoint(self):
        from email.utils import formatdate
        self.client.ntp_check = lambda: True
        self.replies.append(api.HttpResponse(200, {'success': True, 'result': []},
                            {'Date': formatdate(self.clock.time(), usegmt=True)}))
        self.assertEqual(await self.client.sync_time(), 0)
        self.assertTrue(self.client.clock_verified)
        self.assertEqual(self.sent[0].path_query, api.MARKETS_PATH)
        self.assertFalse(hasattr(api, 'TIME_PATH'))

    async def test_unsynchronized_ntp_or_missing_date_refuses_time_check(self):
        self.client.ntp_check = lambda: False
        with self.assertRaises(api.ApiError) as caught:
            await self.client.sync_time()
        self.assertEqual(caught.exception.category, 'halt')
        self.assertFalse(self.sent)
        self.client.ntp_check = lambda: True
        with self.assertRaises(api.ApiError) as caught:
            await self.client.sync_time()
        self.assertEqual(caught.exception.category, 'halt')

    async def test_date_clock_difference_over_two_seconds_refuses_time_check(self):
        from email.utils import formatdate
        self.client.ntp_check = lambda: True
        self.replies.append(api.HttpResponse(200, {'success': True, 'result': []},
                            {'Date': formatdate(self.clock.time() - 3, usegmt=True)}))
        with self.assertRaises(api.ApiError) as caught:
            await self.client.sync_time()
        self.assertEqual(caught.exception.category, 'halt')

    async def test_timestamp_error_halts_immediately_without_order_retry(self):
        self.replies.append(api.HttpResponse(400, {'error_code': 'timestamp_too_far'}))
        with self.assertRaises(api.ApiError) as caught:
            await self.client.place_order({'market': MARKET})
        self.assertEqual(caught.exception.category, 'halt')
        self.assertEqual(len(self.sent), 1)


class HaltedTradingGateTests(unittest.IsolatedAsyncioTestCase):
    setUp = ClientTests.setUp

    def body(self, **changes):
        return {'market': MARKET, 'side': 'sell', 'quantity': '1', 'price': '100',
                'reduceOnly': True, 'timeInForce': 'IOC', 'postOnly': False, **changes}

    async def test_trade_halt_keeps_reads_and_cancels_but_rejects_normal_post(self):
        self.gate.halt_trading()
        self.assertTrue(self.gate.is_open)
        self.assertTrue(self.gate.trading_halted)
        with self.assertRaises(api.GateClosed):
            await self.client.place_order(self.body())
        await self.client.get_open_orders(MARKET)
        cancel = asyncio.create_task(self.client.cancel_all(MARKET))
        await settle()
        self.clock.advance(1)
        await settle()
        await cancel
        self.assertEqual([request.method for request in self.sent], ['GET', 'DELETE'])

    async def test_only_explicit_reduce_only_ioc_can_pass_emergency_channel(self):
        self.gate.halt_trading()
        for change in ({'reduceOnly': False}, {'reduceOnly': 'true'}, {'postOnly': True},
                       {'timeInForce': 'GTC'}, {'postOnly': 0}):
            with self.subTest(change=change), self.assertRaises(api.GateClosed):
                await self.client.place_order(self.body(**change), emergency=True)
        await self.client.place_order(self.body(), emergency=True)
        self.assertEqual(len(self.sent), 1)

    async def test_emergency_cannot_bypass_full_gate_closure(self):
        self.gate.halt_trading()
        self.gate.close()
        with self.assertRaises(api.GateClosed):
            await self.client.place_order(self.body(), emergency=True)
        self.gate.finish_stop()
        self.gate.halt_trading()
        with self.assertRaises(api.GateClosed):
            await self.client.place_order(self.body(), emergency=True)
        self.assertFalse(self.sent)

    async def test_emergency_requires_prior_trade_halt(self):
        with self.assertRaises(api.GateClosed):
            await self.client.place_order(self.body(), emergency=True)
        with self.assertRaises(api.GateClosed):
            await self.client.request('GET', api.MARKETS_PATH, emergency=True)
        self.assertFalse(self.sent)

    async def test_trade_halt_rejects_already_queued_normal_order(self):
        await self.client.request('GET', api.MARKETS_PATH)
        callback = []
        pending = asyncio.create_task(self.client.place_order(self.body(),
            before_send=lambda: callback.append('sent')))
        await settle()
        self.gate.halt_trading()
        await settle()
        with self.assertRaises(api.GateClosed):
            await pending
        self.clock.advance(10)
        await settle()
        self.assertEqual(len(self.sent), 1)
        self.assertFalse(callback)

    async def test_final_validation_blocks_halt_or_full_close_inside_risk_callback(self):
        with self.assertRaises(api.GateClosed):
            await self.client.place_order(self.body(), before_send=self.gate.halt_trading)
        emergency = asyncio.create_task(self.client.place_order(self.body(), emergency=True,
            before_send=self.gate.close))
        await settle()
        self.clock.advance(1)
        await settle()
        with self.assertRaises(api.GateClosed):
            await emergency
        self.assertFalse(self.sent)
