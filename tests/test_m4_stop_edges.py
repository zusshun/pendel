"""M4 停止边界贯通回归；签名队列与告警 HTTP 均使用内存传输。"""

import asyncio
import json
import unittest
from dataclasses import replace
from datetime import datetime
from decimal import Decimal as D
from zoneinfo import ZoneInfo

import test_m3_wiring as wiring
from pendle.maker_orders import OrderStatus
from pendle.ondo_api import HttpResponse
from test_maker import MARKET, book
from test_ondo_api import settle


class M4StopEdgesTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = wiring.M3WiringTests.asyncSetUp
    asyncTearDown = wiring.M3WiringTests.asyncTearDown
    wall = wiring.M3WiringTests.wall
    drive = wiring.M3WiringTests.drive
    ready = wiring.M3WiringTests.ready
    publish = wiring.M3WiringTests.publish

    def trace_transports(self):
        """记录实际传输顺序，并补齐 CID 查询的远端终态证据。"""
        self.events, self.closed = [], {}
        original_transport, original_http = self.bot.api.transport, self.bot.alerter._http

        async def transport(request):
            path = request.path_query.partition('?')[0]
            self.events.append((request.method, path))
            if request.method == 'DELETE':
                for cid, row in self.remote.items():
                    self.closed[cid] = {**row, 'status': 'CANCELED',
                                        'time_ms': int(self.wall()*1000)+1}
            if request.method == 'GET' and '/orders/client:' in path:
                self.requests.append(request)
                cid = path.split('/orders/client:', 1)[1]
                row = self.remote.get(cid, self.closed.get(cid))
                if row is None:
                    return HttpResponse(400, {'success': False, 'error_code': 'order_not_found'})
                return HttpResponse(200, {'success': True, 'result': dict(row)})
            return await original_transport(request)

        async def http(method, url, **kwargs):
            payload = kwargs.get('json') or {}
            self.events.append(('HTTP', payload.get('text', '')))
            return await original_http(method, url, **kwargs)

        self.bot.api.transport, self.bot.alerter._http = transport, http

    async def live_pair(self):
        await self.ready()
        await self.drive(self.bot.tick())
        await settle()
        self.assertEqual(len(self.remote), 2)
        self.trace_transports()
        self.bot.alerter._last_alert_attempt = None

    def set_wall(self, value):
        self.epoch.epoch = datetime.fromisoformat(value).replace(
            tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()-self.clock.monotonic()

    def assert_cancelled_market(self):
        deletes = [request for request in self.requests if request.method == 'DELETE'
                   and request.path_query.partition('?')[2] == 'market=IBM-USD.P']
        self.assertTrue(deletes, '停止分支必须按市场全部撤单')
        first_cancel = self.requests.index(deletes[0])
        self.assertTrue(any(request.method == 'GET'
                            and request.path_query.partition('?')[0].endswith('/orders')
                            and 'status=open' in request.path_query
                            for request in self.requests[first_cancel+1:]),
                        '按市场全撤后必须列出挂单核零')
        self.assertFalse(self.remote, '必须核实远端挂单归零')

    def assert_cancel_before_ordinary_http(self):
        deletes = [i for i, event in enumerate(self.events) if event[0] == 'DELETE']
        ordinary = [i for i, event in enumerate(self.events)
                    if event[0] == 'HTTP' and event[1].startswith('做市告警：')]
        self.assertTrue(deletes, '必须实际发出撤单请求')
        self.assertTrue(ordinary, '本场景必须实际运行普通告警 HTTP')
        self.assertLess(deletes[0], ordinary[0], '首个撤单必须先于本轮普通告警 HTTP')

    async def test_first_bulk_cancel_does_not_wait_for_inflight_write_response(self):
        await self.ready()
        self.trace_transports()
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.bot.api.transport

        async def slow_response(request):
            response = await original(request)
            if request.method == 'POST':
                entered.set()
                while not release.is_set():
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        continue
            return response

        self.bot.api.transport = slow_response
        order = self.bot.manager.create_intent(MARKET, 'buy', '.1', '99.88')
        sending = asyncio.create_task(self.bot.manager.submit(order))
        self.background.append(sending)
        stopping = None
        try:
            await entered.wait()
            stopping = asyncio.create_task(self.bot.manager.stop('在途响应未回先全撤'))
            self.background.append(stopping)
            for _ in range(20):
                await settle()
                self.clock.advance(.002)
                if any(event[0] == 'DELETE' for event in self.events):
                    break
            self.assertTrue(any(event[0] == 'DELETE' for event in self.events),
                            '响应仍未释放时就必须先发送 DELETE')
            self.assertFalse(release.is_set())
            self.assertFalse(self.remote)
        finally:
            release.set()
            await self.drive(asyncio.gather(sending, *([stopping] if stopping else []),
                                            return_exceptions=True))

    async def test_reduce_only_fallback_bulk_cancel_precedes_ordinary_http(self):
        await self.ready()
        self.trace_transports()
        self.position = self.bot.state.positions[MARKET] = D(3)
        self.bot.settings = replace(self.settings, reduce_only_gtc_verified=True)
        self.bot.engine.settings = self.bot.settings
        self.bot.last_gate_status = True
        original = self.bot.api.transport

        async def reject_reduce_only(request):
            if request.method == 'POST' and json.loads(request.body).get('reduceOnly'):
                self.events.append(('POST', request.path_query))
                self.requests.append(request)
                return HttpResponse(400, {'success': False,
                                         'error_code': 'reduce_only_invalid_tif'})
            return await original(request)

        self.bot.api.transport = reject_reduce_only
        await self.drive(self.bot.tick())
        await settle()
        self.assertFalse(self.bot.settings.reduce_only_gtc_verified)
        self.assert_cancelled_market()
        self.assert_cancel_before_ordinary_http()

    async def test_legacy_order_cancel_precedes_ordinary_http(self):
        await self.ready()
        self.trace_transports()
        row = dict(market=MARKET, clientOrderId='late-legacy', orderId='late-legacy',
                   side='sell', size='1', price='100.12', filledSize='0',
                   status='OPEN', time_ms=int(self.wall()*1000))
        self.remote[row['clientOrderId']] = dict(row)
        await self.drive(self.bot.manager.handle_order_event(row))
        await settle()
        self.assertFalse(self.remote)
        self.assert_cancel_before_ordinary_http()

    async def test_late_ack_repeats_bulk_cancel_before_ordinary_http(self):
        await self.ready()
        self.trace_transports()
        # 本地等待先超时，底层传输仍可正常返回迟到 ACK。
        self.bot.manager.request_timeout_s = .1
        self.bot.api.timeout_s = 30
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.bot.api.transport

        async def slow_response(request):
            response = await original(request)
            if request.method == 'POST':
                entered.set()
                while not release.is_set():
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        continue
            return response

        async def record_failed_stop(reason):
            return None

        self.bot.api.transport = slow_response
        # 只隔离停止失败后的进程退出策略，检验订单管理器自身的迟到受理补全撤。
        self.bot.manager.stop_failed = record_failed_stop
        order = self.bot.manager.create_intent(MARKET, 'buy', '.1', '99.88')
        sending = asyncio.create_task(self.bot.manager.submit(order))
        self.background.append(sending)
        try:
            await entered.wait()
            self.assertFalse(await self.drive(self.bot.manager.stop('响应迟到，首次核零不能确认')))
            await settle()
            self.events.clear()
            self.bot.alerter._last_alert_attempt = None
            release.set()
            await self.drive(sending)
            for _ in range(100):
                await settle()
                self.clock.advance(.01)
                if (any(event[0] == 'DELETE' for event in self.events)
                        and not any(not task.done() for task in self.bot.manager.background)):
                    break
            self.assert_cancel_before_ordinary_http()
            self.assertFalse(self.remote)
        finally:
            release.set()
            await self.drive(asyncio.gather(sending, return_exceptions=True))

    async def test_outside_window_cancels_unknown_orders(self):
        await self.live_pair()
        self.bot.state.active_orders()[0].status = OrderStatus.UNKNOWN
        self.set_wall('2026-11-09T06:00:00')
        await self.drive(self.bot.tick())
        self.assert_cancelled_market()

    async def test_last_hour_flat_cancels_while_market_is_syncing(self):
        await self.live_pair()
        self.bot.state.modes[MARKET] = 'SYNCING'
        self.set_wall('2026-11-09T04:30:00')
        await self.drive(self.bot.tick())
        self.assert_cancelled_market()

    async def test_last_hour_unavailable_reduction_cancels_while_market_is_syncing(self):
        await self.live_pair()
        self.bot.state.modes[MARKET] = 'SYNCING'
        self.position = self.bot.state.positions[MARKET] = D(1)
        self.bot.passive_reduce_unavailable = True
        self.set_wall('2026-11-09T04:30:00')
        await self.drive(self.bot.tick())
        self.assert_cancelled_market()

    async def test_stale_book_cancels_unknown_orders(self):
        await self.live_pair()
        self.bot.state.active_orders()[0].status = OrderStatus.UNKNOWN
        self.bot.book_seen = self.clock.monotonic()-self.settings.max_book_age_s-1
        await self.drive(self.bot.tick())
        self.assert_cancelled_market()
        self.assertEqual(self.bot.guard.state, 'COOLDOWN')

    async def test_window_rotation_cancels_unknown_orders_before_waiting(self):
        await self.live_pair()
        self.bot.state.active_orders()[0].status = OrderStatus.UNKNOWN
        self.set_wall('2026-11-14T10:00:00')
        self.bot.guard.state = 'COOLDOWN'
        self.bot.guard.triggered_at = self.clock.monotonic()
        await self.drive(self.bot.tick())
        self.assert_cancelled_market()

    async def test_hard_cap_cancels_before_reduction_while_market_is_syncing(self):
        await self.live_pair()
        self.bot.state.modes[MARKET] = 'SYNCING'
        self.position = self.bot.state.positions[MARKET] = D(6)
        await self.drive(self.bot.tick())
        self.assert_cancelled_market()

    async def test_single_side_residue_cancels_while_market_is_syncing(self):
        await self.live_pair()
        order = self.bot.state.active_orders()[0]
        self.remote.pop(order.client_order_id)
        order.status = OrderStatus.CANCELED
        self.bot.state.modes[MARKET] = 'SYNCING'
        self.publish()
        await self.drive(self.bot.tick())
        self.assert_cancelled_market()

    async def test_single_side_residue_cancels_without_deadman_ack(self):
        await self.live_pair()
        order = self.bot.state.active_orders()[0]
        self.remote.pop(order.client_order_id)
        order.status = OrderStatus.CANCELED
        self.bot.state.deadman_acked = False
        self.publish()
        await self.drive(self.bot.tick())
        self.assert_cancelled_market()
        self.publish()
        # 撤单不依赖 ACK；后续节拍等待 ACK，不应过早恢复并抛异常。
        await self.drive(self.bot.tick())
        self.assertTrue(self.bot.state.stopping)

    async def test_cooldown_waits_for_deadman_ack_before_resuming(self):
        await self.live_pair()
        self.bot.state.deadman_acked = False
        self.bot.guard.cooldown_s = self.bot.guard.stable_s = 0
        self.bot.engine.mark = None
        await self.drive(self.bot.tick())
        self.assert_cancelled_market()
        self.publish()
        await self.drive(self.bot.tick())
        self.assertEqual(self.bot.guard.state, 'COOLDOWN')
        self.assertTrue(self.bot.state.stopping)

    async def test_single_side_residue_cancels_during_rejection_backoff(self):
        await self.live_pair()
        order = self.bot.state.active_orders()[0]
        self.remote.pop(order.client_order_id)
        order.status = OrderStatus.CANCELED
        self.bot.backoff_until = self.clock.monotonic()+8
        self.publish()
        await self.drive(self.bot.tick())
        self.assert_cancelled_market()

    async def unsafe_soft_pair(self):
        await self.live_pair()
        self.position = self.bot.state.positions[MARKET] = D('3.1')
        self.publish()

    async def test_oversized_pair_cancels_while_market_is_syncing(self):
        await self.unsafe_soft_pair()
        self.bot.state.modes[MARKET] = 'SYNCING'
        await self.drive(self.bot.tick())
        self.assert_cancelled_market()

    async def test_oversized_pair_cancels_without_deadman_ack(self):
        await self.unsafe_soft_pair()
        self.bot.state.deadman_acked = False
        await self.drive(self.bot.tick())
        self.assert_cancelled_market()

    async def test_oversized_pair_cancels_during_rejection_backoff(self):
        await self.unsafe_soft_pair()
        self.bot.backoff_until = self.clock.monotonic()+8
        await self.drive(self.bot.tick())
        self.assert_cancelled_market()

    async def test_oversized_pair_cancels_before_minimum_requote_interval(self):
        await self.unsafe_soft_pair()
        self.bot.last_requote = self.clock.monotonic()
        await self.drive(self.bot.tick())
        self.assert_cancelled_market()

    async def unsafe_price_pair(self):
        await self.live_pair()
        bids, asks = [('100.05', '10'), ('99', '10')], [('100.25', '10'), ('101', '10')]
        for row in self.remote.values():
            (bids if row['side'] == 'buy' else asks).append((row['price'], row['size']))
        self.bot.book = book(bids=bids, asks=asks, at=int(self.wall()*1000))
        self.bot.book_seen = self.clock.monotonic()
        self.bot.engine.mark = (D('100.15'), self.clock.monotonic())

    async def test_old_price_outside_latest_band_cancels_before_requote_interval(self):
        await self.unsafe_price_pair()
        self.bot.last_requote = self.clock.monotonic()
        await self.drive(self.bot.tick())
        self.assert_cancelled_market()

    async def test_old_price_outside_latest_band_cancels_while_market_is_syncing(self):
        await self.unsafe_price_pair()
        self.bot.state.modes[MARKET] = 'SYNCING'
        await self.drive(self.bot.tick())
        self.assert_cancelled_market()
