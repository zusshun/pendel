"""M25 regressions for economic position changes and valuation-only private updates."""

import json
import unittest
from copy import deepcopy
from decimal import Decimal as D
from pathlib import Path

import test_m3_wiring as wiring
from pendle.maker_orders import AccountState
from pendle.ondo_api import HttpResponse, normalize_position
from test_maker import MARKET


SAMPLES = json.loads((Path(__file__).parent / 'fixtures/live_samples/'
                      'ws_private_fill_sequence.json').read_text())


class M25PositionFingerprintTests(unittest.TestCase):
    def setUp(self):
        self.state = AccountState([MARKET])
        self.position = normalize_position(SAMPLES['position_nonempty_example']['data'][0])
        self.state.apply_untimed_position_snapshot(MARKET, self.position['quantity'])
        self.state.position_details[MARKET] = dict(self.position)
        self.assertTrue(self.state.note_position_event(MARKET, self.position))

    def test_valuation_fields_do_not_change_position_revision(self):
        revision = self.state.market_revisions[MARKET]
        for field in ('markPrice', 'unrealizedPnl', 'notionalValue', 'usedMargin',
                      'maintenanceMargin', 'returnOnEquity', 'netFundingSinceNeutral',
                      'liquidationPrice', 'bankruptcyPrice', 'leverage', 'lastUpdate'):
            with self.subTest(field=field):
                raw = deepcopy(SAMPLES['position_nonempty_example']['data'][0])
                raw[field] = ('2026-11-04T12:21:08.004660268Z' if field == 'lastUpdate'
                              else str(D(raw[field]) + D('.01')))
                self.assertFalse(self.state.note_position_event(
                    MARKET, normalize_position(raw)))
                self.assertEqual(self.state.market_revisions[MARKET], revision)

    def test_rest_confirmed_valuation_difference_does_not_change_position_revision(self):
        confirmed = dict(SAMPLES['position_nonempty_example']['data'][0],
                         markPrice='251', unrealizedPnl='.15', usedMargin='1.7')
        self.state.apply_untimed_position_snapshot(MARKET, self.position['quantity'])
        self.state.position_details[MARKET] = normalize_position(confirmed)
        revision = self.state.market_revisions[MARKET]

        self.assertFalse(self.state.note_position_event(MARKET, self.position))
        self.assertEqual(self.state.market_revisions[MARKET], revision)

    def test_repeated_position_is_stable_before_fill_or_rest_catches_up(self):
        state = AccountState([MARKET])
        state.position_details[MARKET] = normalize_position(
            {'market': MARKET, 'direction': 'neutral', 'netQuantity': '0'})
        self.assertTrue(state.note_position_event(MARKET, self.position))
        revision = state.market_revisions[MARKET]

        for index in range(100):
            position = dict(self.position, markPrice=str(D(252) + D(index) / 1000))
            self.assertFalse(state.note_position_event(MARKET, position))
        self.assertEqual(state.market_revisions[MARKET], revision)

    def test_fill_invalidates_cached_quantity_only_when_inventory_diverges(self):
        self.state.apply_fill('new-sell', MARKET, 'sell', '.01', 100)
        revision = self.state.market_revisions[MARKET]

        self.assertTrue(self.state.note_position_event(MARKET, self.position))
        self.assertEqual(self.state.market_revisions[MARKET], revision + 1)
        self.assertFalse(self.state.note_position_event(MARKET, self.position))

    def test_numerically_equal_position_fields_do_not_change_revision(self):
        revision = self.state.market_revisions[MARKET]
        position = normalize_position(dict(SAMPLES['position_nonempty_example']['data'][0],
                                           netQuantity='0.0700', averageEntryPrice='253.1900'))
        self.assertFalse(self.state.note_position_event(MARKET, position))
        self.assertEqual(self.state.market_revisions[MARKET], revision)

    def test_quantity_direction_or_entry_price_change_increments_position_revision(self):
        for field, value in (('netQuantity', '.08'), ('direction', 'long'),
                             ('averageEntryPrice', '253.20')):
            with self.subTest(field=field):
                state = AccountState([MARKET])
                state.apply_untimed_position_snapshot(MARKET, self.position['quantity'])
                state.position_details[MARKET] = dict(self.position)
                self.assertTrue(state.note_position_event(MARKET, self.position))
                revision = state.market_revisions[MARKET]
                changed = dict(SAMPLES['position_nonempty_example']['data'][0],
                               **{field: value})

                self.assertTrue(state.note_position_event(MARKET, normalize_position(changed)))
                self.assertEqual(state.market_revisions[MARKET], revision + 1)


class M25PrivatePositionBurstTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = wiring.M3WiringTests.asyncSetUp
    asyncTearDown = wiring.M3WiringTests.asyncTearDown
    wall = wiring.M3WiringTests.wall
    drive = wiring.M3WiringTests.drive
    ready = wiring.M3WiringTests.ready
    publish = wiring.M3WiringTests.publish

    async def test_rest_entry_price_change_invalidates_previous_private_position(self):
        await self.ready()
        message = deepcopy(SAMPLES['position_nonempty_example'])
        transport = self.bot.api.transport

        async def nonempty_position(request):
            if request.path_query.partition('?')[0].endswith('/positions'):
                return HttpResponse(200, {'success': True, 'result': message['data']})
            return await transport(request)

        self.bot.api.transport = nonempty_position
        self.assertFalse(await self.drive(self.bot.reconcile_once()))
        self.assertTrue(await self.drive(self.bot.reconcile_once()))
        previous = deepcopy(message)
        await self.bot.on_private(previous)
        self.assertTrue(await self.drive(self.bot.reconcile_once()))
        message['data'][0]['averageEntryPrice'] = '253.20'
        self.assertTrue(await self.drive(self.bot.reconcile_once()))
        revision = self.bot.state.market_revisions[MARKET]
        self.bot.reconcile_event.clear()

        await self.bot.on_private(previous)
        self.assertEqual(self.bot.state.market_revisions[MARKET], revision + 1)
        self.assertEqual(self.bot.state.market_mode(MARKET), 'SYNCING')
        self.assertTrue(self.bot.reconcile_event.is_set())
        await self.bot.on_private(previous)
        self.assertEqual(self.bot.state.market_revisions[MARKET], revision + 1)

    async def test_one_hundred_valuation_pushes_keep_revision_and_sync_generation(self):
        await self.ready()
        message = deepcopy(SAMPLES['position_nonempty_example'])
        transport = self.bot.api.transport

        async def nonempty_position(request):
            if request.path_query.partition('?')[0].endswith('/positions'):
                self.requests.append(request)
                return HttpResponse(200, {'success': True, 'result': message['data']})
            return await transport(request)

        self.bot.api.transport = nonempty_position
        self.assertFalse(await self.drive(self.bot.reconcile_once()))
        self.assertTrue(await self.drive(self.bot.reconcile_once()))
        await self.bot.on_private(message)
        self.assertTrue(await self.drive(self.bot.reconcile_once()))
        state = self.bot.state
        self.assertEqual(state.positions[MARKET], D('-.07'))
        self.assertEqual(state.market_mode(MARKET), 'RUNNING')
        revision, generation = state.market_revisions[MARKET], state.generations[MARKET]
        self.bot.reconcile_event.clear()

        for index in range(100):
            push = deepcopy(message)
            push['data'][0]['markPrice'] = str(D('252') + D(index) / 1000)
            push['data'][0]['unrealizedPnl'] = str(D('.07') - D(index) / 10000)
            self.clock.advance(.08)
            await self.bot.on_private(push)

        self.assertEqual(state.market_revisions[MARKET], revision)
        self.assertEqual(state.generations[MARKET], generation)
        self.assertEqual(state.market_mode(MARKET), 'RUNNING')
        self.assertFalse(self.bot.reconcile_event.is_set())


if __name__ == '__main__':
    unittest.main()
