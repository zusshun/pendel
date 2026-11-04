"""M24：以真实订单字段验证时间排序、可选标识与 REST 对账。"""

import json
import unittest
from copy import deepcopy
from decimal import Decimal as D
from pathlib import Path

from pendle.maker_orders import AccountState, Order, OrderManager, OrderStatus
from pendle.ondo_api import Credentials, HttpResponse, OndoAPI
from test_maker import MARKET


SAMPLES = json.loads((Path(__file__).parent / 'fixtures/live_samples/ws_private_orders.json').read_text())
OPEN_MS, CANCELED_MS = 1793790476045, 1793790477033


def sample_row(index=0, **changes):
    row = deepcopy(SAMPLES['orders_updates'][index]['data'][0])
    row.update(changes)
    return row


class M24OrderEventsTests(unittest.TestCase):
    def setUp(self):
        self.state = AccountState([MARKET])
        row = sample_row()
        self.order = Order(row['clientOrderId'], MARKET, 'buy', D('.07'), D('251.62'))
        self.state.orders[self.order.client_order_id] = self.order
        self.state.modes[MARKET] = 'RUNNING'

    def test_live_open_then_canceled_without_time_ms_or_reduce_only(self):
        for index, status, at in ((0, OrderStatus.OPEN, OPEN_MS),
                                  (1, OrderStatus.CANCELED, CANCELED_MS)):
            self.assertIs(self.state.apply_order_event(sample_row(index)), self.order)
            self.assertEqual((self.order.status, self.order.exchange_ms), (status, at))
            self.assertFalse(self.order.reduce_only)
        self.assertEqual(self.state.market_mode(MARKET), 'RUNNING')

    def test_live_canceled_then_open_keeps_terminal(self):
        self.state.apply_order_event(sample_row(1))
        self.state.apply_order_event(sample_row())
        self.assertEqual((self.order.status, self.order.exchange_ms),
                         (OrderStatus.CANCELED, CANCELED_MS))

    def test_latest_created_filled_canceled_time_is_used_for_rest(self):
        for field in ('createdAt', 'filledAt', 'canceledAt'):
            with self.subTest(field=field):
                row = sample_row(**{field: '2026-11-04T11:07:58.000999999Z'})
                self.state.apply_order_event(row, source='rest')
                self.assertEqual(self.order.exchange_ms, 1793790478000)

    def test_ws_envelope_timestamp_participates_in_maximum(self):
        self.state.apply_order_event(sample_row(), timestamp='2026-11-04T11:07:59.123456789Z')
        self.assertEqual(self.order.exchange_ms, 1793790479123)
        self.state.apply_order_event(sample_row(1), timestamp=SAMPLES['orders_updates'][0]['timestamp'])
        self.assertEqual(self.order.exchange_ms, 1793790479123)

    def test_documented_time_overrides_unsupported_updated_at_and_time_ms(self):
        self.state.apply_order_event(sample_row(time_ms=10**20,
            updatedAt='2099-01-01T00:00:00Z'))
        self.assertEqual(self.order.exchange_ms, OPEN_MS)

    def test_rest_and_ws_compare_exchange_time_in_both_directions(self):
        for first_source, second_source in (('ws', 'rest'), ('rest', 'ws')):
            with self.subTest(first_source=first_source):
                self.setUp()
                self.state.apply_order_event(sample_row(), source=first_source)
                self.state.apply_order_event(sample_row(1), source=second_source)
                self.state.apply_order_event(sample_row(), source=first_source)
                self.assertEqual((self.order.status, self.order.exchange_ms, self.order.source),
                                 (OrderStatus.CANCELED, CANCELED_MS, second_source))

    def test_equal_time_terminal_has_priority_over_open_for_rest_and_ws(self):
        for terminal in ('canceled', 'fullyfilled'):
            for sources in (('ws', 'rest'), ('rest', 'ws')):
                for reverse in (False, True):
                    with self.subTest(terminal=terminal, sources=sources, reverse=reverse):
                        self.setUp()
                        rows = [sample_row(), sample_row(status=terminal,
                            filledSize='.07' if terminal == 'fullyfilled' else '0')]
                        if reverse:
                            rows.reverse()
                        for row, source in zip(rows, sources):
                            self.state.apply_order_event(row, source=source)
                        self.assertEqual(self.order.status, OrderStatus.FILLED
                                         if terminal == 'fullyfilled' else OrderStatus.CANCELED)

    def test_equal_time_larger_cumulative_fill_wins_regardless_of_source(self):
        for sources in (('ws', 'rest'), ('rest', 'ws')):
            with self.subTest(sources=sources):
                self.setUp()
                for filled, source in (('.01', sources[0]), ('.03', sources[1]), ('.02', sources[0])):
                    self.state.apply_order_event(sample_row(filledSize=filled), source=source)
                self.assertEqual(self.order.event_filled, D('.03'))

    def test_equal_time_lower_terminal_fill_cannot_replace_higher_fill(self):
        self.state.apply_order_event(sample_row(status='canceled', filledSize='.03'))
        self.state.apply_order_event(sample_row(status='canceled', filledSize='.02'), source='rest')
        self.assertEqual((self.order.status, self.order.event_filled),
                         (OrderStatus.CANCELED, D('.03')))

    def test_equal_time_terminal_and_larger_fill_are_preserved_in_either_order(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                self.setUp()
                rows = [sample_row(filledSize='.03'),
                        sample_row(status='canceled', filledSize='.02')]
                if reverse:
                    rows.reverse()
                for row in rows:
                    self.state.apply_order_event(row)
                self.assertEqual((self.order.status, self.order.event_filled),
                                 (OrderStatus.CANCELED, D('.03')))

    def test_missing_client_id_matches_existing_exchange_order_id(self):
        self.state.apply_order_event(sample_row())
        row = sample_row(1)
        del row['clientOrderId']
        self.assertIs(self.state.apply_order_event(row), self.order)
        self.assertEqual(self.order.status, OrderStatus.CANCELED)
        self.assertEqual(len(self.state.orders), 1)

    def test_missing_client_id_without_match_is_a_legacy_order(self):
        row = sample_row()
        del row['clientOrderId']
        order = self.state.apply_order_event(row)
        self.assertTrue(order.legacy)
        self.assertEqual(order.order_id, row['orderId'])
        self.assertFalse(order.reduce_only)
        self.assertEqual(self.state.market_mode(MARKET), 'SYNCING')

    def test_legacy_order_still_matches_when_later_event_includes_client_id(self):
        state = AccountState([MARKET])
        row = sample_row()
        del row['clientOrderId']
        order = state.apply_order_event(row)
        self.assertIs(state.apply_order_event(sample_row(1)), order)
        self.assertEqual(order.status, OrderStatus.CANCELED)
        self.assertEqual(len(state.orders), 1)

    def test_explicit_reduce_only_is_preserved_on_legacy_order(self):
        order = self.state.apply_order_event(sample_row(clientOrderId='other', reduceOnly=True))
        self.assertTrue(order.reduce_only)


class M24RestOrderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.open_rows = []
        self.single = None

        async def transport(request):
            self.requests.append(request)
            path = request.path_query.partition('?')[0]
            if request.method == 'DELETE':
                result = None
            elif path.endswith('/orders'):
                result = self.open_rows
            elif '/orders/' in path:
                result = self.single
            elif path.endswith('/positions'):
                result = []
            else:
                result = {'equity': '1000', 'available': '900'}
            return HttpResponse(200, {'success': True, 'result': result})

        self.api = OndoAPI(Credentials('测试-key', '测试-secret'), transport=transport,
                           rate=1_000_000)
        self.state = AccountState([MARKET])
        self.state.deadman_ack()
        self.manager = OrderManager(self.state, self.api, risk_check=lambda *args: None,
                                    quiet_s=.001)

    async def test_rest_list_and_single_order_use_real_lifecycle_times(self):
        self.open_rows, self.single = [sample_row()], sample_row(1)
        rows = await self.api.get_open_orders(MARKET)
        order = await self.manager.handle_order_event(rows[0], source='rest')
        found = await self.api.get_order(order.client_order_id)
        await self.manager.handle_order_event(found, source='rest')
        self.assertEqual((order.status, order.exchange_ms), (OrderStatus.CANCELED, CANCELED_MS))
        self.assertTrue(any('/orders/client:' in r.path_query for r in self.requests))

    async def test_reconcile_missing_client_id_matches_known_order_without_query(self):
        self.state.modes[MARKET] = 'RUNNING'
        order = self.manager.create_intent(MARKET, 'buy', '.07', '251.62')
        self.state.apply_order_event(sample_row(clientOrderId=order.client_order_id))
        self.open_rows = [sample_row()]
        del self.open_rows[0]['clientOrderId']
        self.assertTrue(await self.manager.reconcile(MARKET))
        self.assertEqual(len(self.state.orders), 1)
        self.assertIs(self.state.orders[order.client_order_id], order)
        self.assertFalse(any('/orders/' in r.path_query for r in self.requests))

    async def test_unknown_order_without_client_id_is_canceled_and_queried_by_order_id(self):
        row = sample_row()
        del row['clientOrderId']
        self.single = sample_row(1)
        del self.single['clientOrderId']
        order = await self.manager.handle_order_event(row)
        self.assertTrue(order.legacy)
        self.assertEqual(order.status, OrderStatus.CANCEL_SENT)
        self.assertTrue(await self.manager.confirm_cancel(order))
        path = '/v1/perps/orders/' + row['orderId']
        self.assertEqual([(r.method, r.path_query) for r in self.requests],
                         [('DELETE', path), ('GET', path)])


if __name__ == '__main__':
    unittest.main()
