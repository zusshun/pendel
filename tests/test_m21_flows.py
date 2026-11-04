"""M21：真实成功空正文贯通停止、人工复位与下单结果消歧。"""

import unittest
from decimal import Decimal as D
from unittest.mock import patch

import test_m3_wiring as wiring
from pendle import maker_transport
from pendle.maker_config import MakerSettings
from pendle.maker_orders import OrderStatus
from pendle.ondo_api import Credentials, HttpResponse, OndoAPI
from test_maker import MARKET
from test_maker_transport import Files as EmergencyFiles


class M21StopAndUnknownFlowTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = wiring.M3WiringTests.asyncSetUp
    asyncTearDown = wiring.M3WiringTests.asyncTearDown
    wall = wiring.M3WiringTests.wall
    drive = wiring.M3WiringTests.drive
    ready = wiring.M3WiringTests.ready
    publish = wiring.M3WiringTests.publish

    async def test_startup_stop_with_zero_orders_accepts_success_without_result(self):
        original = self.bot.api.transport

        async def transport(request):
            if request.method == 'DELETE':
                self.requests.append(request)
                return HttpResponse(200, {'success': True})
            return await original(request)

        self.bot.api.transport = transport
        with patch('socket.socket.connect', side_effect=AssertionError('测试禁止联网')):
            await self.drive(self.bot.startup())
            self.assertFalse(self.remote)
            self.assertTrue(await self.drive(self.bot.stop_orders('启动时零挂单停止')))
        methods = [request.method for request in self.requests]
        self.assertEqual(methods, ['DELETE', 'GET'])
        self.assertIn('status=open', self.requests[-1].path_query)
        self.assertFalse(self.bot._stop_failed)
        self.assertFalse(self.files.exists(self.settings.halt_path))
        self.assertEqual(self.bot.exit_code, 0)

    async def test_missing_order_result_is_unknown_and_resolves_by_client_id_without_resubmit(self):
        await self.ready()
        original = self.bot.api.transport

        async def transport(request):
            if request.method == 'POST':
                await original(request)
                return HttpResponse(200, {'success': True})
            if request.method == 'GET' and '/client:' in request.path_query:
                self.requests.append(request)
                cid = request.path_query.partition('/client:')[2]
                return HttpResponse(200, {'success': True, 'result': self.remote[cid]})
            return await original(request)

        self.bot.api.transport = transport
        order = self.bot.manager.create_intent(MARKET, 'buy', D(1), D('99.88'))
        with patch('socket.socket.connect', side_effect=AssertionError('测试禁止联网')):
            await self.drive(self.bot.manager.submit(order))
            self.assertEqual(order.status, OrderStatus.UNKNOWN)
            self.assertEqual(order.last_error_status, 200)
            self.assertEqual(self.bot.state.market_mode(MARKET), 'SYNCING')
            self.assertTrue(await self.drive(self.bot.manager.resolve_unknown(order)))
        self.assertEqual(order.status, OrderStatus.OPEN)
        self.assertEqual(sum(request.method == 'POST' for request in self.requests), 1)
        self.assertEqual(sum('/client:' in request.path_query for request in self.requests), 1)

    async def test_missing_order_and_query_results_confirm_absence_after_cancel_without_resubmit(self):
        await self.ready()
        original = self.bot.api.transport
        self.requests.clear()

        async def transport(request):
            if request.method == 'POST' or '/client:' in request.path_query:
                self.requests.append(request)
                return HttpResponse(200, {'success': True})
            if request.method == 'DELETE':
                self.requests.append(request)
                return HttpResponse(200, {'success': True})
            return await original(request)

        self.bot.api.transport = transport
        order = self.bot.manager.create_intent(MARKET, 'buy', D(1), D('99.88'))
        with patch('socket.socket.connect', side_effect=AssertionError('测试禁止联网')):
            await self.drive(self.bot.manager.submit(order))
            self.assertEqual(order.status, OrderStatus.UNKNOWN)
            self.assertTrue(await self.drive(self.bot.manager.resolve_unknown(order)))
        self.assertEqual(order.status, OrderStatus.ABSENT)
        self.assertTrue(order.confirmed_absent)
        self.assertEqual(sum(request.method == 'POST' for request in self.requests), 1)
        self.assertGreaterEqual(sum('/client:' in request.path_query for request in self.requests), 2)
        deletes = [i for i, request in enumerate(self.requests) if request.method == 'DELETE']
        self.assertEqual(len(deletes), 1)
        self.assertTrue(any(request.method == 'GET' and 'status=open' in request.path_query
                            for request in self.requests[deletes[0] + 1:]))
        self.assertTrue(any('/client:' in request.path_query
                            for request in self.requests[deletes[0] + 1:]))


class M21ResetHaltFlowTests(unittest.TestCase):
    def test_reset_halt_zero_orders_removes_lock_after_success_without_result(self):
        files, requests = EmergencyFiles(), []

        async def transport(request):
            requests.append(request)
            if request.method == 'DELETE':
                files.events.append('cancel')
                return HttpResponse(200, {'success': True})
            files.events.append('verify')
            return HttpResponse(200, {'success': True, 'result': []})

        api = OndoAPI(Credentials('测试-key', '测试-secret'), transport=transport,
                      rate=1_000_000)
        with patch('socket.socket.connect', side_effect=AssertionError('测试禁止联网')):
            result = maker_transport.reset_halt(MakerSettings(mode='sandbox'),
                                                api=api, files=files)
        self.assertEqual(result, 0)
        self.assertFalse(files.halted)
        self.assertEqual([request.method for request in requests], ['DELETE', 'GET'])
        self.assertEqual(files.events, ['instance-acquire', 'cancel', 'verify',
                                       'unlink-halt', 'parent-fsync', 'instance-release'])
