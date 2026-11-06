"""M24: regressions using real private order samples through the builder, signing queue, and verification of zero open orders."""

from copy import deepcopy
from decimal import Decimal as D
import json
from pathlib import Path
import unittest

from pendle.maker_orders import OrderStatus
from pendle.ondo_api import HttpResponse
import test_m3_wiring as wiring
from test_maker import MARKET, book


class M24FlowTests(unittest.IsolatedAsyncioTestCase):
    wall = wiring.M3WiringTests.wall
    drive = wiring.M3WiringTests.drive
    ready = wiring.M3WiringTests.ready
    asyncTearDown = wiring.M3WiringTests.asyncTearDown

    async def asyncSetUp(self):
        await wiring.M3WiringTests.asyncSetUp(self)
        fixture = Path(__file__).parent / 'fixtures/live_samples/ws_private_orders.json'
        self.samples = json.loads(fixture.read_text())['orders_updates']
        self.rest_fills, self.last_order = [], None
        original_transport = self.bot.api.transport

        async def network(request):
            path = request.path_query.partition('?')[0]
            if request.method == 'POST' and path == '/v1/perps/orders':
                self.requests.append(request)
                intent = json.loads(request.body)
                row = deepcopy(self.samples[0]['data'][0])
                row['clientOrderId'] = intent['clientOrderId']
                self.remote[row['clientOrderId']] = row
                self.last_order = deepcopy(row)
                result = row
                self.publish()
            elif request.method == 'DELETE' and path.startswith('/v1/perps/orders/client:'):
                self.requests.append(request)
                self.files.events.append('撤单')
                row = deepcopy(self.samples[1]['data'][0])
                row['clientOrderId'] = self.last_order['clientOrderId']
                self.remote.pop(row['clientOrderId'], None)
                self.last_order = row
                result = {}
                self.publish()
            elif request.method == 'GET' and path.startswith('/v1/perps/orders/client:'):
                self.requests.append(request)
                result = deepcopy(self.last_order)
            elif request.method == 'GET' and path == '/v1/perps/fills':
                self.requests.append(request)
                result = deepcopy(self.rest_fills)
            else:
                return await original_transport(request)
            return HttpResponse(200, {'success': True, 'result': result})

        self.bot.api.transport = network

    def publish(self, *, mark=True):
        bids, asks = [('251.75', '1'), ('250', '10')], [('252.25', '1'), ('254', '10')]
        for row in self.remote.values():
            (bids if row['side'] == 'buy' else asks).append((row['price'], row['size']))
        self.bot.book = book(bids=bids, asks=asks, at=int(self.wall() * 1000))
        self.bot.book_seen = self.clock.monotonic()
        if mark:
            self.bot.engine.mark = (D(252), self.clock.monotonic())
            self.bot.engine.in_price_discovery = False
            self.bot.engine.price_discovery_seen = self.clock.monotonic()

    def assert_not_halted(self):
        self.assertTrue(self.bot.gate.is_open)
        self.assertFalse(self.bot.gate.trading_halted)
        self.assertFalse(self.bot._stop_failed)
        self.assertNotEqual(self.bot.guard.state, 'STOP_FAILED')
        self.assertIsNone(self.bot.halt.exit_code)
        self.assertNotIn(str(self.settings.halt_path), self.files.rows)
        self.assertNotIn('fsync锁', self.files.events)

    def documented_fill(self):
        return {'id': 'm24-fill', 'market': MARKET, 'orderId': 'm24-exchange-order',
                'clientOrderId': 'm24-client-order', 'price': '251.62', 'size': '0.07',
                'side': 'buy', 'fee': '0.01', 'pnl': '0',
                'time': '2026-11-04T11:07:56.045756519Z', 'isMaker': True}

    async def test_real_orders_open_cancel_zero_with_signed_rest_transport(self):
        await self.ready()
        order = self.bot.manager.create_intent(MARKET, 'buy', '0.07', '251.62')
        await self.drive(self.bot.manager.submit(order))
        self.assertEqual(order.status, OrderStatus.OPEN)
        self.assertEqual(order.exchange_ms, 1793790476045)
        self.assertFalse(order.reduce_only)
        self.assertNotIn('time_ms', self.last_order)
        self.assertNotIn('reduceOnly', self.last_order)

        opened, canceled = deepcopy(self.samples)
        for message in (opened, canceled):
            message['data'][0]['clientOrderId'] = order.client_order_id
        await self.bot.on_private(opened)
        self.assertEqual(order.status, OrderStatus.OPEN)
        self.assertEqual(self.bot.state.market_mode(MARKET), 'RUNNING')
        self.assert_not_halted()

        self.assertFalse(await self.drive(self.bot.manager.cancel(order)))
        self.assertEqual(order.status, OrderStatus.CANCEL_SENT)
        self.assertFalse(self.remote)
        await self.bot.on_private(canceled)
        self.assertEqual(order.status, OrderStatus.CANCELED)
        self.assertEqual(order.exchange_ms, 1793790477033)
        self.assertTrue(await self.drive(self.bot.manager.confirm_cancel(order)))
        self.assertTrue(await self.drive(self.bot.stop_orders('M24 真实样本撤单核零')))
        self.assertFalse(self.bot.state.active_orders())
        self.assertFalse(self.remote)
        self.assert_not_halted()

        writes = [request for request in self.requests if request.method in ('POST', 'DELETE')]
        self.assertEqual(sum(request.method == 'POST' for request in writes), 1)
        self.assertTrue(any(request.method == 'DELETE' and '/client:' in request.path_query
                            for request in writes))
        self.assertTrue(all(request.headers.get('ONDO-SIGN') for request in writes))

    async def test_rest_documented_fill_fields_use_time_without_time_ms(self):
        self.rest_fills = [self.documented_fill()]
        del self.rest_fills[0]['market']
        fills = await self.drive(self.bot.api.get_fills(MARKET))
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0]['fill_id'], 'm24-fill')
        self.assertEqual(fills[0]['time_ms'], 1793790476045)
        self.assertEqual(fills[0]['quantity'], '0.07')
        self.assertTrue(fills[0]['is_maker'])
        self.assertNotIn('time_ms', self.rest_fills[0])
        self.assert_not_halted()

    async def test_ws_documented_fill_fields_use_time_before_time_ms(self):
        await self.ready()
        row = self.documented_fill()
        row['time_ms'] = 1
        await self.bot.on_private({'type': 'update', 'channel': 'fillsPerps', 'data': [row]})
        self.assertEqual(self.bot.state.fills['m24-fill'][1], 1793790476045)
        self.assertEqual(self.bot.last_fill_ms, 1793790476045)
        self.assertEqual(self.bot.state.positions[MARKET], D('0.07'))
        self.assert_not_halted()

    async def test_missing_ws_fill_required_fields_warn_sync_without_halt(self):
        await self.ready()
        # clientOrderId is optional: fills of orders placed outside the bot (web app) do not carry it.
        for field in (field for field in self.documented_fill() if field != 'clientOrderId'):
            with self.subTest(field=field):
                self.bot.state.modes[MARKET] = 'RUNNING'
                self.bot.reconcile_event.clear()
                row = self.documented_fill()
                del row[field]
                with self.assertLogs(self.bot.logger, level='WARNING') as captured:
                    await self.bot.on_private({'type': 'update', 'channel': 'fillsPerps',
                                               'data': [row]})
                self.assertTrue(any('fillsPerps' in line for line in captured.output))
                self.assertEqual(self.bot.state.market_mode(MARKET), 'SYNCING')
                self.assertTrue(self.bot.reconcile_event.is_set())
                self.assertFalse(self.bot.state.fills)
                self.assert_not_halted()


    async def test_web_app_fill_without_client_order_id_is_booked_and_reconcile_succeeds(self):
        """Production 2026-11-05: a manual web-app close broke every REST reconcile until the watchdog exited."""
        from pendle.ondo_api import normalize_fill
        row = {'id': '32146c2acd02f1549c9123e5def51791', 'orderId': '5UTVTPI73IUIFZVASTJ4RRC5JO2APLF5',
               'market': MARKET, 'price': '255.01', 'size': '2.15', 'side': 'sell', 'direction': 'closeLong',
               'filledCost': '548.2715', 'fee': '0.137068', 'feeRebate': '0.006853', 'pnl': '0.828844',
               'time': '2026-11-05T08:17:28.509096489Z', 'isMaker': False}
        fill = normalize_fill(row, MARKET)
        self.assertIsNone(fill['client_order_id'])
        self.assertEqual(fill['fill_id'], row['id'])
        with self.assertRaises(Exception):
            normalize_fill({**row, 'clientOrderId': 7}, MARKET)
        await self.ready()
        await self.bot.on_private({'type': 'update', 'channel': 'fillsPerps', 'data': [row]})
        self.assertIn(row['id'], self.bot.state.fills)
        self.assert_not_halted()


if __name__ == '__main__':
    unittest.main()
