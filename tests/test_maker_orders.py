"""Test order safety states and asynchronous races without connecting to an exchange."""

import asyncio
import unittest
from decimal import Decimal as D

from pendle.maker_orders import (AccountState, OrderManager, OrderStatus,
                                 OrdersBlocked, OrderStateError)


class Clock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now

    async def sleep(self, delay):
        self.now += delay
        await asyncio.sleep(0)


class API:
    def __init__(self):
        self.calls = []
        self.rows = {}
        self.open_rows = []
        self.place = None
        self.query = None
        self.cancel = None

    async def place_order(self, body, *, before_send=None):
        if before_send:
            before_send()
        self.calls.append(('place', body['clientOrderId']))
        if self.place:
            return await self.place(body)
        return row(body['clientOrderId'], quantity=body['size'],
                   side=body['side'], price=body['price'])

    async def get_order(self, cid):
        self.calls.append(('query', cid))
        if self.query:
            return await self.query(cid)
        return self.rows.get(cid)

    async def cancel_order(self, cid):
        self.calls.append(('cancel', cid))
        if self.cancel:
            return await self.cancel(cid)
        return {}

    async def cancel_all(self, market, *, stop=False):
        self.calls.append(('cancel_all', market, stop))
        return {}

    async def get_open_orders(self, market, *, stop=False):
        self.calls.append(('open', market, stop))
        return list(self.open_rows)

    async def get_positions(self):
        return [{'market': 'IBM-USD.P', 'quantity': '0', 'time_ms': 10}]

    async def get_balances(self):
        return {'equity': '1000', 'available': '900', 'time_ms': 10}


def row(cid, *, status='OPEN', filled='0', at=10, quantity='2', side='buy', price='100'):
    return {'clientOrderId': cid, 'orderId': 'ex-' + cid,
            'market': 'IBM-USD.P', 'side': side, 'size': quantity,
            'filledSize': filled, 'price': price, 'status': status, 'time_ms': at}


class OrdersTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock, self.api = Clock(), API()
        self.alerts, self.halts = [], []
        async def alert(reason):
            self.alerts.append(reason)
        async def halt(reason):
            self.halts.append(reason)
        self.state = AccountState(['IBM-USD.P'])
        self.manager = OrderManager(self.state, self.api, risk_check=lambda order, state: None,
                                    monotonic=self.clock, sleep=self.clock.sleep,
                                    alert=alert, halt=halt, request_timeout_s=2)
        self.state.deadman_ack()
        self.assertTrue(await self.manager.reconcile('IBM-USD.P'))
        self.api.calls.clear()

    def intent(self, side='buy'):
        return self.manager.create_intent('IBM-USD.P', side, '2', '100')

    async def test_startup_and_deadman_ack_are_both_required(self):
        state = AccountState(['IBM-USD.P'])
        manager = OrderManager(state, self.api, risk_check=lambda *args: None)
        with self.assertRaises(OrdersBlocked):
            manager.create_intent('IBM-USD.P', 'buy', '1', '100')
        self.assertFalse(await manager.reconcile('IBM-USD.P'))
        state.deadman_ack()
        self.assertTrue(await manager.reconcile('IBM-USD.P'))
        self.assertIsNotNone(manager.create_intent('IBM-USD.P', 'buy', '1', '100'))

    async def test_new_intent_sent_open_and_fixed_id(self):
        order = self.intent()
        self.assertEqual(order.status, OrderStatus.INTENT)
        await self.manager.submit(order)
        self.assertEqual(order.status, OrderStatus.OPEN)
        self.assertEqual(self.api.calls, [('place', order.client_order_id)])
        with self.assertRaises(OrderStateError):
            await self.manager.submit(order)
        with self.assertRaises(OrdersBlocked):
            self.intent()

    async def test_ids_are_never_reused_across_same_named_runs(self):
        first = self.intent().client_order_id
        manager = OrderManager(self.state, self.api, risk_check=lambda *args: None)
        other = manager.create_intent('IBM-USD.P', 'sell', '1', '100').client_order_id
        self.assertNotEqual(first, other)
        self.assertLessEqual(len(other), 64)

    async def test_send_rechecks_sync_and_risk_at_actual_dispatch(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def queued(body, *, before_send=None):
            entered.set()
            await release.wait()
            before_send()
            self.api.calls.append(('unsafe-place',))
        self.api.place_order = queued
        order = self.intent()
        task = asyncio.create_task(self.manager.submit(order))
        await entered.wait()
        self.state.begin_sync('IBM-USD.P', '断线')
        release.set()
        await task
        self.assertEqual(order.status, OrderStatus.REJECTED)
        self.assertNotIn(('unsafe-place',), self.api.calls)

    async def test_timeout_unknown_blocks_both_sides_and_confirms_absence(self):
        async def fail(body):
            raise TimeoutError('超时')
        self.api.place = fail
        order = self.intent()
        await self.manager.submit(order)
        self.assertEqual(order.status, OrderStatus.UNKNOWN)
        for side in ('buy', 'sell'):
            with self.assertRaises(OrdersBlocked):
                self.intent(side)
        self.assertTrue(await self.manager.resolve_unknown(order))
        self.assertEqual(order.status, OrderStatus.ABSENT)
        calls = [call[0] for call in self.api.calls]
        self.assertEqual(calls, ['place', 'query', 'query', 'query',
                                 'cancel_all', 'open', 'query'])
        self.assertEqual(self.clock.now - order.unknown_since, 6)
        self.assertTrue(self.alerts)
        with self.assertRaises(OrdersBlocked):
            self.intent('sell')

    async def test_unknown_remains_if_open_order_exists(self):
        async def fail(body):
            raise OSError('失联')
        self.api.place = fail
        order = self.intent()
        await self.manager.submit(order)
        self.api.open_rows = [row(order.client_order_id)]
        self.assertFalse(await self.manager.resolve_unknown(order))
        self.assertEqual(order.status, OrderStatus.UNKNOWN)

    async def test_found_unknown_is_not_submitted_again(self):
        async def fail(body):
            raise TimeoutError
        self.api.place = fail
        order = self.intent()
        await self.manager.submit(order)
        self.api.rows[order.client_order_id] = row(order.client_order_id)
        self.assertTrue(await self.manager.resolve_unknown(order))
        self.assertEqual(order.status, OrderStatus.OPEN)
        self.assertEqual(sum(c[0] == 'place' for c in self.api.calls), 1)

    async def test_delayed_acceptance_after_absence_is_immediately_cancelled(self):
        async def fail(body):
            raise TimeoutError
        self.api.place = fail
        order = self.intent()
        await self.manager.submit(order)
        await self.manager.resolve_unknown(order)
        await self.manager.handle_order_event(row(order.client_order_id, at=100))
        self.assertEqual(order.status, OrderStatus.CANCEL_SENT)
        self.assertEqual(self.api.calls[-1], ('cancel', order.client_order_id))
        self.assertGreaterEqual(len(self.alerts), 2)

    async def test_unknown_deadline_includes_stuck_query(self):
        async def fail(body):
            raise TimeoutError
        self.api.place = fail
        order = self.intent()
        await self.manager.submit(order)
        async def hung(cid):
            await asyncio.Event().wait()
        self.api.query = hung
        self.clock.now = order.unknown_since + 59
        self.assertFalse(await self.manager.resolve_unknown(order))
        self.assertTrue(self.halts)
        self.assertEqual(order.status, OrderStatus.UNKNOWN)
        self.assertLessEqual(self.clock.now - order.unknown_since, 60)

    async def test_partial_fill_during_cancel_does_not_release_replacement(self):
        order = self.intent()
        await self.manager.submit(order)
        await self.manager.cancel(order)
        self.assertEqual(order.status, OrderStatus.CANCEL_SENT)
        self.state.apply_fill('f1', 'IBM-USD.P', 'buy', '1', 11,
                              client_order_id=order.client_order_id)
        self.assertEqual(order.remaining, D(1))
        self.assertEqual(order.status, OrderStatus.CANCEL_SENT)
        with self.assertRaises(OrdersBlocked):
            self.intent()
        await self.manager.handle_order_event(row(order.client_order_id, status='CANCELED',
                                                   filled='1', at=12))
        self.assertEqual(order.status, OrderStatus.CANCELED)
        self.assertIsNotNone(self.intent())
        self.assertEqual(self.state.positions['IBM-USD.P'], D(1))

    async def test_cancel_timeout_escalates_and_requires_terminal_evidence(self):
        order = self.intent()
        await self.manager.submit(order)
        await self.manager.cancel(order)
        self.assertFalse(await self.manager.confirm_cancel(order))
        self.assertTrue(any(c[0] == 'cancel_all' for c in self.api.calls))
        self.assertTrue(self.halts)
        self.assertEqual(order.status, OrderStatus.CANCEL_SENT)

    async def test_old_rest_and_duplicate_events_cannot_resurrect_terminal_order(self):
        order = self.intent()
        await self.manager.submit(order)
        await self.manager.handle_order_event(row(order.client_order_id, status='FILLED', filled='2', at=30))
        self.state.apply_order_event(row(order.client_order_id, at=20), source='rest')
        self.state.apply_order_event(row(order.client_order_id, status='FILLED', filled='2', at=30))
        self.assertEqual(order.status, OrderStatus.FILLED)
        self.assertEqual(order.remaining, D(0))

    async def test_fill_deduplication_and_position_watermark(self):
        self.state.apply_position_snapshot('IBM-USD.P', '5', 100)
        self.assertTrue(self.state.apply_fill('old', 'IBM-USD.P', 'buy', '2', 90,
                                              realized_pnl='3', fee='0.1'))
        self.assertEqual(self.state.positions['IBM-USD.P'], D(5))
        self.assertTrue(self.state.apply_fill('new', 'IBM-USD.P', 'sell', '1', 110))
        self.assertFalse(self.state.apply_fill('new', 'IBM-USD.P', 'sell', '1', 110))
        self.assertEqual(self.state.positions['IBM-USD.P'], D(4))
        self.assertEqual(self.state.realized_pnl, D(3))
        self.assertEqual(self.state.fees, D('0.1'))

    async def test_rest_rebase_replays_only_fills_newer_than_snapshot(self):
        self.state.apply_fill('a', 'IBM-USD.P', 'buy', '2', 20)
        self.state.apply_fill('b', 'IBM-USD.P', 'buy', '3', 40)
        self.state.apply_position_snapshot('IBM-USD.P', '2', 30)
        self.assertEqual(self.state.positions['IBM-USD.P'], D(5))
        self.state.apply_position_snapshot('IBM-USD.P', '0', 10)
        self.assertEqual(self.state.positions['IBM-USD.P'], D(5))
        self.state.apply_position_snapshot('IBM-USD.P', '4', 35)
        self.assertEqual(self.state.positions['IBM-USD.P'], D(7))
        self.assertEqual(self.state.market_mode('IBM-USD.P'), 'SYNCING')

    async def test_reconnect_requires_new_deadman_ack(self):
        self.state.private_disconnected()
        self.assertFalse(await self.manager.reconcile('IBM-USD.P'))
        self.state.deadman_ack()
        self.assertTrue(await self.manager.reconcile('IBM-USD.P'))

    async def test_reconciliation_does_not_cross_new_sync_generation(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.api.get_balances
        async def balance():
            entered.set()
            await release.wait()
            return await original()
        self.api.get_balances = balance
        task = asyncio.create_task(self.manager.reconcile('IBM-USD.P'))
        await entered.wait()
        self.state.begin_sync('IBM-USD.P', '再次断线')
        release.set()
        self.assertFalse(await task)
        self.assertEqual(self.state.market_mode('IBM-USD.P'), 'SYNCING')

    async def test_stop_disables_intents_before_waiting_for_inflight(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed(body):
            entered.set()
            await release.wait()
            return row(body['clientOrderId'])
        self.api.place = delayed
        order = self.intent()
        send = asyncio.create_task(self.manager.submit(order))
        await entered.wait()
        stop = asyncio.create_task(self.manager.stop('退出'))
        await asyncio.sleep(0)
        with self.assertRaises(OrdersBlocked):
            self.intent('sell')
        release.set()
        await send
        self.assertTrue(await stop)
        calls = [c[0] for c in self.api.calls]
        self.assertLess(calls.index('place'), calls.index('cancel_all'))
        self.assertEqual(calls[-1], 'open')

    async def test_stop_does_not_claim_empty_while_write_still_inflight(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed(body):
            entered.set()
            await release.wait()
            return row(body['clientOrderId'])
        self.api.place = delayed
        order = self.intent()
        send = asyncio.create_task(self.manager.submit(order))
        await entered.wait()
        self.assertFalse(await self.manager.stop('超时退出'))
        self.assertTrue(self.halts or self.alerts)
        release.set()
        await send
        await self.manager.drain()
        self.assertTrue(any(c[0] == 'cancel_all' for c in self.api.calls))

    async def test_risk_failure_never_reaches_transport(self):
        order = self.intent()
        def reject(*args):
            raise ValueError('风险超限')
        self.manager.risk_check = reject
        await self.manager.submit(order)
        self.assertEqual(order.status, OrderStatus.REJECTED)
        self.assertFalse(self.api.calls)

    async def test_unknown_error_is_not_misreported_as_rejection(self):
        async def fail(body):
            raise RuntimeError('传输未知故障')
        self.api.place = fail
        order = self.intent()
        await self.manager.submit(order)
        self.assertEqual(order.status, OrderStatus.UNKNOWN)
        self.assertTrue(self.alerts)

    async def test_fill_rejects_wrong_order_side_without_partial_accounting(self):
        order = self.intent()
        await self.manager.submit(order)
        before = dict(self.state.positions)
        with self.assertRaises(OrderStateError):
            self.state.apply_fill('wrong', 'IBM-USD.P', 'sell', '1', 12,
                                  client_order_id=order.client_order_id, fee='1')
        self.assertEqual(self.state.positions, before)
        self.assertNotIn('wrong', self.state.fills)
        self.assertEqual(self.state.fees, D(0))

    async def test_overfill_is_rejected_before_any_ledger_mutation(self):
        order = self.intent()
        await self.manager.submit(order)
        with self.assertRaises(OrderStateError):
            self.state.apply_fill('large', 'IBM-USD.P', 'buy', '3', 12,
                                  client_order_id=order.client_order_id)
        self.assertEqual(self.state.positions['IBM-USD.P'], 0)
        self.assertFalse(self.state.fills)
        self.assertEqual(order.remaining, D(2))

    async def test_cumulative_watermark_plus_new_fill_and_late_old_fill(self):
        order = self.intent()
        await self.manager.submit(order)
        await self.manager.handle_order_event(row(order.client_order_id, status='PARTIAL',
                                                   filled='1', at=20))
        self.state.apply_fill('newer', 'IBM-USD.P', 'buy', '1', 30,
                              client_order_id=order.client_order_id)
        self.assertEqual(order.status, OrderStatus.FILLED)
        self.assertEqual(order.remaining, 0)
        self.state.apply_fill('older', 'IBM-USD.P', 'buy', '1', 19,
                              client_order_id=order.client_order_id)
        self.assertEqual(order.filled, 2)
        self.assertEqual(self.state.positions['IBM-USD.P'], 2)

    async def test_reconcile_missing_local_open_does_not_claim_consistency(self):
        order = self.intent()
        await self.manager.submit(order)
        self.assertFalse(await self.manager.reconcile('IBM-USD.P'))
        self.assertEqual(self.state.market_mode('IBM-USD.P'), 'SYNCING')
        self.api.rows[order.client_order_id] = row(order.client_order_id, status='CANCELED', at=12)
        self.assertTrue(await self.manager.reconcile('IBM-USD.P'))

    async def test_timeout_cannot_wait_forever_for_cancel_suppressing_query(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def fail(body):
            raise TimeoutError
        self.api.place = fail
        order = self.intent()
        await self.manager.submit(order)
        async def stubborn(cid):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            return None
        self.api.query = stubborn
        self.clock.now = order.unknown_since + 59
        resolving = asyncio.create_task(self.manager.resolve_unknown(order))
        await entered.wait()
        for _ in range(30):
            await asyncio.sleep(0)
            if resolving.done():
                break
        try:
            self.assertTrue(resolving.done(), '60秒时限不能被不合作的传输取消阻塞')
            self.assertFalse(await resolving)
            self.assertTrue(self.halts)
        finally:
            release.set()
            await resolving
            await self.manager.drain()

    async def test_dispatch_allows_only_explicit_timestamp_retry_with_same_id(self):
        attempts = []
        async def timestamp_retry(body, *, before_send=None):
            before_send()
            attempts.append(body['clientOrderId'])
            before_send(timestamp_retry=True)
            attempts.append(body['clientOrderId'])
            return row(body['clientOrderId'])
        self.api.place_order = timestamp_retry
        order = self.intent()
        await self.manager.submit(order)
        self.assertEqual(order.status, OrderStatus.OPEN)
        self.assertEqual(attempts, [order.client_order_id, order.client_order_id])

    async def test_shutdown_late_ack_actively_repeats_bulk_cancel(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed(body):
            entered.set()
            await release.wait()
            return row(body['clientOrderId'])
        self.api.place = delayed
        order = self.intent()
        send = asyncio.create_task(self.manager.submit(order))
        await entered.wait()
        self.assertFalse(await self.manager.stop('停止'))
        count = sum(c[0] == 'cancel_all' for c in self.api.calls)
        release.set()
        await send
        for _ in range(40):
            await asyncio.sleep(0)
        self.assertGreater(sum(c[0] == 'cancel_all' for c in self.api.calls), count)
        await self.manager.drain()

    async def test_api_raw_write_keeps_unknown_and_prevents_clean_stop(self):
        async def fail(body):
            raise TimeoutError
        self.api.place = fail
        self.api.in_flight_writes = 1
        order = self.intent()
        await self.manager.submit(order)
        self.assertFalse(await self.manager.resolve_unknown(order))
        self.assertEqual(order.status, OrderStatus.UNKNOWN)
        self.assertFalse(await self.manager.stop('仍有真实写入在途'))
        self.api.in_flight_writes = 0
        self.assertTrue(await self.manager.stop('写入结束后再次核实'))

    async def test_guard_resume_requires_confirmed_stop_then_new_reconciliation(self):
        self.assertTrue(await self.manager.stop('守卫'))
        self.assertIs(self.manager.prepare_resume(), True)
        self.assertEqual(self.state.market_mode('IBM-USD.P'), 'SYNCING')
        with self.assertRaises(OrdersBlocked):
            self.intent()
        self.assertTrue(await self.manager.reconcile('IBM-USD.P'))
        self.assertIsNotNone(self.intent())

    async def test_prepare_resume_waits_for_inflight_stop_without_changing_barrier(self):
        self.state.stopping = True
        self.state.modes['IBM-USD.P'] = 'STOPPING'
        stopping = asyncio.get_running_loop().create_future()
        self.manager._stop_task = stopping
        try:
            self.assertIs(self.manager.prepare_resume(), False)
            self.assertTrue(self.state.stopping)
            self.assertIs(self.manager._stop_task, stopping)
            self.assertFalse(stopping.done())
            self.assertEqual(self.state.market_mode('IBM-USD.P'), 'STOPPING')
            with self.assertRaises(OrdersBlocked):
                self.intent()
            self.assertEqual(self.api.calls, [])
        finally:
            stopping.cancel()

    async def test_prepare_resume_waits_for_pending_and_inflight_writes(self):
        for blocker in ('pending', 'inflight'):
            with self.subTest(blocker=blocker):
                self.assertTrue(await self.manager.stop('写入等待'))
                stopping = self.manager._stop_task
                writing = asyncio.get_running_loop().create_future()
                self.manager.pending = {'pending-write': writing} if blocker == 'pending' else {}
                self.api.in_flight_writes = int(blocker == 'inflight')
                try:
                    self.assertIs(self.manager.prepare_resume(), False)
                    self.assertTrue(self.state.stopping)
                    self.assertIs(self.manager._stop_task, stopping)
                    self.assertEqual(self.state.market_mode('IBM-USD.P'), 'STOPPING')
                finally:
                    writing.cancel()
                    self.manager.pending.clear()
                    self.api.in_flight_writes = 0

    async def test_prepare_resume_keeps_hard_failures_ahead_of_transient_waits(self):
        from pendle.ondo_api import SendGate
        for blocker in ('false', 'exception', 'cancelled', 'missing_stop',
                        'closed_gate', 'trading_halted', 'active_order', 'missing_ack'):
            with self.subTest(blocker=blocker):
                self.state.stopping = False
                self.state.deadman_acked = True
                self.state.modes['IBM-USD.P'] = 'RUNNING'
                self.state.orders.clear()
                self.api.gate = SendGate()
                if blocker == 'active_order':
                    self.intent()
                self.state.stopping = True
                self.state.modes['IBM-USD.P'] = 'STOPPING'
                stopping = asyncio.get_running_loop().create_future()
                self.manager._stop_task = stopping
                if blocker == 'false':
                    stopping.set_result(False)
                elif blocker == 'exception':
                    stopping.set_exception(RuntimeError('停止失败'))
                elif blocker == 'cancelled':
                    stopping.cancel()
                elif blocker == 'missing_stop':
                    self.manager._stop_task = None
                elif blocker == 'closed_gate':
                    self.api.gate.close()
                elif blocker == 'trading_halted':
                    self.api.gate.halt_trading()
                elif blocker == 'missing_ack':
                    self.state.deadman_acked = False
                writing = asyncio.get_running_loop().create_future()
                self.manager.pending = {'pending-write': writing}
                try:
                    if blocker == 'active_order':
                        # Orders may still be cancelling while cleanup is in flight: wait, then fail once settled.
                        self.assertFalse(self.manager.prepare_resume())
                        self.assertTrue(self.state.stopping)
                        writing.cancel()
                        self.manager.pending.clear()
                        stopping.set_result(True)
                    with self.assertRaises(OrdersBlocked):
                        self.manager.prepare_resume()
                    self.assertTrue(self.state.stopping)
                finally:
                    writing.cancel()
                    self.manager.pending.clear()
                    if not stopping.done():
                        stopping.cancel()
                    elif not stopping.cancelled():
                        stopping.exception()

    async def test_failed_stop_cannot_be_resumed(self):
        self.api.open_rows = [row('other')]
        self.assertFalse(await self.manager.stop('守卫'))
        with self.assertRaises(OrdersBlocked):
            self.manager.prepare_resume()

    async def test_reduce_only_passive_and_ioc_are_explicit(self):
        passive = self.manager.create_intent('IBM-USD.P', 'sell', '1', '100', reduce_only=True)
        bodies = []
        async def place(body, *, before_send=None):
            before_send()
            bodies.append(body)
            return row(body['clientOrderId'], side='sell', quantity='1')
        self.api.place_order = place
        await self.manager.submit(passive)
        self.assertEqual(bodies[0]['timeInForce'], 'GTC')
        self.assertTrue(bodies[0]['postOnly'])
        await self.manager.handle_order_event(row(passive.client_order_id, status='CANCELED',
                                                   quantity='1', side='sell', at=11))
        ioc = self.manager.create_intent('IBM-USD.P', 'sell', '1', '100', reduce_only=True,
                                        time_in_force='IOC', post_only=False)
        await self.manager.submit(ioc)
        self.assertEqual(bodies[-1]['timeInForce'], 'IOC')
        self.assertFalse(bodies[-1]['postOnly'])

    async def test_equal_time_rest_position_does_not_overwrite_ws_position(self):
        self.state.apply_position_snapshot('IBM-USD.P', '2', 100, source='ws')
        self.state.apply_position_snapshot('IBM-USD.P', '0', 100, source='rest')
        self.assertEqual(self.state.positions['IBM-USD.P'], 2)
        self.assertEqual(self.state.market_mode('IBM-USD.P'), 'SYNCING')

    async def test_business_halt_error_applies_even_with_http_200(self):
        from pendle.ondo_api import ApiError
        async def fail(body):
            raise ApiError(200, 'account_in_liquidation')
        self.api.place = fail
        order = self.intent()
        await self.manager.submit(order)
        self.assertTrue(self.halts)
        self.assertTrue(self.state.stopping)
        self.assertEqual(order.last_error_category, 'halt')

    async def test_rejections_expose_error_category_for_m2_policy(self):
        from pendle.ondo_api import ApiError
        async def fail(body):
            raise ApiError(400, 'post_only_has_match')
        self.api.place = fail
        order = self.intent()
        await self.manager.submit(order)
        self.assertEqual(order.status, OrderStatus.REJECTED)
        self.assertEqual(order.last_error_code, 'post_only_has_match')
        self.assertEqual(order.last_error_category, 'recompute')

    async def test_cancel_auth_failure_is_immediate_halt(self):
        from pendle.ondo_api import ApiError
        order = self.intent()
        await self.manager.submit(order)
        async def fail(cid):
            raise ApiError(401)
        self.api.cancel = fail
        await self.manager.cancel(order)
        self.assertTrue(self.halts)
        self.assertTrue(self.state.stopping)

    async def test_reconcile_auth_failure_is_immediate_halt(self):
        from pendle.ondo_api import ApiError
        async def fail():
            raise ApiError(401)
        self.api.get_positions = fail
        self.assertFalse(await self.manager.reconcile('IBM-USD.P'))
        self.assertTrue(self.halts)
        self.assertTrue(self.state.stopping)


class IntegrationClock:
    def __init__(self):
        self.now, self.waiters = 0, []

    def __call__(self):
        return self.now

    async def sleep(self, delay):
        waiter = asyncio.get_running_loop().create_future()
        self.waiters.append((self.now + delay, waiter))
        await waiter

    async def drive(self, awaitable, seconds=80):
        task = asyncio.ensure_future(awaitable)
        for _ in range(seconds):
            for _ in range(25):
                await asyncio.sleep(0)
            if task.done():
                return task.result()
            self.now += 1
            for deadline, waiter in self.waiters:
                if deadline <= self.now and not waiter.done():
                    waiter.set_result(None)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise AssertionError('可控假时钟预算耗尽')


class ModuleIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from pendle.ondo_api import Credentials, HttpResponse, OndoAPI, SendGate
        from pendle.maker_risk import MarketRisk, RiskManager, RiskOrder
        self.clock, self.gate, self.sent = IntegrationClock(), SendGate(), []
        self.post_handler = None
        self.halts = []
        self.HttpResponse = HttpResponse
        async def transport(request):
            import json
            self.sent.append(request)
            if request.method == 'POST':
                body = json.loads(request.body)
                if self.post_handler:
                    return await self.post_handler(body)
                result = row(body['clientOrderId'], side=body['side'], quantity=body['size'])
            elif request.path_query.startswith('/v1/perps/orders/client:'):
                return HttpResponse(404, {'success': False, 'error': 'order_not_found'})
            elif request.path_query.startswith('/v1/perps/orders'):
                result = []
            elif request.path_query == '/v1/perps/positions':
                result = [{'market': 'IBM-USD.P', 'quantity': '0', 'time_ms': 10}]
            elif request.path_query == '/v1/perps/balance':
                result = {'equity': '1000', 'available': '900', 'time_ms': 10}
            else:
                raise AssertionError('集成测试出现意外路径')
            return HttpResponse(200, {'success': True, 'result': result})
        self.api = OndoAPI(Credentials('fake-key', 'ondoApiSecret_fake'), transport=transport,
                           gate=self.gate, clock=self.clock, monotonic=self.clock,
                           sleep=self.clock.sleep, timeout_s=3)
        self.state = AccountState(['IBM-USD.P'])
        risk = RiskManager()
        def risk_check(candidate, account):
            orders = [RiskOrder(order.market, order.side, order.remaining, order.price, order.status)
                      for order in account.active_orders() if order is not candidate]
            return_value = risk.check_order(candidate.market, candidate.side, candidate.quantity,
                candidate.price, [MarketRisk('IBM-USD.P', account.positions['IBM-USD.P'], D(100))],
                orders, account.balances['equity'], account.balances['available'],
                reduce_only=candidate.reduce_only)
            self.last_risk = return_value
        async def halt(reason):
            self.halts.append(reason)
        self.manager = OrderManager(self.state, self.api, risk_check=risk_check,
                                    monotonic=self.clock, sleep=self.clock.sleep,
                                    request_timeout_s=5, halt=halt)
        self.state.deadman_ack()
        self.assertTrue(await self.clock.drive(self.manager.reconcile('IBM-USD.P')))
        self.sent.clear()

    async def test_real_client_risk_and_closed_gate_stop(self):
        order = self.manager.create_intent('IBM-USD.P', 'buy', '2', '100')
        await self.clock.drive(self.manager.submit(order))
        self.assertEqual(order.status, OrderStatus.OPEN)
        self.assertEqual(self.last_risk.account_exposure, 200)
        self.gate.close()
        self.assertTrue(await self.clock.drive(self.manager.stop('看门狗触发')))
        self.assertFalse(self.gate.is_open)
        self.assertEqual(order.status, OrderStatus.CANCELED)
        self.assertEqual([req.method for req in self.sent], ['POST', 'DELETE', 'GET'])
        self.assertEqual(self.sent[-1].path_query, '/v1/perps/orders?market=IBM-USD.P&status=open')
        from pendle.ondo_api import GateClosed
        with self.assertRaises(GateClosed):
            await self.api.get_open_orders('IBM-USD.P', stop=True)

    async def test_real_timestamp_error_halts_without_retry(self):
        bodies = []
        async def post(body):
            bodies.append(body)
            return self.HttpResponse(400, {'success': False, 'error_code': 'timestamp_too_far'})
        self.post_handler = post
        order = self.manager.create_intent('IBM-USD.P', 'buy', '2', '100')
        await self.clock.drive(self.manager.submit(order))
        self.assertEqual(order.status, OrderStatus.REJECTED)
        self.assertEqual(len(bodies), 1)
        self.assertTrue(self.halts)
        self.assertTrue(self.state.stopping)
        self.assertEqual([req.method for req in self.sent], ['POST'])

    async def test_real_transport_late_write_cannot_confirm_absence_or_clean_stop(self):
        release = asyncio.Event()
        async def post(body):
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            return self.HttpResponse(200, {'success': True, 'result': row(body['clientOrderId'])})
        self.post_handler = post
        order = self.manager.create_intent('IBM-USD.P', 'buy', '2', '100')
        try:
            await self.clock.drive(self.manager.submit(order))
            self.assertEqual(order.status, OrderStatus.UNKNOWN)
            self.assertEqual(self.api.in_flight_writes, 1)
            self.assertFalse(await self.clock.drive(self.manager.resolve_unknown(order)))
            self.assertEqual(order.status, OrderStatus.UNKNOWN)
            self.gate.close()
            self.assertFalse(await self.clock.drive(self.manager.stop('取消无响应的写请求')))
            self.assertLessEqual(sum(req.method == 'DELETE' for req in self.sent), 4)
        finally:
            release.set()
            for _ in range(20):
                await asyncio.sleep(0)
        self.assertEqual(self.api.in_flight_writes, 0)

    async def test_halt_lock_precedes_real_cancel_transport(self):
        from pendle.maker_risk import HaltController
        trace = []
        class Files:
            def create_halt(self, path, payload):
                trace.append('write-fsync')
        async def stop(reason):
            trace.append('stop')
            return await self.manager.stop(reason)
        async def alert(reason):
            trace.append('alert')
        halt = HaltController('fake-halt', gate=self.gate, stop=stop, alert=alert,
                              files=Files(), clock=self.clock)
        result = await self.clock.drive(halt.trigger('集成熔断'))
        self.assertTrue(result.persisted)
        self.assertTrue(result.stopped)
        self.assertEqual(trace, ['write-fsync', 'stop', 'alert'])
        self.assertEqual([req.method for req in self.sent], ['DELETE', 'GET'])
        self.assertEqual(result.exit_code, 76)


class FinalRaceTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = OrdersTests.asyncSetUp
    intent = OrdersTests.intent

    async def test_auth_collision_cannot_override_auth_halt(self):
        from pendle.ondo_api import ApiError
        async def fail(body):
            raise ApiError(401, 'clientOrderID_collision')
        self.api.place = fail
        order = self.intent()
        self.api.rows[order.client_order_id] = row(order.client_order_id)
        await self.manager.submit(order)
        self.assertTrue(self.halts)
        self.assertFalse(any(call[0] == 'query' for call in self.api.calls))

    async def test_stop_fatal_error_enters_halt_without_recursive_wait(self):
        from pendle.ondo_api import ApiError, SendGate
        from pendle.maker_risk import HaltController
        trace = []
        self.api.gate = SendGate()
        original = self.api.cancel_all
        attempts = []
        async def cancel(market, *, stop=False):
            attempts.append(1)
            trace.append('cancel')
            if len(attempts) == 1:
                raise ApiError(401)
            return await original(market, stop=stop)
        self.api.cancel_all = cancel
        class Files:
            def create_halt(self, path, payload):
                trace.append('write-fsync')
        async def alert(reason):
            trace.append('alert')
        halt = HaltController('fake', gate=self.api.gate, stop=self.manager.stop,
                              alert=alert, files=Files(), clock=self.clock)
        self.manager.halt = halt.trigger
        self.assertFalse(await self.manager.stop('健康守卫停止中鉴权失败'))
        for _ in range(70):
            await asyncio.sleep(0)
        self.assertIsNotNone(halt._task)
        self.assertTrue(halt._task.done())
        self.assertEqual(trace[:3], ['cancel', 'write-fsync', 'cancel'])
        self.assertEqual(halt.exit_code, 76)

    async def test_balance_same_time_rest_cannot_overwrite_private_balance(self):
        self.state.apply_balance_snapshot({'equity': '100', 'available': '10', 'time_ms': 100}, source='ws')
        self.state.apply_balance_snapshot({'equity': '100', 'available': '100', 'time_ms': 100})
        self.assertEqual(self.state.balances['available'], '10')
        self.assertEqual(self.state.market_mode('IBM-USD.P'), 'SYNCING')

    async def test_timeout_heartbeat_escalates_overdue_cancel(self):
        order = self.intent()
        await self.manager.submit(order)
        await self.manager.cancel(order)
        self.clock.now = 20
        await self.manager.check_timeouts()
        self.assertTrue(self.halts)

    async def test_async_risk_callback_never_bypasses_risk(self):
        async def risk(order, state):
            raise AssertionError('风险检查不能异步运行')
        self.manager.risk_check = risk
        order = self.intent()
        await self.manager.submit(order)
        self.assertEqual(order.status, OrderStatus.REJECTED)
        self.assertFalse(self.api.calls)

    async def test_mutation_while_queued_cannot_change_checked_intent(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def place(body, *, before_send=None):
            entered.set()
            await release.wait()
            before_send()
            self.api.calls.append(('unsafe', body))
            return row(body['clientOrderId'])
        self.api.place_order = place
        order = self.intent()
        send = asyncio.create_task(self.manager.submit(order))
        await entered.wait()
        order.quantity = D('0.1')
        release.set()
        await send
        self.assertEqual(order.status, OrderStatus.REJECTED)
        self.assertFalse(self.api.calls)

    async def test_nonfinite_timeout_and_boolean_attempts_are_rejected(self):
        for name, value in [('request_timeout_s', float('inf')),
                            ('cancel_confirm_timeout_s', float('nan')),
                            ('unknown_query_interval_s', float('nan')),
                            ('unknown_query_attempts', True)]:
            with self.subTest(name=name), self.assertRaises(OrderStateError):
                OrderManager(self.state, self.api, risk_check=lambda *args: None, **{name: value})

    async def test_closed_stop_cannot_turn_unknown_into_confirmed_terminal(self):
        from pendle.ondo_api import SendGate
        async def fail(body):
            raise TimeoutError
        self.api.place = fail
        order = self.intent()
        await self.manager.submit(order)
        self.api.gate = SendGate()
        self.api.gate.close()
        self.assertFalse(await self.manager.stop('死人开关封锁期间未知请求'))
        self.assertEqual(order.status, OrderStatus.UNKNOWN)
        self.assertFalse(any(call[0] == 'query' for call in self.api.calls))

    async def test_healthy_stop_disambiguates_unknown_with_client_queries(self):
        async def fail(body):
            raise TimeoutError
        self.api.place = fail
        order = self.intent()
        await self.manager.submit(order)
        self.assertTrue(await self.manager.stop('正常退出未知请求'))
        self.assertEqual(order.status, OrderStatus.ABSENT)
        self.assertGreaterEqual(sum(call[0] == 'query' for call in self.api.calls), 2)

    async def test_order_event_overfill_does_not_partially_mutate_order(self):
        order = self.intent()
        await self.manager.submit(order)
        self.state.apply_fill('newer', 'IBM-USD.P', 'buy', '1', 30,
                              client_order_id=order.client_order_id)
        before = dict(order.__dict__)
        with self.assertRaises(OrderStateError):
            self.state.apply_order_event(row(order.client_order_id, status='PARTIAL', filled='2', at=25))
        self.assertEqual(order.__dict__, before)

    async def test_stop_rechecks_private_revision_after_final_client_query(self):
        async def fail(body):
            raise TimeoutError
        self.api.place = fail
        order = self.intent()
        await self.manager.submit(order)
        queries = []
        async def query(cid):
            queries.append(cid)
            if len(queries) == 2:
                self.state.apply_order_event(row('late-legacy', at=100))
            return None
        self.api.query = query
        self.assertTrue(await self.manager.stop('最后查询期间迟到订单'))
        self.assertGreaterEqual(sum(call[0] == 'cancel_all' for call in self.api.calls), 2)

    async def test_order_fill_event_cannot_release_risk_before_fill_accounting(self):
        from pendle.maker_risk import MarketRisk, RiskLimits, RiskManager, RiskOrder
        risk = RiskManager(RiskLimits(quote_notional='800'))
        def check(candidate, state):
            orders = [RiskOrder(o.market, o.side, o.remaining, o.price, o.status)
                      for o in state.active_orders() if o is not candidate]
            return risk.check_order(candidate.market, candidate.side, candidate.quantity,
                candidate.price, [MarketRisk('IBM-USD.P', state.positions['IBM-USD.P'], '100')],
                orders, '1000', '900')
        self.manager.risk_check = check
        order = self.manager.create_intent('IBM-USD.P', 'buy', '8', '100')
        await self.manager.submit(order)
        await self.manager.handle_order_event(row(order.client_order_id, status='FILLED',
                                                   quantity='8', filled='8', at=20))
        self.assertEqual(self.state.positions['IBM-USD.P'], 0)
        with self.assertRaises(OrdersBlocked):
            self.manager.create_intent('IBM-USD.P', 'buy', '8', '100')

    async def test_old_position_snapshot_cannot_clear_unaccounted_cumulative_fill(self):
        order = self.intent()
        await self.manager.submit(order)
        await self.manager.handle_order_event(row(order.client_order_id, status='FILLED', filled='2', at=20))
        self.assertFalse(await self.manager.reconcile('IBM-USD.P'))
        async def fresh_position():
            return [{'market': 'IBM-USD.P', 'quantity': '2', 'time_ms': 30}]
        self.api.get_positions = fresh_position
        self.assertFalse(await self.manager.reconcile('IBM-USD.P'))
        self.assertTrue(await self.manager.reconcile('IBM-USD.P'))
        self.assertEqual(self.state.positions['IBM-USD.P'], 2)

    async def test_fatal_category_on_5xx_preserves_unknown_and_halts(self):
        from pendle.ondo_api import ApiError
        async def fail(body):
            raise ApiError(503, 'account_in_liquidation')
        self.api.place = fail
        order = self.intent()
        await self.manager.submit(order)
        self.assertEqual(order.status, OrderStatus.UNKNOWN)
        self.assertTrue(self.halts)
        self.assertTrue(self.state.stopping)


class UntimedPositionReconciliationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock, self.api = Clock(), API()
        self.state = AccountState(['IBM-USD.P'])
        self.alerts = []
        async def alert(message):
            self.alerts.append(message)
        self.manager = OrderManager(self.state, self.api, risk_check=lambda *args: None,
            monotonic=self.clock, sleep=self.clock.sleep, quiet_s=3, alert=alert)
        self.state.deadman_ack()
        self.quantity = '0'
        self.snapshot_times = []
        async def positions():
            self.snapshot_times.append(self.clock())
            return [{'market': 'IBM-USD.P', 'quantity': self.quantity,
                     'unrealized_pnl': '-4', 'mark_price': '100'}]
        async def balances():
            return {'equity': '1000', 'available': '900', 'margin_ratio': '.1',
                    'used_margin': '100', 'under_liquidation': False}
        self.api.get_positions, self.api.get_balances = positions, balances

    async def test_matching_snapshot_waits_for_quiet_and_recovers_without_timestamp(self):
        self.state.apply_fill('one', 'IBM-USD.P', 'buy', '2', 100)
        self.quantity = '2'
        self.assertTrue(await self.manager.reconcile('IBM-USD.P'))
        self.assertGreaterEqual(self.snapshot_times[0], 3)
        self.assertEqual(self.state.market_mode('IBM-USD.P'), 'RUNNING')
        self.assertEqual(self.state.balances['margin_ratio'], '.1')

    async def test_mismatch_rebases_then_requires_another_quiet_consistent_snapshot(self):
        self.quantity = '-2'
        self.assertFalse(await self.manager.reconcile('IBM-USD.P'))
        self.assertEqual(self.state.positions['IBM-USD.P'], -2)
        self.assertEqual(self.state.market_mode('IBM-USD.P'), 'SYNCING')
        first = self.clock()
        self.assertTrue(await self.manager.reconcile('IBM-USD.P'))
        self.assertGreaterEqual(self.snapshot_times[-1] - first, 3)
        self.assertTrue(self.alerts)

    async def test_changing_mismatch_does_not_count_as_two_consistent_snapshots(self):
        self.quantity = '1'
        self.assertFalse(await self.manager.reconcile('IBM-USD.P'))
        self.quantity = '2'
        self.assertFalse(await self.manager.reconcile('IBM-USD.P'))
        self.assertEqual(self.state.market_mode('IBM-USD.P'), 'SYNCING')
        self.assertTrue(await self.manager.reconcile('IBM-USD.P'))

    async def test_new_fill_during_snapshot_blocks_restore_and_restarts_quiet(self):
        async def positions():
            self.state.apply_fill('during', 'IBM-USD.P', 'buy', '1', 100)
            return [{'market': 'IBM-USD.P', 'quantity': '0'}]
        self.api.get_positions = positions
        self.assertFalse(await self.manager.reconcile('IBM-USD.P'))
        self.assertEqual(self.state.positions['IBM-USD.P'], 1)
        self.assertEqual(self.state.last_fill_monotonic['IBM-USD.P'], self.clock())
        self.assertEqual(self.state.market_mode('IBM-USD.P'), 'SYNCING')

    async def test_fill_after_untimed_snapshot_adds_exactly_once(self):
        self.quantity = '2'
        self.assertFalse(await self.manager.reconcile('IBM-USD.P'))
        self.assertTrue(await self.manager.reconcile('IBM-USD.P'))
        self.state.apply_fill('new', 'IBM-USD.P', 'sell', '.5', 100)
        self.state.apply_fill('new', 'IBM-USD.P', 'sell', '.5', 100)
        self.assertEqual(self.state.positions['IBM-USD.P'], D('1.5'))

    async def test_delayed_fill_already_covered_by_snapshot_is_not_counted_twice(self):
        self.assertTrue(await self.manager.reconcile('IBM-USD.P'))
        order = self.manager.create_intent('IBM-USD.P', 'buy', '2', '100')
        await self.manager.submit(order)
        await self.manager.handle_order_event(row(order.client_order_id, status='FILLED', filled='2', at=20))
        self.quantity = '2'
        self.assertFalse(await self.manager.reconcile('IBM-USD.P'))
        self.assertTrue(await self.manager.reconcile('IBM-USD.P'))
        self.state.apply_fill('delayed', 'IBM-USD.P', 'buy', '2', 20,
                              client_order_id=order.client_order_id)
        self.assertEqual(self.state.positions['IBM-USD.P'], 2)

    async def test_confirmed_quiet_position_details_include_final_floating_loss(self):
        self.assertTrue(await self.manager.reconcile('IBM-USD.P'))
        self.assertEqual(self.state.position_details['IBM-USD.P']['unrealized_pnl'], '-4')
        self.assertEqual(self.state.position_details['IBM-USD.P']['mark_price'], '100')

    async def test_untimed_position_missing_floating_pnl_cannot_restore(self):
        async def positions():
            return [{'market': 'IBM-USD.P', 'quantity': '0'}]
        self.api.get_positions = positions
        self.assertFalse(await self.manager.reconcile('IBM-USD.P'))
        self.assertEqual(self.state.market_mode('IBM-USD.P'), 'SYNCING')


class EmergencyHaltedOrderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await OrdersTests.asyncSetUp(self)
        from pendle.ondo_api import SendGate
        self.api.gate = SendGate()
        self.api.gate.halt_trading()
        self.state.positions['IBM-USD.P'] = D(2)
        self.state.stopping = True
        self.state.modes['IBM-USD.P'] = 'HALTED'
        self.emergency_sent = []
        async def place(body, *, before_send=None, emergency=False):
            before_send()
            self.emergency_sent.append((body, emergency))
            return row(body['clientOrderId'], side=body['side'], quantity=body['size'],
                       price=body['price'], status='CANCELED', at=30)
        self.api.place_order = place

    def emergency(self, **changes):
        params = dict(reduce_only=True, time_in_force='IOC', post_only=False, emergency=True)
        params.update(changes)
        return self.manager.create_intent('IBM-USD.P', 'sell', '1', '100', **params)

    async def test_emergency_ioc_passes_risk_and_preserves_halted_barrier(self):
        checked = []
        self.manager.risk_check = lambda order, state: checked.append(order.emergency)
        order = self.emergency()
        await self.manager.submit(order)
        self.assertEqual(order.status, OrderStatus.CANCELED)
        self.assertEqual(checked, [True])
        self.assertTrue(self.emergency_sent[0][1])
        self.assertTrue(self.state.stopping)
        self.assertEqual(self.state.market_mode('IBM-USD.P'), 'HALTED')
        with self.assertRaises(OrdersBlocked):
            self.manager.create_intent('IBM-USD.P', 'buy', '1', '100')

    async def test_emergency_requires_halted_mode_ack_no_other_orders_and_no_pending_fills(self):
        for field, value in (('stopping', False), ('deadman_acked', False)):
            original = getattr(self.state, field)
            setattr(self.state, field, value)
            with self.subTest(field=field), self.assertRaises(OrdersBlocked):
                self.emergency()
            setattr(self.state, field, original)
        self.state.modes['IBM-USD.P'] = 'SYNCING'
        with self.assertRaises(OrdersBlocked):
            self.emergency()
        self.state.modes['IBM-USD.P'] = 'HALTED'
        active = self.emergency()
        with self.assertRaises(OrdersBlocked):
            self.emergency()
        active.status = OrderStatus.FILLED
        active.event_filled = D(1)
        active.exchange_ms = 99
        with self.assertRaises(OrdersBlocked):
            self.emergency()

    async def test_emergency_rejects_unsafe_shape_before_creating_order(self):
        for changes in ({'reduce_only': False}, {'time_in_force': 'GTC'}, {'post_only': True}):
            with self.subTest(changes=changes), self.assertRaises((OrdersBlocked, OrderStateError)):
                self.emergency(**changes)
        self.assertFalse(self.state.orders)

    async def test_emergency_never_bypasses_full_closure_or_missing_trade_halt(self):
        from pendle.ondo_api import SendGate
        self.api.gate = SendGate()
        with self.assertRaises(OrdersBlocked):
            self.emergency()
        self.api.gate.halt_trading()
        self.api.gate.close()
        with self.assertRaises(OrdersBlocked):
            self.emergency()

    async def test_emergency_rechecks_ack_and_risk_when_queued(self):
        order = self.emergency()
        self.state.deadman_acked = False
        await self.manager.submit(order)
        self.assertEqual(order.status, OrderStatus.REJECTED)
        self.assertFalse(self.emergency_sent)
        self.state.deadman_acked = True
        self.manager.risk_check = lambda *args: False
        order = self.emergency()
        await self.manager.submit(order)
        self.assertEqual(order.status, OrderStatus.REJECTED)
        self.assertFalse(self.emergency_sent)

    async def test_monitor_reconciliation_can_confirm_without_reopening_halted_trading(self):
        async def positions():
            return [{'market': 'IBM-USD.P', 'quantity': '2', 'unrealized_pnl': '-5'}]
        self.api.get_positions = positions
        self.assertTrue(await self.manager.reconcile('IBM-USD.P', monitor_halted=True))
        self.assertTrue(self.state.stopping)
        self.assertEqual(self.state.market_mode('IBM-USD.P'), 'HALTED')
        self.assertTrue(self.api.gate.trading_halted)

    async def test_monitor_reconciliation_fails_after_full_closure(self):
        self.api.gate.close()
        self.assertFalse(await self.manager.reconcile('IBM-USD.P', monitor_halted=True))
        self.assertTrue(self.state.stopping)

    async def test_prepare_resume_never_clears_trading_halt_barrier(self):
        async def completed():
            return True
        self.manager._stop_task = asyncio.create_task(completed())
        await self.manager._stop_task
        with self.assertRaises(OrdersBlocked):
            self.manager.prepare_resume()
        self.assertTrue(self.state.stopping)
        self.assertEqual(self.state.market_mode('IBM-USD.P'), 'HALTED')


class EmergencyAPIIntegrationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = ModuleIntegrationTests.asyncSetUp

    async def test_halted_emergency_reaches_real_api_with_strict_ioc_body(self):
        self.gate.halt_trading()
        self.state.stopping = True
        self.state.modes['IBM-USD.P'] = 'HALTED'
        self.state.positions['IBM-USD.P'] = D(2)
        async def post(body):
            return self.HttpResponse(200, {'success': True, 'result': row(body['clientOrderId'],
                status='CANCELED', quantity=body['size'], side=body['side'],
                price=body['price'], at=30)})
        self.post_handler = post
        order = self.manager.create_intent('IBM-USD.P', 'sell', '1', '99',
            reduce_only=True, time_in_force='IOC', post_only=False, emergency=True)
        await self.clock.drive(self.manager.submit(order))
        self.assertEqual(order.status, OrderStatus.CANCELED)
        self.assertEqual([request.method for request in self.sent], ['POST'])
        import json
        body = json.loads(self.sent[0].body)
        self.assertTrue(body['reduceOnly'])
        self.assertEqual(body['timeInForce'], 'IOC')
        self.assertFalse(body['postOnly'])
        self.assertTrue(self.state.stopping)
        self.assertEqual(self.state.market_mode('IBM-USD.P'), 'HALTED')


class UnknownTimeoutConfigurationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = OrdersTests.asyncSetUp

    def build_manager(self, **changes):
        return OrderManager(self.state, self.api, risk_check=lambda *args: None,
            monotonic=self.clock, sleep=self.clock.sleep, halt=self.manager_halt, **changes)

    async def manager_halt(self, reason):
        self.halts.append(reason)

    async def unknown_order(self, manager):
        async def fail(body):
            raise TimeoutError
        self.api.place = fail
        order = manager.create_intent('IBM-USD.P', 'buy', '1', '100')
        await manager.submit(order)
        return order

    async def test_five_second_unknown_deadline_never_restarts_on_resolution_retry(self):
        manager = self.build_manager(unknown_timeout_s=5)
        order = await self.unknown_order(manager)
        started = order.unknown_since
        async def failed_query(cid):
            raise OSError
        self.api.query = failed_query
        self.assertFalse(await manager.resolve_unknown(order))
        self.assertFalse(self.halts)
        self.api.query = None
        self.clock.now = started + 4
        self.assertFalse(await manager.resolve_unknown(order))
        self.assertEqual(order.unknown_since, started)
        self.assertEqual(self.clock() - started, 5)
        self.assertTrue(self.halts)
        self.assertEqual(order.status, OrderStatus.UNKNOWN)

    async def test_timeout_heartbeat_uses_configured_five_second_boundary(self):
        manager = self.build_manager(unknown_timeout_s=5)
        order = await self.unknown_order(manager)
        self.clock.now = order.unknown_since + 4
        await manager.check_timeouts()
        self.assertFalse(self.halts)
        self.clock.now += 1
        await manager.check_timeouts()
        self.assertTrue(self.halts)

    async def test_unknown_timeout_must_be_finite_positive_and_at_most_sixty(self):
        for timeout in (0, -1, 60.001, float('nan'), float('inf'), True):
            with self.subTest(timeout=timeout), self.assertRaises(OrderStateError):
                self.build_manager(unknown_timeout_s=timeout)
        self.assertEqual(self.build_manager(unknown_timeout_s=60).unknown_timeout_s, 60)


class M3DrainTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = OrdersTests.asyncSetUp

    async def test_drain_without_local_orders_cancels_and_verifies_every_market(self):
        markets = ['IBM-USD.P', 'AAPL-USD.P']
        state = AccountState(markets)
        state.stopping = True
        manager = OrderManager(state, self.api, risk_check=lambda *args: None,
            monotonic=self.clock, sleep=self.clock.sleep)

        self.assertTrue(await manager.drain())
        self.assertEqual(self.api.calls, [
            ('cancel_all', markets[0], True), ('open', markets[0], True),
            ('cancel_all', markets[1], True), ('open', markets[1], True)])

    async def test_drain_rejects_remote_orders_after_three_empty_local_retries(self):
        self.state.stopping = True
        self.api.open_rows = [row('remote-only')]

        self.assertFalse(await self.manager.drain())
        self.assertEqual(sum(call[0] == 'cancel_all' for call in self.api.calls), 3)
        self.assertEqual(sum(call[0] == 'open' for call in self.api.calls), 3)
        self.assertFalse(self.state.orders)

    async def test_drain_rejects_unverified_zero_after_gate_closes(self):
        from pendle.ondo_api import SendGate
        self.state.stopping = True
        self.api.gate = SendGate()
        self.api.gate.close()

        self.assertFalse(await self.manager.drain())
        self.assertFalse(self.api.calls)

    async def test_stop_failure_callback_runs_after_three_attempts_and_finished_stop_task(self):
        failures = []
        async def stop_failed(reason):
            self.assertTrue(manager._stop_task.done())
            failures.append(reason)
        manager = OrderManager(self.state, self.api, risk_check=lambda *args: None,
            monotonic=self.clock, sleep=self.clock.sleep, stop_failed=stop_failed)
        self.api.open_rows = [row('remote-only')]

        self.assertFalse(await manager.stop('三轮远端核零失败'))
        self.assertEqual(failures, ['三轮远端核零失败'])
        self.assertEqual(sum(call[0] == 'cancel_all' for call in self.api.calls), 3)
        self.assertEqual(sum(call[0] == 'open' for call in self.api.calls), 3)

    async def test_drain_failure_notifies_stop_failure_callback(self):
        failures = []
        async def stop_failed(reason):
            failures.append(reason)
        manager = OrderManager(self.state, self.api, risk_check=lambda *args: None,
            monotonic=self.clock, sleep=self.clock.sleep, stop_failed=stop_failed)
        self.state.stopping = True
        self.api.open_rows = [row('remote-only')]

        self.assertFalse(await manager.drain())
        self.assertEqual(len(failures), 1)


class M3StopCancellationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = OrdersTests.asyncSetUp

    async def test_repeated_cancellation_still_finishes_failed_stop_and_failure_callback(self):
        entered, release = asyncio.Event(), asyncio.Event()
        notifying, finish_notification = asyncio.Event(), asyncio.Event()
        original_cancel = self.api.cancel_all
        failures = []
        async def cancel(market, *, stop=False):
            if not entered.is_set():
                entered.set()
                await release.wait()
            return await original_cancel(market, stop=stop)
        async def stop_failed(reason):
            self.assertTrue(manager._stop_task.done())
            notifying.set()
            await finish_notification.wait()
            failures.append(reason)
        self.api.cancel_all = cancel
        self.api.open_rows = [row('remote-only')]
        manager = OrderManager(self.state, self.api, risk_check=lambda *args: None,
            sleep=asyncio.sleep, stop_failed=stop_failed, request_timeout_s=1)
        stopping = asyncio.create_task(manager.stop('取消期间远端核零失败'))
        try:
            await entered.wait()
            stopping.cancel()
            await asyncio.sleep(0)
            self.assertFalse(stopping.done(), '必须等停止清理和失败回调后再传播取消')
            stopping.cancel()
            release.set()
            await asyncio.wait_for(notifying.wait(), 1)
            stopping.cancel()
            await asyncio.sleep(0)
            self.assertFalse(stopping.done(), '重复取消不能中断紧急熔断回调')
            finish_notification.set()
            with self.assertRaises(asyncio.CancelledError):
                await stopping
            self.assertEqual(failures, ['取消期间远端核零失败'])
            self.assertEqual(sum(call[0] == 'cancel_all' for call in self.api.calls), 3)
            self.assertEqual(sum(call[0] == 'open' for call in self.api.calls), 3)
        finally:
            release.set()
            finish_notification.set()
            await asyncio.gather(stopping, manager._stop_task, return_exceptions=True)

    async def test_cancelled_successful_stop_finishes_cleanup_then_preserves_cancellation(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original_cancel = self.api.cancel_all
        failures = []
        async def cancel(market, *, stop=False):
            entered.set()
            await release.wait()
            return await original_cancel(market, stop=stop)
        async def stop_failed(reason):
            failures.append(reason)
        self.api.cancel_all = cancel
        manager = OrderManager(self.state, self.api, risk_check=lambda *args: None,
            sleep=asyncio.sleep, stop_failed=stop_failed, request_timeout_s=1)
        stopping = asyncio.create_task(manager.stop('取消期间成功核零'))
        try:
            await entered.wait()
            stopping.cancel()
            await asyncio.sleep(0)
            self.assertFalse(stopping.done(), '成功清理也必须等远端核零后传播取消')
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await stopping
            self.assertTrue(manager._stop_task.result())
            self.assertEqual(self.api.calls, [
                ('cancel_all', 'IBM-USD.P', True), ('open', 'IBM-USD.P', True)])
            self.assertFalse(failures)
        finally:
            release.set()
            await asyncio.gather(stopping, manager._stop_task, return_exceptions=True)
