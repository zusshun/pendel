"""首版单市场账户不得忽略其他市场的名义持仓。"""

import unittest
from dataclasses import replace
from decimal import Decimal as D

from pendle.maker import Maker, MarketRules
from pendle.maker_config import MakerSettings
from pendle.maker_orders import AccountState, Order
from pendle.maker_risk import RiskRejected
from test_maker import MARKET, book
from test_maker_e2e import Alerts, Clock, Exchange, Files


class MakerAccountTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock, self.files, self.alerts = Clock(), Files(), Alerts()
        self.api = Exchange(self.clock, self.files)
        self.settings = replace(MakerSettings(), mode='sandbox', quiet_s=.01)
        self.bot = Maker(self.settings, api=self.api, alerter=self.alerts,
            rules=MarketRules(D('.01'), D('.001')), files=self.files,
            monotonic=self.clock, clock=self.clock.wall, sleep=self.clock.sleep)
        await self.bot.startup()
        self.bot.state.deadman_ack()
        await self.bot.reconcile_once()
        self.bot.book = book(at=int(self.clock.wall()*1000))
        self.bot.book_seen = self.clock()
        self.bot.engine.mark = (D(100), self.clock())
        self.bot.engine.in_price_discovery = False
        self.bot.engine.price_discovery_seen = self.clock()

    def other(self, quantity='20'):
        return dict(market='OTHER-USD.P', quantity=D(quantity), mark_price=D(100),
                    unrealized_pnl=D(0))

    async def test_state_starts_with_explicit_empty_account_position_snapshot(self):
        self.assertEqual(AccountState([MARKET]).account_positions, [])

    async def test_reconcile_detects_other_market_and_halts_without_new_order(self):
        original = self.api.get_positions

        async def positions():
            return await original() + [self.other()]

        self.api.get_positions = positions
        self.assertFalse(await self.bot.reconcile_once())
        self.assertEqual(self.bot.halt.exit_code, 76)
        self.assertTrue(any('其他市场持仓' in text for text in self.alerts.messages))
        self.assertFalse(any(call[0] == 'place' for call in self.api.calls))

    async def test_sync_risk_callback_rejects_other_market_before_actual_send(self):
        self.bot.state.account_positions = [self.other()]
        order = Order('proof', MARKET, 'buy', D(4), D(100))
        with self.assertRaisesRegex(RiskRejected, '其他市场持仓'):
            self.bot.check_order(order, self.bot.state)

    async def test_zero_other_position_is_retained_and_does_not_block(self):
        original = self.api.get_positions
        rows = await original() + [self.other('0')]

        async def positions():
            return rows

        self.api.get_positions = positions
        self.assertTrue(await self.bot.reconcile_once())
        self.assertEqual(len(self.bot.state.account_positions), 2)
        rows[1]['quantity'] = D(20)
        self.assertEqual(self.bot.state.account_positions[1]['quantity'], 0)
        self.bot.check_order(Order('proof', MARKET, 'buy', D(4), D('99.88')), self.bot.state)

    async def test_changed_revision_during_snapshot_does_not_replace_account_view(self):
        previous = list(self.bot.state.account_positions)
        original = self.api.get_positions

        async def positions():
            self.bot.state.revision += 1
            return await original() + [self.other()]

        self.api.get_positions = positions
        self.assertFalse(await self.bot.reconcile_once())
        self.assertEqual(self.bot.state.account_positions, previous)
