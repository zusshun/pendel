"""M4 停止贯通回归；签名队列和交易所均为内存依赖。"""

import asyncio
import json
import unittest
from decimal import Decimal as D

import test_m3_wiring as wiring
from pendle.maker import Decision
from pendle.maker_orders import OrderStatus
from pendle.ondo_api import HttpResponse
from test_maker import MARKET
from test_ondo_api import settle


class M4StopTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = wiring.M3WiringTests.asyncSetUp
    asyncTearDown = wiring.M3WiringTests.asyncTearDown
    wall = wiring.M3WiringTests.wall
    drive = wiring.M3WiringTests.drive
    ready = wiring.M3WiringTests.ready
    publish = wiring.M3WiringTests.publish

    async def accepted_pair_with_unknown_sell(self):
        await self.ready()
        original = self.bot.api.transport

        async def network(request):
            path = request.path_query.partition('?')[0]
            if '/client:' in path and request.method == 'GET':
                self.requests.append(request)
                cid = path.partition('/client:')[2]
                row = self.remote.get(cid)
                return HttpResponse(200, {'success': True, 'result': dict(row)}) if row else (
                    HttpResponse(404, {'success': False, 'error': 'order_not_found'}))
            response = await original(request)
            if request.method == 'POST' and json.loads(request.body)['side'] == 'sell':
                raise TimeoutError('卖单已被远端受理，但响应超时')
            return response

        self.bot.api.transport = network
        for side, price in (('buy', '99.88'), ('sell', '100.12')):
            order = self.bot.manager.create_intent(MARKET, side, D(1), D(price))
            await self.drive(self.bot.manager.submit(order))
        self.assertEqual(order.status, OrderStatus.UNKNOWN)
        self.assertEqual(len(self.remote), 2)
        return order

    def assert_stopped(self):
        self.assertTrue(any(r.method == 'DELETE' for r in self.requests))
        self.assertEqual(self.remote, {})
        self.assertFalse(self.bot.state.active_orders())
        self.assertEqual(self.bot.guard.state, 'COOLDOWN')
        deletes = [i for i, r in enumerate(self.requests) if r.method == 'DELETE']
        self.assertTrue(any(r.method == 'GET' and 'status=open' in r.path_query
                            for r in self.requests[deletes[-1]+1:]))

    async def test_stale_mark_after_remote_sell_timeout_cancels_and_cools(self):
        unknown = await self.accepted_pair_with_unknown_sell()
        self.clock.advance(self.settings.mark_quiet_max_s + .01)
        self.publish(mark=False)
        await self.drive(self.bot.tick())
        self.assert_stopped()
        self.assertEqual(unknown.status, OrderStatus.ABSENT)
        queries = [i for i, r in enumerate(self.requests) if '/client:' in r.path_query]
        delete = next(i for i, r in enumerate(self.requests) if r.method == 'DELETE')
        self.assertGreater(min(queries), delete, '先按市场全撤，再继续 UNKNOWN 消歧')
        self.assertGreaterEqual(len(queries), 2, 'UNKNOWN 仍需双重查询证据')

    async def test_missing_mark_stops_sent_unknown_cancel_sent_and_intent(self):
        unknown = await self.accepted_pair_with_unknown_sell()
        self.bot.engine.mark = None
        for status in (OrderStatus.SENT, OrderStatus.UNKNOWN, OrderStatus.CANCEL_SENT, OrderStatus.INTENT):
            with self.subTest(status=status):
                unknown.status = status
                self.remote[unknown.client_order_id] = dict(clientOrderId=unknown.client_order_id,
                    orderId=unknown.client_order_id, market=MARKET, side='sell', size='1',
                    price='100.12', status='OPEN', filledSize='0', time_ms=int(self.wall()*1000))
                await self.drive(self.bot.tick())
                self.assert_stopped()

    async def test_slow_ordinary_alert_cannot_delay_market_cancel_or_guard_cooldown(self):
        await self.ready()
        await self.drive(self.bot.tick())
        self.assertEqual(len(self.remote), 2)
        entered, release = asyncio.Event(), asyncio.Event()
        self.bot.alerter._last_alert_attempt = None
        self.bot.last_gate_status = True
        original = self.bot.alerter._http
        cancellation_seen = []

        async def slow_http(method, url, **kwargs):
            if kwargs.get('json', {}).get('text', '').startswith('做市告警'):
                cancellation_seen.append(any(r.method == 'DELETE' for r in self.requests))
                entered.set()
                await release.wait()
            return await original(method, url, **kwargs)

        self.bot.alerter._http = slow_http
        self.bot.engine.mark = None
        ticking = asyncio.create_task(self.bot.tick())
        self.background.append(ticking)
        try:
            for _ in range(30):
                await settle()
                self.clock.advance(.01)
                if ticking.done():
                    break
            self.assertTrue(ticking.done(), '慢普通告警不得阻塞停止关键路径')
            await ticking
            self.assert_stopped()
            self.assertTrue(entered.is_set())
            self.assertEqual(cancellation_seen, [True], '此次停止通知 HTTP 必须晚于撤单请求')
        finally:
            release.set()
            await settle()

    async def test_any_failed_quote_decision_stops_even_with_unknown(self):
        unknown = await self.accepted_pair_with_unknown_sell()
        reasons = ('第三方价差超过阈值', '偏离独立慢锚', '中心价跳变', '独立慢锚过期',
                   '外部参考偏离', '第三方盘口覆盖不完整', '竞争量超过判据',
                   '取整后任一侧不合规', '标记价格辅助守卫', '我方扣除存在不确定性',
                   '公开盘口尚未覆盖我方剩余量')
        for reason in reasons:
            with self.subTest(reason=reason):
                unknown.status = OrderStatus.UNKNOWN
                self.remote[unknown.client_order_id] = dict(clientOrderId=unknown.client_order_id,
                    orderId=unknown.client_order_id, market=MARKET, side='sell', size='1',
                    price='100.12', status='OPEN', filledSize='0', time_ms=int(self.wall()*1000))
                self.bot.engine.evaluate = lambda *args, **kwargs: Decision(False, reason, D(100))
                await self.drive(self.bot.tick())
                self.assert_stopped()

    async def test_uncertain_own_deduction_itself_stops_both_sides(self):
        await self.accepted_pair_with_unknown_sell()
        await self.drive(self.bot.tick())
        self.assert_stopped()
        self.assertEqual(self.bot.guard.reason, '我方扣除存在不确定性')

    async def test_reconcile_inconsistency_stops_even_with_unknown(self):
        await self.accepted_pair_with_unknown_sell()
        original = self.bot.api.transport

        async def inconsistent_snapshot(request):
            if 'status=open' in request.path_query and not self.bot.state.stopping:
                self.requests.append(request)
                return HttpResponse(200, {'success': True, 'result': []})
            return await original(request)

        self.bot.api.transport = inconsistent_snapshot
        self.assertFalse(await self.drive(self.bot.reconcile_once()))
        reconciling = asyncio.create_task(self.bot.reconcile_loop())
        self.background.append(reconciling)
        for _ in range(40):
            await settle()
            self.clock.advance(.01)
            if self.bot.guard.state == 'COOLDOWN':
                break
        self.assert_stopped()

    async def test_submit_timeout_stops_the_other_side_immediately(self):
        await self.ready()
        original = self.bot.api.transport
        original_http = self.bot.alerter._http
        cancellation_before_alert = []
        self.bot.last_gate_status = True
        self.bot.alerter._last_alert_attempt = None

        async def http(method, url, **kwargs):
            if kwargs.get('json', {}).get('text', '').startswith('做市告警'):
                cancellation_before_alert.append(any(r.method == 'DELETE' for r in self.requests))
            return await original_http(method, url, **kwargs)

        async def network(request):
            response = await original(request)
            if request.method == 'POST' and json.loads(request.body)['side'] == 'sell':
                raise TimeoutError('远端已受理卖单')
            if '/client:' in request.path_query:
                return HttpResponse(404, {'success': False, 'error': 'order_not_found'})
            return response

        self.bot.api.transport = network
        self.bot.alerter._http = http
        await self.drive(self.bot.tick())
        self.assert_stopped()
        self.assertEqual(cancellation_before_alert, [True])

    async def test_stop_cancels_before_a_slow_unknown_query(self):
        await self.accepted_pair_with_unknown_sell()
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.bot.api.transport

        async def network(request):
            if '/client:' in request.path_query:
                entered.set()
                await release.wait()
            return await original(request)

        self.bot.api.transport = network
        stopping = asyncio.create_task(self.bot.guard.trigger('未知订单查询很慢'))
        self.background.append(stopping)
        try:
            await self.drive(entered.wait())
            self.assertEqual(self.remote, {}, 'UNKNOWN 消歧查询不能挡住全撤')
            self.assertTrue(any(r.method == 'DELETE' for r in self.requests))
        finally:
            release.set()
            await self.drive(stopping)

    async def test_late_open_ack_during_stop_is_still_confirmed_canceled(self):
        unknown = await self.accepted_pair_with_unknown_sell()
        accepted = dict(self.remote[unknown.client_order_id])
        original = self.bot.api.transport
        delivered = False

        async def late_ack(request):
            nonlocal delivered
            result = await original(request)
            if request.method == 'DELETE' and not delivered:
                delivered = True
                # 已受理回执迟到：撤单前后的状态版本变化必须让核零重试。
                self.bot.state.apply_order_event(accepted, source='rest')
            return result

        self.bot.api.transport = late_ack
        self.assertTrue(await self.drive(self.bot.guard.trigger('停止期间迟到 OPEN 回执')))
        self.assert_stopped()
        self.assertEqual(unknown.status, OrderStatus.CANCELED)
        self.assertGreaterEqual(sum(r.method == 'DELETE' for r in self.requests), 2)
