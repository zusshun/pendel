"""M12: deduplicate private updates against the currently confirmed account state."""

import unittest
from copy import deepcopy
from decimal import Decimal

import test_m3_wiring as wiring
from pendle.maker_orders import AccountState, OrdersBlocked
from pendle.ondo_api import normalize_balance, normalize_position
from test_maker import MARKET
from test_m11_positions import SAMPLES


class M12StateDedupTests(unittest.TestCase):
    def setUp(self):
        self.state = AccountState([MARKET])
        self.zero = normalize_position({'market': MARKET, 'quantity': '0'})

    def test_timed_rest_update_prevents_dropping_previous_ws_position(self):
        self.assertTrue(self.state.note_position_event(MARKET, self.zero))
        self.state.apply_position_snapshot(MARKET, '1', 100)

        revision = self.state.market_revisions[MARKET]
        self.assertTrue(self.state.note_position_event(MARKET, self.zero))
        self.assertEqual(self.state.market_revisions[MARKET], revision + 1)

    def test_untimed_rest_update_prevents_dropping_previous_ws_position(self):
        self.assertTrue(self.state.note_position_event(MARKET, self.zero))
        self.state.apply_untimed_position_snapshot(MARKET, '1')

        revision = self.state.market_revisions[MARKET]
        self.assertTrue(self.state.note_position_event(MARKET, self.zero))
        self.assertEqual(self.state.market_revisions[MARKET], revision + 1)

    def test_identical_ws_content_is_not_duplicate_of_different_current_position(self):
        self.state.apply_untimed_position_snapshot(MARKET, '1')
        self.assertTrue(self.state.note_position_event(MARKET, self.zero))

        revision = self.state.market_revisions[MARKET]
        self.assertTrue(self.state.note_position_event(MARKET, self.zero))
        self.assertEqual(self.state.market_revisions[MARKET], revision + 1)

    def test_ws_metadata_after_same_quantity_rest_change_is_not_duplicate(self):
        position = normalize_position({'market': MARKET, 'quantity': '1',
                                       'unrealizedPnl': '0', 'markPrice': '100'})
        self.state.apply_untimed_position_snapshot(MARKET, '1')
        self.state.position_details[MARKET] = dict(position)
        self.assertTrue(self.state.note_position_event(MARKET, position))

        # Match real REST reconciliation: apply quantities first, then store the full confirmed details.
        self.state.apply_untimed_position_snapshot(MARKET, '1')
        self.state.position_details[MARKET] = normalize_position(dict(
            position, unrealizedPnl='-10', markPrice='90'))
        revision = self.state.market_revisions[MARKET]

        for change in (1, 2):
            self.assertTrue(self.state.note_position_event(MARKET, position))
            self.assertEqual(self.state.market_revisions[MARKET], revision + change)

        self.state.position_details[MARKET] = dict(position)
        self.assertFalse(self.state.note_position_event(MARKET, position))
        self.assertEqual(self.state.market_revisions[MARKET], revision + 2)

    def test_ws_balance_after_rest_change_compares_current_confirmed_balance(self):
        for timed in (False, True):
            with self.subTest(timed=timed):
                state = AccountState([MARKET])
                balance = normalize_balance(SAMPLES['balance_update']['data'])
                first = dict(balance, time_ms=100) if timed else dict(balance)
                confirmed = dict(balance, available='299')
                if timed:
                    confirmed['time_ms'] = 200
                self.assertTrue(state.apply_balance_snapshot(first, source='ws'))
                self.assertTrue(state.apply_balance_snapshot(confirmed))
                revision = state.revision

                latest = dict(balance, time_ms=300) if timed else dict(balance)
                self.assertTrue(state.apply_balance_snapshot(latest, source='ws'))
                self.assertEqual(state.balances['available'], '300')
                self.assertEqual(state.revision, revision + 1)

                repeated = dict(balance, time_ms=400) if timed else dict(balance)
                self.assertTrue(state.apply_balance_snapshot(repeated, source='ws'))
                self.assertEqual(state.revision, revision + 1)


class M12PositionReconcileTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = wiring.M3WiringTests.asyncSetUp
    asyncTearDown = wiring.M3WiringTests.asyncTearDown
    wall = wiring.M3WiringTests.wall
    drive = wiring.M3WiringTests.drive
    ready = wiring.M3WiringTests.ready
    publish = wiring.M3WiringTests.publish

    async def test_empty_ws_after_rest_confirms_nonzero_updates_zero_and_requests_reconcile(self):
        await self.ready()
        state = self.bot.state
        await self.bot.on_private(deepcopy(SAMPLES['positions_update_empty']))
        self.position = Decimal(1)
        self.assertFalse(await self.drive(self.bot.reconcile_once()))
        self.assertTrue(await self.drive(self.bot.reconcile_once()))
        self.assertEqual(state.positions[MARKET], 1)
        self.assertEqual(state.market_mode(MARKET), 'RUNNING')
        state.require_running(MARKET)
        self.bot.reconcile_event.clear()
        revision = state.market_revisions[MARKET]
        generation = state.generations[MARKET]

        self.position = Decimal(0)
        await self.bot.on_private(deepcopy(SAMPLES['positions_update_empty']))

        self.assertEqual(state.positions[MARKET], 0)
        self.assertEqual(state.market_revisions[MARKET], revision + 1)
        self.assertEqual(state.generations[MARKET], generation + 1)
        self.assertEqual(state.market_mode(MARKET), 'SYNCING')
        self.assertTrue(self.bot.reconcile_event.is_set())
        with self.assertRaises(OrdersBlocked):
            state.require_running(MARKET)

        self.bot.reconcile_event.clear()
        await self.bot.on_private(deepcopy(SAMPLES['positions_update_empty']))
        self.assertEqual(state.market_revisions[MARKET], revision + 1)
        self.assertEqual(state.generations[MARKET], generation + 1)
        self.assertFalse(self.bot.reconcile_event.is_set())
        self.assertTrue(await self.drive(self.bot.reconcile_once()))
        self.assertEqual(state.positions[MARKET], 0)
        state.require_running(MARKET)


if __name__ == '__main__':
    unittest.main()
