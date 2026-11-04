"""M22：离线验证官方订单数量字段与拒单诊断。"""

import json
import logging
import unittest
from decimal import Decimal as D

import test_m3_wiring as wiring
from pendle.maker_orders import AccountState, OrderManager, OrderStateError, OrderStatus
from pendle.ondo_api import ApiError, Credentials, HttpResponse, OndoAPI
from test_maker import MARKET


def documented_order(cid, *, size='0.07', filled='0', status='OPEN', at=10):
    """数量采用文档字段，累计成交与末笔成交、成交金额相互独立。"""
    return {'orderId': 'exchange-' + cid, 'clientOrderId': cid, 'market': MARKET,
            'side': 'buy', 'type': 'limit', 'price': '250.63', 'size': size,
            'filledSize': filled, 'lastFillSize': '0.01' if D(filled) else '0',
            'filledCost': str(D(filled) * D('250.63')), 'status': status,
            'time_ms': at}


class M22OrderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.requests = []
        self.response = None

        async def transport(request):
            self.requests.append(request)
            if self.response is not None:
                return self.response
            body = json.loads(request.body)
            status = 'CANCELED' if body['timeInForce'] == 'IOC' else 'OPEN'
            return HttpResponse(200, {'success': True, 'result': documented_order(
                body['clientOrderId'], status=status)})

        self.api = OndoAPI(Credentials('测试-key', '测试-secret'), transport=transport,
                           rate=1_000_000)
        self.state = AccountState([MARKET])
        self.state.deadman_ack()
        self.state.modes[MARKET] = 'RUNNING'
        self.manager = OrderManager(self.state, self.api, risk_check=lambda *args: None)

    async def submit(self, *, ioc=False, emergency=False):
        if emergency:
            self.api.gate.halt_trading()
            self.state.stopping = True
            self.state.modes[MARKET] = 'HALTED'
        order = self.manager.create_intent(MARKET, 'buy', '0.07', '250.63',
            reduce_only=ioc, time_in_force='IOC' if ioc else 'GTC',
            post_only=not ioc, emergency=emergency)
        await self.manager.submit(order)
        return order

    def assert_payload(self, *, ioc):
        self.assertEqual(len(self.requests), 1)
        request = self.requests[0]
        self.assertEqual((request.method, request.path_query), ('POST', '/v1/perps/orders'))
        body = json.loads(request.body)
        self.assertEqual(body.get('size'), '0.07')
        self.assertEqual(body.get('type'), 'limit')
        self.assertNotIn('quantity', body)
        self.assertEqual(body['timeInForce'], 'IOC' if ioc else 'GTC')
        self.assertIs(body['reduceOnly'], ioc)
        self.assertIs(body['postOnly'], not ioc)

    async def test_gtc_payload_uses_size_and_explicit_limit_type(self):
        order = await self.submit()
        self.assert_payload(ioc=False)
        self.assertEqual(order.status, OrderStatus.OPEN)

    async def test_reduce_only_ioc_payload_uses_size_and_limit_type(self):
        order = await self.submit(ioc=True)
        self.assert_payload(ioc=True)
        self.assertEqual(order.status, OrderStatus.CANCELED)

    async def test_emergency_ioc_payload_uses_size_and_limit_type(self):
        order = await self.submit(ioc=True, emergency=True)
        self.assert_payload(ioc=True)
        self.assertEqual(order.status, OrderStatus.CANCELED)

    async def test_documented_rest_response_uses_cumulative_filled_size(self):
        order = self.manager.create_intent(MARKET, 'buy', '0.07', '250.63')
        row = documented_order(order.client_order_id, filled='0.03', status='PARTIAL')
        row.update(quantity='999', filledQuantity='999')
        try:
            parsed = await self.manager.handle_order_event(row, source='rest')
        except OrderStateError as exc:
            self.fail(f'官方 size/filledSize 订单应成功解析：{exc}')
        self.assertIs(parsed, order)
        self.assertEqual((order.quantity, order.filled, order.remaining),
                         (D('0.07'), D('0.03'), D('0.04')))
        self.assertEqual(order.event_filled, D('0.03'))
        self.assertEqual(self.state.positions[MARKET], 0)
        self.assertEqual(self.state.fills, {})

    async def test_zero_filled_size_is_authoritative(self):
        order = self.manager.create_intent(MARKET, 'buy', '0.07', '250.63')
        row = documented_order(order.client_order_id)
        row.update(quantity='0', filledQuantity='0.07', lastFillSize='0.07', filledCost='17.5441')
        try:
            await self.manager.handle_order_event(row, source='rest')
        except OrderStateError as exc:
            self.fail(f'filledSize 为零的官方订单应成功解析：{exc}')
        self.assertEqual((order.status, order.filled), (OrderStatus.OPEN, D(0)))

    async def test_rest_error_preserves_exchange_error_text(self):
        self.response = HttpResponse(400, {'success': False, 'error_code': 'order_invalid_size',
                                           'error': 'order quantity is zero'})
        with self.assertRaises(ApiError) as caught:
            await self.api.place_order({'market': MARKET})
        self.assertEqual(caught.exception.code, 'order_invalid_size')
        self.assertEqual(getattr(caught.exception, 'error', None), 'order quantity is zero')

    async def test_4xx_rejection_logs_warning_with_exchange_code_and_text(self):
        self.response = HttpResponse(400, {'success': False, 'error_code': 'order_invalid_size',
                                           'error': 'order quantity is zero'})
        with self.assertLogs('pendle.maker', level=logging.WARNING) as captured:
            order = await self.submit()
        self.assertEqual(order.status, OrderStatus.REJECTED)
        warning = '\n'.join(captured.output)
        for value in ('400', 'order_invalid_size', 'order quantity is zero'):
            self.assertIn(value, warning)

    async def test_exchange_error_text_is_redacted_before_diagnostics(self):
        self.response = HttpResponse(400, {'success': False, 'error_code': 'order_invalid_size',
            'error': 'order quantity is zero 测试-key 测试-secret sign=' + 'a' * 64})
        with self.assertRaises(ApiError) as caught:
            await self.api.place_order({'market': MARKET})
        text = getattr(caught.exception, 'error', None)
        self.assertIsInstance(text, str)
        self.assertIn('order quantity is zero', text)
        for secret in ('测试-key', '测试-secret', 'a' * 64):
            self.assertNotIn(secret, text)


class M22MakerDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = wiring.M3WiringTests.asyncSetUp
    asyncTearDown = wiring.M3WiringTests.asyncTearDown
    wall = wiring.M3WiringTests.wall
    drive = wiring.M3WiringTests.drive
    ready = wiring.M3WiringTests.ready
    publish = wiring.M3WiringTests.publish

    async def test_documented_ws_order_uses_size_and_filled_size_without_booking_twice(self):
        await self.ready()
        order = self.bot.manager.create_intent(MARKET, 'buy', '0.07', '250.63')
        row = documented_order(order.client_order_id, filled='0.03', status='PARTIAL',
                               at=int(self.wall() * 1000))
        await self.drive(self.bot.on_private({'channel': 'ordersPerps', 'data': [row]}))
        self.assertEqual((order.status, order.quantity, order.filled),
                         (OrderStatus.PARTIAL, D('0.07'), D('0.03')))
        self.assertEqual(self.bot.state.positions[MARKET], 0)
        self.assertEqual(self.bot.state.fills, {})

    async def test_4xx_rejection_reaches_health_pause_reason(self):
        await self.ready()
        original = self.bot.api.transport

        async def transport(request):
            if request.method == 'POST':
                self.requests.append(request)
                return HttpResponse(400, {'success': False, 'error_code': 'order_invalid_size',
                                          'error': 'order quantity is zero'})
            return await original(request)

        self.bot.api.transport = transport
        with self.assertLogs(self.bot.logger, level=logging.WARNING) as captured:
            await self.drive(self.bot.tick())
        self.assertTrue(any('order_invalid_size' in warning and 'order quantity is zero' in warning
                            for warning in captured.output), captured.output)
        reasons = self.bot.observation_metrics()['守卫及停报原因']
        self.assertTrue(any('order_invalid_size' in reason and 'order quantity is zero' in reason
                            for reason in reasons), reasons)
        self.assertIn('order_invalid_size', self.bot.observed_reason)
        self.assertIn('order quantity is zero', self.bot.observed_reason)
