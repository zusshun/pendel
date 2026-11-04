"""窗口盈亏按成交时间归属，REST 补账水位不能被 WS 提前推进。"""

import unittest
from dataclasses import replace
from datetime import datetime
from decimal import Decimal as D
from zoneinfo import ZoneInfo

from pendle.maker import Maker, MarketRules, window_state
from pendle.maker_config import MakerSettings
from test_maker import MARKET, book
from test_maker_e2e import Alerts, Clock, Exchange, Files


class FilteredExchange(Exchange):
    def __init__(self, clock, files):
        super().__init__(clock, files)
        self.fill_queries = []

    async def get_fills(self, market, start_time=None, end_time=None):
        self.fill_queries.append(start_time)
        return [dict(row) for row in self.fills
                if (start_time is None or row['time_ms'] >= start_time)
                and (end_time is None or row['time_ms'] <= end_time)]


class MakerPnlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock, self.files = Clock(), Files()
        self.settings = replace(MakerSettings(), mode='sandbox', quiet_s=.01)
        self.api = FilteredExchange(self.clock, self.files)
        self.bot = self.make_bot()
        await self.bot.startup()
        self.bot.state.deadman_ack()
        await self.bot.reconcile_once()
        self.refresh()

    def make_bot(self, settings=None):
        return Maker(settings or self.settings, api=self.api, alerter=Alerts(),
            rules=MarketRules(D('.01'), D('.001')), files=self.files,
            monotonic=self.clock, clock=self.clock.wall, sleep=self.clock.sleep)

    def refresh(self):
        self.bot.book = book(at=int(self.clock.wall()*1000))
        self.bot.book_seen = self.clock()

    def new_window(self, seconds=0):
        at = datetime(2026, 11, 14, 9, tzinfo=ZoneInfo('Asia/Shanghai'))
        self.clock.epoch = at.timestamp() + seconds - (self.clock.now-100)
        self.refresh()
        return int(at.timestamp()*1000)

    def fill(self, name, at, loss='1'):
        return dict(fill_id=name, market=MARKET, side='buy', quantity=D(1),
                    time_ms=at, realized_pnl=-D(loss), fee=D('.01'))

    async def test_boundary_ws_fill_uses_new_ledger_before_tick_switches_current(self):
        previous = self.bot.ledger
        start = self.new_window(.1)
        await self.bot.record_fill(self.fill('boundary', start+100, '3'))
        self.api.position = D(1)
        self.assertIs(self.bot.ledger, previous)
        self.assertEqual(previous.loss_usd, 0)
        await self.bot.tick()
        self.assertEqual(self.bot.ledger.loss_usd, D('3.01'))
        self.assertEqual(self.bot.ledger.events['boundary'][0], D(-3))

    async def test_old_window_fill_and_outside_window_fill_do_not_pollute_current(self):
        current = self.bot.ledger
        friday = datetime(2026, 11, 6, 12, tzinfo=ZoneInfo('Asia/Shanghai'))
        fill = self.fill('old', int(friday.timestamp()*1000), '2')
        await self.bot.record_fill(fill)
        self.assertIs(self.bot.ledger, current)
        self.assertEqual(current.loss_usd, 0)
        old_window = window_state(self.settings, friday)
        self.assertFalse(old_window.active)
        self.assertEqual(self.bot.ledger_for_window(old_window.window_id).loss_usd, D('2.01'))

    async def test_first_rest_query_of_window_does_not_skip_missing_fill_after_newer_ws(self):
        start = self.new_window(20)
        first = self.fill('rest-only-early', start+100, '3')
        second = self.fill('ws-later', start+20000, '2')
        self.api.fills = [first, second]
        await self.bot.record_fill(second)
        self.api.position = D(2)
        self.bot.book_seen = -100
        await self.bot.tick()
        self.api.fill_queries.clear()
        await self.bot.reconcile_once()
        self.assertEqual(self.api.fill_queries[0], start)
        self.assertEqual(self.bot.ledger.loss_usd, D('5.02'))
        self.assertIn('rest-only-early', self.bot.ledger.events)

    async def test_failed_reconcile_does_not_advance_per_window_rest_watermark(self):
        start = self.new_window(20)
        row = self.fill('rest', start+10000)
        self.api.fills = [row]
        self.api.position = D(1)
        self.bot.book_seen = -100
        await self.bot.tick()
        window_id = self.bot.ledger.window_id
        self.bot.manager.prepare_resume()
        original_positions = self.api.get_positions

        async def changed_positions():
            self.bot.state.revision += 1
            return await original_positions()

        self.api.get_positions = changed_positions
        self.assertFalse(await self.bot.reconcile_once())
        self.assertNotIn(window_id, self.bot.perwindow_fill_sync_ms)
        self.api.get_positions = original_positions
        self.assertTrue(await self.bot.reconcile_once())
        self.assertEqual(self.bot.perwindow_fill_sync_ms[window_id], start+10000)
        later = self.fill('ws-future', start+60000)
        await self.bot.record_fill(later)
        self.api.position = D(2)
        self.api.fill_queries.clear()
        await self.bot.reconcile_once()
        self.assertEqual(self.api.fill_queries[0], start)

    async def test_accounts_have_separate_persistent_window_ledgers(self):
        self.bot.ledger.record('a', realized_pnl='-7')
        other = self.make_bot(replace(self.settings, account='other'))
        await other.startup()
        self.assertEqual(other.ledger.loss_usd, 0)
        self.assertNotEqual(other.ledger.path, self.bot.ledger.path)
        self.assertIn('default', str(self.bot.ledger.path))
        self.assertIn('other', str(other.ledger.path))

    async def test_replayed_fill_is_persistently_deduplicated_after_restart(self):
        fill = self.fill('repeat', int(self.clock.wall()*1000), '4')
        await self.bot.record_fill(fill)
        recovered = self.make_bot()
        await recovered.startup()
        await recovered.record_fill(fill)
        self.assertEqual(recovered.ledger.loss_usd, D('4.01'))
        self.assertEqual(len(recovered.ledger.events), 1)
