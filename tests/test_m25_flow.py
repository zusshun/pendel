"""M25: replay a real maker fill through the signed Maker assembly without network access."""

from copy import deepcopy
from dataclasses import replace
from decimal import Decimal as D
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from pendle.maker_orders import OrderStatus
from pendle.ondo_api import HttpResponse, exchange_time_ms
import test_m3_wiring as wiring
from test_maker import MARKET, book


class M25FillReplayTests(unittest.IsolatedAsyncioTestCase):
    wall = wiring.M3WiringTests.wall
    drive = wiring.M3WiringTests.drive
    ready = wiring.M3WiringTests.ready
    asyncTearDown = wiring.M3WiringTests.asyncTearDown

    async def asyncSetUp(self):
        await wiring.M3WiringTests.asyncSetUp(self)
        fixture = Path(__file__).parent / 'fixtures/live_samples/ws_private_fill_sequence.json'
        self.samples = json.loads(fixture.read_text())
        self.sequence = self.samples['sequence_around_fill']
        self.epoch.epoch = exchange_time_ms(self.sequence[0]['data'][0]['createdAt']) / 1000 + 1
        self.bot.settings = replace(self.settings, window_start_day=2,
            window_start_time='00:00', window_end_day=3, window_end_time='23:59')
        self.bot.engine.settings = self.bot.settings
        self.bot.rules = replace(self.bot.rules, max_position_base_size=D('1.6'))
        self.bot.manager.run_id = 'maker-2ddcebc113f9454d'
        self.position_row, self.rest_fills = None, []
        self.replay_tail, self.valuation_pushes = False, 0
        original = self.bot.api.transport

        async def transport(request):
            path = request.path_query.partition('?')[0]
            if request.method == 'POST' and path.endswith('/orders'):
                intent = json.loads(request.body)
                template = self.sequence[4 if intent['side'] == 'buy' else 0]['data'][0]
                if intent['clientOrderId'] == template['clientOrderId']:
                    self.requests.append(request)
                    row = {**intent, 'orderId': template['orderId'], 'status': 'open',
                           'filledSize': '0', 'createdAt': template['createdAt']}
                    self.remote[row['clientOrderId']] = row
                    self.publish()
                    return HttpResponse(200, {'success': True, 'result': row})
            if request.method == 'DELETE' and self.replay_tail:
                self.requests.append(request)
                self.files.events.append('撤单')
                self.remote.clear()
                self.replay_tail = False
                for message in self.sequence[4:]:
                    await self.bot.on_private(deepcopy(message))
                    if message['channel'] == 'positionsPerps':
                        self.position_row = deepcopy(message['data'][0])
                self.publish()
                return HttpResponse(200, {'success': True, 'result': {}})
            if request.method == 'GET' and path.endswith('/positions') and self.position_row:
                self.requests.append(request)
                response = HttpResponse(200, {'success': True, 'result': [deepcopy(self.position_row)]})
            elif request.method == 'GET' and path.endswith('/fills'):
                self.requests.append(request)
                response = HttpResponse(200, {'success': True, 'result': deepcopy(self.rest_fills)})
            else:
                response = await original(request)
            # Valuation updates race every zero-check and reconciliation read, as in production.
            if request.method == 'GET' and self.position_row:
                await self.push_valuation()
            return response

        self.bot.api.transport = transport
        self.socket_patch = patch('socket.socket.connect', side_effect=AssertionError('回放禁止联网'))
        self.socket_patch.start()
        self.addCleanup(self.socket_patch.stop)

    def publish(self, *, mark=True):
        bids, asks = [('252.7', '1'), ('250', '10')], [('252.9', '1'), ('255', '10')]
        for row in self.remote.values():
            (bids if row['side'] == 'buy' else asks).append((row['price'], row['size']))
        self.bot.book = book(bids=bids, asks=asks, at=int(self.wall() * 1000))
        self.bot.book_seen = self.clock.monotonic()
        if mark:
            self.bot.engine.mark = (D('252.8'), self.clock.monotonic())
            self.bot.engine.in_price_discovery = False
            self.bot.engine.price_discovery_seen = self.clock.monotonic()

    async def push_valuation(self):
        self.valuation_pushes += 1
        self.clock.advance(.08)
        message = deepcopy(self.sequence[1])
        row = message['data'][0]
        row['markPrice'] = str(D(row['markPrice']) + D('.001') * self.valuation_pushes)
        row['unrealizedPnl'] = str(D(row['unrealizedPnl']) - D('.00007') * self.valuation_pushes)
        row['lastUpdate'] = int(self.wall() * 1000)
        await self.bot.on_private(message)

    def assert_not_halted(self):
        self.assertTrue(self.bot.gate.is_open)
        self.assertFalse(self.bot.gate.trading_halted)
        self.assertFalse(self.bot._stop_failed)
        self.assertNotEqual(self.bot.guard.state, 'STOP_FAILED')
        self.assertIsNone(self.bot.halt.exit_code)
        self.assertNotIn(str(self.settings.halt_path), self.files.rows)
        self.assertFalse(any('STOP_FAILED' in text for text in wiring.M3WiringTests.messages(self)))

    async def test_real_maker_sell_fill_cancels_residual_and_resumes_with_short_inventory(self):
        await self.ready()
        orders = {}
        for side, price in (('buy', '252.42'), ('sell', '253.19')):
            order = self.bot.manager.create_intent(MARKET, side, '0.07', price)
            await self.drive(self.bot.manager.submit(order))
            self.assertEqual(order.status, OrderStatus.OPEN, order.last_error_reason)
            orders[side] = order

        self.clock.advance(max(0, exchange_time_ms(self.sequence[0]['timestamp']) / 1000 - self.wall()))
        self.remote.pop(orders['sell'].client_order_id)
        self.rest_fills = deepcopy(self.sequence[2]['data'])
        for message in self.sequence[:4]:
            await self.bot.on_private(deepcopy(message))
            if message['channel'] == 'positionsPerps':
                self.position_row = deepcopy(message['data'][0])
        self.replay_tail = True
        self.publish()
        await self.drive(self.bot.tick())

        self.assertEqual(self.bot.last_stop_reason, '任一侧终结，先撤销残留单边')
        self.assertEqual(orders['sell'].status, OrderStatus.FILLED)
        self.assertEqual(orders['buy'].status, OrderStatus.CANCELED)
        self.assertFalse(self.remote)
        self.assertFalse(self.bot.state.active_orders())
        self.assertEqual(self.bot.state.positions[MARKET], D('-.07'))
        self.assert_not_halted()
        self.assertTrue(self.bot.state.stopping)
        self.assertTrue(self.bot.manager._stop_task.result())

        self.publish()
        await self.drive(self.bot.tick())
        self.assert_not_halted()
        self.assertFalse(self.bot.state.stopping)
        self.assertEqual(self.bot.state.market_mode(MARKET), 'RUNNING')
        self.assertEqual(self.bot.state.positions[MARKET], D('-.07'))
        self.assertFalse(self.bot.state.has_unaccounted_fills(MARKET))
        self.assertEqual(len(self.bot.state.fills), 1)
        self.assertTrue(self.bot.qualified)
        resumed = {order.side: order for order in self.bot.state.active_orders(MARKET)}
        self.assertEqual(set(resumed), {'buy', 'sell'})
        self.assertLessEqual(resumed['sell'].quantity, D('1.6') - D('.07'))
        self.assertGreater(resumed['buy'].quantity, resumed['sell'].quantity)
        self.assertTrue(all(order.post_only and order.status == OrderStatus.OPEN
                            for order in resumed.values()))
        self.assertGreaterEqual(self.valuation_pushes, 3)
        deletes = [index for index, request in enumerate(self.requests) if request.method == 'DELETE']
        posts = [index for index, request in enumerate(self.requests) if request.method == 'POST']
        positions = [index for index, request in enumerate(self.requests)
                     if request.method == 'GET' and '/positions' in request.path_query]
        # The real canceled event invalidates the first zero-check; valuation pushes must not invalidate its retry.
        self.assertEqual(len(deletes), 2)
        self.assertEqual(len(posts), 4)
        self.assertTrue(any(deletes[0] < index < posts[2] for index in positions))


if __name__ == '__main__':
    unittest.main()
