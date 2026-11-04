"""M23：订单意图标识字符约束与撤单、查询一致性；只用内存传输。"""

import json
import unittest

from pendle.maker_orders import AccountState, OrderManager, OrderStateError, OrderStatus
from pendle.ondo_api import Credentials, HttpResponse, OndoAPI


class M23ClientOrderIdTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.requests, self.remote = [], {}

        async def transport(request):
            self.requests.append(request)
            if request.method == 'POST':
                body = json.loads(request.body)
                result = {**body, 'orderId': 'exchange-' + body['clientOrderId'],
                          'status': 'OPEN', 'filledSize': '0', 'time_ms': 10}
                self.remote[body['clientOrderId']] = result
            else:
                cid = request.path_query.partition('/client:')[2]
                result = self.remote[cid]
                if request.method == 'DELETE':
                    result.update(status='CANCELED', time_ms=11)
            return HttpResponse(200, {'success': True, 'result': result})

        self.api = OndoAPI(Credentials('测试-key', '测试-secret'),
                           transport=transport, rate=1_000_000)

    def manager(self, market='IBM-USD.P', *, run_id='maker'):
        state = AccountState([market])
        state.deadman_ack()
        state.modes[market] = 'RUNNING'
        return OrderManager(state, self.api, risk_check=lambda *args: None, run_id=run_id)

    async def test_ibm_market_generates_valid_client_order_id(self):
        manager = self.manager()
        order = manager.create_intent('IBM-USD.P', 'buy', '1', '100')
        self.assertRegex(order.client_order_id, r'^[A-Za-z0-9_-]{1,64}$')
        self.assertIn('-IBM-USD_P-buy-1', order.client_order_id)
        self.assertEqual(order.market, 'IBM-USD.P')

    async def test_special_market_characters_are_replaced_individually(self):
        for market, sanitized in (
                ('A.B/C: D@E', 'A_B_C__D_E'),
                ('中 文-USD.P', '___-USD_P'),
                ('A_\t\n+#%-Z', 'A______-Z')):
            with self.subTest(market=market):
                manager = self.manager(market)
                order = manager.create_intent(market, 'sell', '1', '100')
                self.assertEqual(order.client_order_id,
                                 f'{manager.run_id}-{sanitized}-sell-1')
                self.assertRegex(order.client_order_id, r'^[A-Za-z0-9_-]{1,64}$')
                self.assertEqual(order.market, market)

    async def test_client_order_id_accepts_exactly_64_characters(self):
        manager = self.manager()
        market = 'A' * (64 - len(manager.run_id) - len('--buy-1'))
        manager = self.manager(market)
        order = manager.create_intent(market, 'buy', '1', '100')
        self.assertEqual(len(order.client_order_id), 64)
        self.assertRegex(order.client_order_id, r'^[A-Za-z0-9_-]{1,64}$')

    async def test_overlong_client_order_id_raises_before_request(self):
        manager = self.manager()
        market = 'A' * (65 - len(manager.run_id) - len('--buy-1'))
        manager = self.manager(market)
        with self.assertRaises(OrderStateError):
            manager.create_intent(market, 'buy', '1', '100')
        self.assertEqual(manager.state.orders, {})
        self.assertEqual(self.requests, [])

    async def test_invalid_final_client_order_id_raises_before_request(self):
        for run_id in ('bad.run', '中文', 'line\n'):
            with self.subTest(run_id=run_id):
                manager = self.manager(run_id=run_id)
                with self.assertRaises(OrderStateError):
                    manager.create_intent('IBM-USD.P', 'buy', '1', '100')
                self.assertEqual(manager.state.orders, {})
                self.assertEqual(self.requests, [])

    async def test_submit_cancel_and_query_keep_same_id_and_original_market(self):
        manager = self.manager()
        order = manager.create_intent('IBM-USD.P', 'buy', '1', '100')
        cid = order.client_order_id
        await manager.submit(order)
        self.assertEqual(order.status, OrderStatus.OPEN)
        await manager.cancel(order)
        self.assertTrue(await manager.confirm_cancel(order))
        body = json.loads(self.requests[0].body)
        self.assertRegex(body['clientOrderId'], r'^[A-Za-z0-9_-]{1,64}$')
        self.assertEqual(body['clientOrderId'], cid)
        self.assertEqual(body['market'], 'IBM-USD.P')
        self.assertEqual([(request.method, request.path_query) for request in self.requests],
                         [('POST', '/v1/perps/orders'),
                          ('DELETE', '/v1/perps/orders/client:' + cid),
                          ('GET', '/v1/perps/orders/client:' + cid)])
        self.assertIs(manager.state.orders[cid], order)
        self.assertEqual(order.market, 'IBM-USD.P')
        self.assertEqual(order.status, OrderStatus.CANCELED)
