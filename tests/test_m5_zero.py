"""M5 核零只受本市场订单及持仓更新影响；全部传输均为内存桩。"""

import unittest

import test_m3_wiring as wiring
import test_maker_orders as orders
from pendle.maker_orders import AccountState, OrderManager, OrderStatus
from pendle.ondo_api import HttpResponse
from test_maker import MARKET


OTHER = 'OTHER-USD.P'


class M5ZeroWiringTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = wiring.M3WiringTests.asyncSetUp
    asyncTearDown = wiring.M3WiringTests.asyncTearDown
    wall = wiring.M3WiringTests.wall
    drive = wiring.M3WiringTests.drive
    ready = wiring.M3WiringTests.ready
    publish = wiring.M3WiringTests.publish

    async def live_pair(self):
        await self.ready()
        await self.drive(self.bot.tick())
        self.assertEqual(len(self.remote), 2)
        self.requests.clear()

    def assert_confirmed_stop(self):
        self.assertEqual(self.remote, {})
        self.assertFalse(self.bot.state.active_orders(MARKET))
        self.assertEqual(self.bot.guard.state, 'COOLDOWN')
        self.assertTrue(self.bot.gate.is_open)
        self.assertNotEqual(self.bot.exit_code, 76)
        self.assertNotIn(str(self.settings.halt_path), self.files.rows)

    async def test_balance_push_during_zero_snapshot_does_not_stop_failed(self):
        await self.live_pair()
        transport = self.bot.api.transport
        pushed = []

        async def balance_push(request):
            response = await transport(request)
            if request.method == 'GET' and 'status=open' in request.path_query:
                pushed.append(request)
                self.bot.state.apply_balance_snapshot(dict(self.bot.state.balances,
                    equity='10001', totalFundingPayments='0.01',
                    time_ms=int(self.wall()*1000)+len(pushed)), source='ws')
            return response

        self.bot.api.transport = balance_push
        self.assertTrue(await self.drive(self.bot.guard.trigger('余额不应干扰核零')))
        self.assert_confirmed_stop()
        self.assertEqual(len(pushed), 1)
        self.assertEqual(sum(r.method == 'DELETE' for r in self.requests), 1)

    async def test_same_market_new_order_during_zero_snapshot_repeats_cancel(self):
        await self.live_pair()
        transport = self.bot.api.transport
        delivered = False
        late = dict(clientOrderId='late-m5', orderId='late-m5', market=MARKET,
                    side='buy', size='1', price='99.88', filledSize='0',
                    status='OPEN', time_ms=int(self.wall()*1000)+1)

        async def late_order(request):
            nonlocal delivered
            response = await transport(request)
            if request.method == 'GET' and 'status=open' in request.path_query and not delivered:
                delivered = True
                self.remote[late['clientOrderId']] = dict(late)
                self.bot.state.apply_order_event(late)
            return response

        self.bot.api.transport = late_order
        self.assertTrue(await self.drive(self.bot.guard.trigger('核零期间迟到挂单')))
        self.assert_confirmed_stop()
        self.assertTrue(delivered)
        self.assertEqual(sum(r.method == 'DELETE' for r in self.requests), 2)
        self.assertEqual(self.bot.state.orders[late['clientOrderId']].status,
                         OrderStatus.CANCELED)

    async def test_same_market_position_during_zero_snapshot_repeats_cancel(self):
        await self.live_pair()
        transport = self.bot.api.transport
        delivered = False

        async def position_push(request):
            nonlocal delivered
            response = await transport(request)
            if request.method == 'GET' and 'status=open' in request.path_query and not delivered:
                delivered = True
                self.bot.state.apply_position_snapshot(MARKET, '0',
                    int(self.wall()*1000)+1, source='ws')
            return response

        self.bot.api.transport = position_push
        self.assertTrue(await self.drive(self.bot.guard.trigger('核零期间持仓推送')))
        self.assert_confirmed_stop()
        self.assertEqual(sum(r.method == 'DELETE' for r in self.requests), 2)

    async def test_private_position_push_during_zero_snapshot_repeats_cancel(self):
        await self.live_pair()
        transport = self.bot.api.transport
        delivered = False

        async def private_position_push(request):
            nonlocal delivered
            response = await transport(request)
            if request.method == 'GET' and 'status=open' in request.path_query and not delivered:
                delivered = True
                await self.bot.on_private({'channel': 'positionsPerps',
                    'data': [{'market': MARKET, 'quantity': '0'}]})
            return response

        self.bot.api.transport = private_position_push
        self.assertTrue(await self.drive(self.bot.guard.trigger('核零期间真实私有持仓推送')))
        self.assert_confirmed_stop()
        self.assertEqual(sum(r.method == 'DELETE' for r in self.requests), 2)

    async def test_private_position_push_during_unknown_proof_repeats_cancel(self):
        await self.live_pair()
        unknown = self.bot.state.active_orders(MARKET)[0]
        unknown.status = OrderStatus.UNKNOWN
        unknown.unknown_since = self.clock.monotonic()
        transport = self.bot.api.transport
        queries = 0

        async def private_position_push(request):
            nonlocal queries
            if request.method == 'GET' and '/orders/client:' in request.path_query:
                self.requests.append(request)
                queries += 1
                if queries == 1:
                    await self.bot.on_private({'channel': 'positionsPerps',
                        'data': [{'market': MARKET, 'quantity': '0'}]})
                return HttpResponse(404, {'success': False, 'error': 'order_not_found'})
            return await transport(request)

        self.bot.api.transport = private_position_push
        self.assertTrue(await self.drive(self.bot.guard.trigger('双查期间真实私有持仓推送')))
        self.assert_confirmed_stop()
        self.assertEqual(sum(r.method == 'DELETE' for r in self.requests), 2)
        self.assertEqual(queries, 3)
        self.assertEqual(unknown.status, OrderStatus.ABSENT)


class M5ZeroUnknownTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock, self.api = orders.Clock(), orders.API()
        self.state = AccountState([MARKET, OTHER])
        self.failures = []

        async def stop_failed(reason):
            self.failures.append(reason)

        self.manager = OrderManager(self.state, self.api,
            risk_check=lambda *args: None, monotonic=self.clock,
            sleep=self.clock.sleep, stop_failed=stop_failed)
        self.state.deadman_ack()
        self.assertTrue(await self.manager.reconcile(MARKET))
        self.api.calls.clear()

    def unknown(self):
        order = self.manager.create_intent(MARKET, 'buy', '2', '100')
        order.status = OrderStatus.UNKNOWN
        order.unknown_since = self.clock()
        return order

    def balance(self):
        self.state.apply_balance_snapshot(dict(equity='1000', available='900',
            totalFundingPayments='0.01', time_ms=self.state.balance_ms+1), source='ws')

    async def test_balance_push_during_unknown_proofs_keeps_both_queries(self):
        order = self.unknown()

        async def query(cid):
            self.balance()
            return None

        self.api.query = query
        self.assertTrue(await self.manager.stop('余额不影响 UNKNOWN 核零'))
        self.assertEqual(order.status, OrderStatus.ABSENT)
        self.assertEqual([call[0] for call in self.api.calls if call[1] != OTHER],
                         ['cancel_all', 'open', 'query', 'query'])
        self.assertEqual(self.failures, [])
        self.assertFalse(self.state.active_orders(MARKET))

    async def test_other_market_updates_do_not_restart_market_zero_snapshot(self):
        original = self.api.get_open_orders
        updates = 0

        async def snapshot(market, *, stop=False):
            nonlocal updates
            result = await original(market, stop=stop)
            if market == MARKET:
                updates += 1
                self.state.apply_position_snapshot(OTHER, '0', 100+updates, source='ws')
                self.state.apply_order_event(dict(orders.row('other', at=100+updates,
                    status='CANCELED'), market=OTHER))
            return result

        self.api.get_open_orders = snapshot
        self.assertTrue(await self.manager.stop('其他市场推送不影响本市场核零'))
        self.assertEqual(updates, 1)
        self.assertEqual(self.failures, [])

    async def test_balance_conflict_sync_generation_does_not_restart_zero_snapshot(self):
        original = self.api.get_open_orders
        snapshots = 0

        async def snapshot(market, *, stop=False):
            nonlocal snapshots
            result = await original(market, stop=stop)
            if market == MARKET:
                snapshots += 1
                at = self.state.balance_ms+1
                self.state.apply_balance_snapshot(dict(equity='1001', available='900',
                    time_ms=at), source='ws')
                self.state.apply_balance_snapshot(dict(equity='1000', available='900',
                    time_ms=at), source='rest')
            return result

        self.api.get_open_orders = snapshot
        self.assertTrue(await self.manager.stop('余额矛盾的同步代际不属于挂单核零版本'))
        self.assertEqual(snapshots, 1)
        self.assertEqual(self.failures, [])

    async def test_other_market_updates_do_not_invalidate_unknown_proofs(self):
        order = self.unknown()
        queries = 0

        async def query(cid):
            nonlocal queries
            queries += 1
            self.state.apply_position_snapshot(OTHER, '0', 100+queries, source='ws')
            return None

        self.api.query = query
        self.assertTrue(await self.manager.stop('其他市场持仓不影响 UNKNOWN 双查'))
        self.assertEqual(order.status, OrderStatus.ABSENT)
        self.assertEqual(queries, 2)
        self.assertEqual(self.failures, [])

    async def test_same_market_new_order_during_unknown_proof_restarts_zero(self):
        order = self.unknown()
        queries = 0

        async def query(cid):
            nonlocal queries
            queries += 1
            if queries == 1:
                self.state.apply_order_event(orders.row('late-other-side', side='sell', at=100))
            return None

        self.api.query = query
        self.assertTrue(await self.manager.stop('UNKNOWN 双查期间迟到同市场订单'))
        self.assertEqual(sum(call[0] == 'cancel_all' and call[1] == MARKET
                             for call in self.api.calls), 2)
        self.assertEqual(queries, 3)
        self.assertEqual(order.status, OrderStatus.ABSENT)
        self.assertFalse(self.state.active_orders(MARKET))
        self.assertEqual(self.failures, [])
