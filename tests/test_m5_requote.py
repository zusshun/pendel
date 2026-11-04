"""M5 微动保留与重报节流；所有 DELETE 均经过真实签名队列的内存传输。"""

import unittest
from dataclasses import replace
from decimal import Decimal as D

import test_m3_wiring as wiring
from pendle.maker import MarketRules
from pendle.maker_orders import OrdersBlocked
from pendle.maker_risk import RiskRejected
from pendle.ondo_api import HttpResponse
from test_maker import MARKET, book


class M5RequoteTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = wiring.M3WiringTests.asyncTearDown
    wall = wiring.M3WiringTests.wall
    drive = wiring.M3WiringTests.drive
    ready = wiring.M3WiringTests.ready

    async def asyncSetUp(self):
        await wiring.M3WiringTests.asyncSetUp(self)
        self.center = D(100)
        self.closed = {}
        original = self.bot.api.transport

        async def transport(request):
            path = request.path_query.partition('?')[0]
            if request.method == 'DELETE':
                self.requests.append(request)
                cids = ([path.split('/client:', 1)[1]] if '/client:' in path
                        else tuple(self.remote))
                for cid in cids:
                    row = self.remote.pop(cid, None)
                    if row is not None:
                        self.closed[cid] = {**row, 'status': 'CANCELED',
                                            'time_ms': int(self.wall()*1000)+1}
                return HttpResponse(200, {'success': True, 'result': {}})
            if request.method == 'GET' and '/orders/client:' in path:
                self.requests.append(request)
                cid = path.split('/orders/client:', 1)[1]
                row = self.remote.get(cid, self.closed.get(cid))
                if row is None:
                    return HttpResponse(400, {'success': False, 'error_code': 'order_not_found'})
                return HttpResponse(200, {'success': True, 'result': dict(row)})
            return await original(request)

        self.bot.api.transport = transport

    def publish(self, *, mark=True, center=None):
        if center is not None:
            self.center = D(center)
        bids = [(self.center-D('.1'), D(10)), (self.center-1, D(10))]
        asks = [(self.center+D('.1'), D(10)), (self.center+1, D(10))]
        for row in self.remote.values():
            remaining = D(row['size'])-D(row.get('filledSize', '0'))
            (bids if row['side'] == 'buy' else asks).append((row['price'], remaining))
        self.bot.book = book(bids=bids, asks=asks, at=int(self.wall()*1000))
        self.bot.book_seen = self.clock.monotonic()
        if mark:
            self.bot.engine.mark = (self.center, self.clock.monotonic())
            self.bot.engine.in_price_discovery = False
            self.bot.engine.price_discovery_seen = self.clock.monotonic()

    async def live_pair(self, *, tick='.01'):
        self.bot.rules = MarketRules(D(tick), D('.001'))
        await self.ready()
        await self.drive(self.bot.tick())
        self.assertEqual(len(self.remote), 2)
        self.requests.clear()

    def deletes(self):
        return [request for request in self.requests if request.method == 'DELETE']

    def set_outer_pair(self, *, buy=None, sell=None):
        """模拟此前在外沿成交前已挂好的双边，金额留足取整余量。"""
        prices = {'buy': D(buy) if buy is not None else (self.center-D('.18')).quantize(self.bot.rules.tick),
                  'sell': D(sell) if sell is not None else (self.center+D('.20')).quantize(self.bot.rules.tick)}
        for order in self.bot.state.active_orders(MARKET):
            order.price, order.quantity = prices[order.side], D('3.99')
            self.remote[order.client_order_id].update(price=str(order.price),
                                                       size=str(order.quantity))
        self.publish()
        self.assertEqual(self.retention_failures(), [])

    def retention_failures(self):
        failures = []
        for order in self.bot.state.active_orders(MARKET):
            try:
                self.bot.validate_resting_or_pending(order, self.bot.state,
                                                    now=self.clock.monotonic())
            except (OrdersBlocked, RiskRejected) as exc:
                failures.append(str(exc))
        return failures

    async def test_one_basis_point_move_keeps_compliant_pair_without_delete_at_one_request_per_second(self):
        await self.live_pair()
        self.set_outer_pair(buy='99.87', sell='100.14')
        before = {cid: dict(row) for cid, row in self.remote.items()}
        self.bot.api.rate, self.bot.api._tokens = 1, 1
        self.bot.last_requote = self.clock.monotonic()
        self.clock.advance(.01)
        self.publish(center='100.01')
        self.assertEqual(self.retention_failures(), [])
        await self.drive(self.bot.tick())
        self.assertEqual(self.deletes(), [], '1bp 微动未达到重报阈值，合规旧单应保持不动')
        self.assertEqual(self.remote, before)
        self.assertTrue(self.bot.qualified)

    async def test_compliant_old_pair_is_throttled_when_new_theoretical_mark_guard_fails(self):
        await self.live_pair()
        before = {cid: dict(row) for cid, row in self.remote.items()}
        self.bot.settings = replace(self.bot.settings, requote_bps=D(1))
        self.bot.engine.settings = self.bot.settings
        self.bot.last_requote = self.clock.monotonic()
        self.clock.advance(.01)
        self.publish(center='100.02')
        self.bot.engine.mark = (D('99.96'), self.clock.monotonic())
        for order in self.bot.state.active_orders(MARKET):
            for reference in (self.center, self.bot.engine.mark[0]):
                distance = abs(order.price/reference-1)*D(10000)
                self.assertGreaterEqual(distance, self.bot.settings.min_offset_bps)
                self.assertLessEqual(distance, self.bot.settings.band_bps-self.bot.settings.band_margin_bps)
        # 新理论卖价为 100.15，旧卖价 100.12 已超过收紧后的普通重报阈值。
        self.assertGreater(abs(D('100.12')/D('100.15')-1)*D(10000), self.bot.settings.requote_bps)
        self.assertEqual(self.retention_failures(), [])
        await self.drive(self.bot.tick())
        self.assertEqual(self.deletes(), [], '理论价超出 mark 区间不应绕过合规旧单的普通重报节流')
        self.assertEqual(self.remote, before)
        self.assertEqual(len(self.bot.state.active_orders(MARKET)), 2)

    async def test_default_evaluation_rejects_new_theoretical_price_even_with_compliant_old_pair(self):
        await self.live_pair()
        self.publish(center='100.02')
        self.bot.engine.mark = (D('99.96'), self.clock.monotonic())
        decision = self.bot.engine.evaluate(self.bot.book, self.bot.state, self.bot.rules,
                                           now=self.clock.monotonic(), guard_only=True)
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.reason, '标记价格辅助守卫')

    async def test_partial_remainders_below_m4_minimum_cancel_immediately(self):
        await self.live_pair()
        self.bot.rules = MarketRules(D('.01'), D('.001'), D(10))
        at = int(self.wall()*1000)
        for order in self.bot.state.active_orders(MARKET):
            row = self.remote[order.client_order_id]
            row.update(status='PARTIAL', filledSize=str(order.quantity-D('.01')), time_ms=at)
            self.bot.state.apply_order_event(row)
        self.bot.state.apply_position_snapshot(MARKET, '0', at+1, source='ws')
        self.assertTrue(await self.drive(self.bot.reconcile_once()))
        bids, asks = [(D('99.9'), D(10)), (D(99), D(10))], [(D('100.1'), D(10)), (D(101), D(10))]
        for order in self.bot.state.active_orders(MARKET):
            self.assertLess(order.remaining*order.price, self.bot.rules.min_notional)
            (bids if order.side == 'buy' else asks).append((order.price, order.remaining))
        self.bot.book = book(bids=bids, asks=asks, at=int(self.wall()*1000))
        self.bot.book_seen = self.clock.monotonic()
        self.bot.engine.mark = (D(100), self.clock.monotonic())
        self.bot.last_requote = self.clock.monotonic()
        self.assertTrue(self.retention_failures())
        await self.drive(self.bot.tick())
        self.assertEqual(len(self.deletes()), 1, '部分成交剩余量低于 M4 最小金额时必须立即撤单')
        self.assertFalse(self.remote)
        self.assertFalse(self.bot.state.active_orders(MARKET))
        self.assertIn('最小金额', self.bot.last_stop_reason)

    async def test_compliant_pair_above_theoretical_threshold_waits_for_requote_interval(self):
        await self.live_pair(tick='.001')
        self.set_outer_pair()
        self.bot.last_requote = self.clock.monotonic()
        self.clock.advance(.01)
        self.publish(center='100.035')
        self.assertEqual(self.retention_failures(), [])
        await self.drive(self.bot.tick())
        self.assertEqual(self.deletes(), [], '通过完整 M4 复核的普通重报必须等待节流')
        self.assertEqual(len(self.remote), 2)
        self.clock.advance(self.settings.min_requote_interval_s)
        self.publish()
        await self.drive(self.bot.tick())
        self.assertEqual(len(self.deletes()), 1)
        self.assertEqual(self.deletes()[0].path_query.partition('?')[2], 'market='+MARKET)
        self.assertEqual(self.remote, {})
        self.assertFalse(self.bot.state.active_orders(MARKET))
        self.assertFalse(self.bot.state.stopping)
        self.assertFalse([request for request in self.requests if request.method == 'POST'])
        cancel_index = self.requests.index(self.deletes()[0])
        self.assertTrue(any('status=open' in request.path_query
                            for request in self.requests[cancel_index+1:]))
        self.clock.advance(.1)
        self.publish()
        await self.drive(self.bot.tick())
        self.assertFalse(self.bot.state.stopping)
        self.assertEqual(self.bot.state.market_mode(MARKET), 'RUNNING')
        self.assertEqual(len(self.remote), 2)

    async def test_out_of_band_pair_cancels_immediately_despite_requote_interval(self):
        await self.live_pair()
        self.bot.last_requote = self.clock.monotonic()
        self.clock.advance(.01)
        self.publish(center='100.15')
        await self.drive(self.bot.tick())
        self.assertTrue(self.deletes(), '价格越界属于安全路径，不能被普通重报间隔延迟')
        self.assertEqual(self.deletes()[0].path_query.partition('?')[2], 'market='+MARKET)
        self.assertEqual(self.remote, {})
        self.assertFalse(self.bot.state.active_orders(MARKET))

    async def test_normal_requote_reconciles_then_next_tick_reprices_after_one_basis_point_move(self):
        await self.live_pair(tick='.001')
        self.set_outer_pair()
        self.clock.advance(self.settings.min_requote_interval_s)
        self.publish(center='100.035')
        transport = self.bot.api.transport
        moved = False

        async def moving_balance(request):
            nonlocal moved
            response = await transport(request)
            if request.path_query.partition('?')[0].endswith('/balance') and not moved:
                moved = True
                self.publish(center='100.025')
            return response

        self.bot.api.transport = moving_balance
        await self.drive(self.bot.tick())
        self.assertTrue(moved, '普通重报核零后本节拍应先完成恢复对账')
        self.assertFalse(self.bot.state.stopping)
        self.assertFalse([request for request in self.requests if request.method == 'POST'])
        self.assertEqual(self.bot.guard.state, 'RUNNING')
        self.clock.advance(.1)
        self.publish()
        await self.drive(self.bot.tick())
        self.assertEqual(self.bot.guard.state, 'RUNNING', '对账期间 1bp 微动不能触发下单失败冷却')
        self.assertEqual(len(self.remote), 2)
        self.assertEqual({D(row['price']) for row in self.remote.values()},
                         {D('99.904'), D('100.146')})

    async def micro_moves(self, *, period):
        await self.live_pair(tick='.001')
        self.set_outer_pair()
        # 使用明确合规的外沿旧价覆盖普通重报；更快反向微动仍可能触发安全撤单。
        start = self.clock.monotonic()
        ordinary_batches, safety_batches = [], []
        total_deletes = 0
        for step in range(1, 3001):
            at = start+step/10
            self.clock.advance(max(0, at-self.clock.monotonic()))
            phase = step % period
            shift = D(min(phase, period-phase))*D('.035')/(period//2)
            self.publish(center=D(100)+shift)
            active_ids = set(self.remote)
            failures = self.retention_failures()
            before = len(self.deletes())
            await self.drive(self.bot.tick())
            new_deletes = self.deletes()[before:]
            if new_deletes:
                total_deletes += len(new_deletes)
                self.assertEqual(len(new_deletes), 1, '每次撤销双边只发送一次市场全撤')
                self.assertEqual(new_deletes[0].path_query.partition('?')[2], 'market='+MARKET)
                if self.bot.last_stop_reason == '合规挂单偏离理论价超过重报阈值，按市场撤销双边':
                    self.assertFalse(failures, '普通重报前旧单必须通过完整 M4 复核')
                    ordinary_batches.append(at)
                else:
                    self.assertTrue(self.bot.last_stop_reason)
                    self.assertTrue(failures, '安全撤单必须有完整 M4 复核失败的实际证据')
                    safety_batches.append(at)
            elif self.remote and set(self.remote) != active_ids:
                # 新一轮测试样本继续使用已存在的外沿旧价，避免理论内沿遮蔽节流覆盖。
                self.set_outer_pair()
        self.assertTrue(ordinary_batches, '微动样本必须实际触发通过完整 M4 复核的普通重报')
        self.assertLessEqual(len(ordinary_batches), 300/self.settings.min_requote_interval_s)
        self.assertEqual(total_deletes, len(ordinary_batches)+len(safety_batches),
                         '每个实际 DELETE 都必须按普通重报或安全撤单计入')
        self.assertTrue(all(right-left >= self.settings.min_requote_interval_s-.01
                            for left, right in zip(ordinary_batches, ordinary_batches[1:])))
        return safety_batches

    async def test_five_minutes_of_micro_moves_bounds_actual_ordinary_requote_deletes(self):
        await self.micro_moves(period=700)

    async def test_five_minutes_of_twenty_four_second_micro_moves_bounds_only_ordinary_requotes(self):
        safety_batches = await self.micro_moves(period=240)
        self.assertTrue(safety_batches, '快速反向微动还必须实际覆盖完整 M4 失败后的安全撤单')


if __name__ == '__main__':
    unittest.main()
