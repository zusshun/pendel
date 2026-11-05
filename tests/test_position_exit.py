"""Position exit acceptance tests through the real Maker and in-memory exchange."""

import unittest
from dataclasses import replace
from decimal import Decimal as D
from unittest.mock import patch

import test_maker_e2e as e2e
from pendle.maker import Maker
from pendle.maker_config import MakerSettings
from test_maker import MARKET


class ExitConfigTests(unittest.TestCase):
    def test_defaults_and_validation(self):
        cfg = MakerSettings()
        for name in ('max_hold_s', 'stop_loss_bps', 'exit_reduce_offset_bps'):
            self.assertIsNone(getattr(cfg, name))
            for value in (0, -1, float('inf'), float('nan'), True):
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    replace(cfg, **{name: value})
        for changes in ({'stop_loss_bps': 100}, {'exit_reduce_offset_bps': 7},
                        {'exit_reduce_offset_bps': 23}):
            with self.assertRaises(ValueError):
                replace(cfg, **changes)
        cfg = replace(cfg, max_hold_s=600, stop_loss_bps=30, exit_reduce_offset_bps=8)
        self.assertEqual(cfg.exit_reduce_offset_bps, D(8))

    def test_summary_reports_period_exit_counts(self):
        from pendle.alerts import Alerter
        alerter = Alerter()
        alerter._last_summary = 0
        alerter._summary_baseline = alerter._summary_snapshot({'守卫及停报原因': {
            '持仓超时离场': {'次数': 2, '时长秒': 10},
            '止损离场': {'次数': 1, '时长秒': 5}}})
        metrics = {'守卫及停报原因': {
            '持仓超时离场': {'次数': 3, '时长秒': 10},
            '止损离场': {'次数': 3, '时长秒': 5}}}
        summary = alerter._format_summary(metrics, alerter._summary_snapshot(metrics), 3600)
        self.assertIn('持仓超时离场：1次', summary)
        self.assertIn('止损离场：2次', summary)


class PositionExitTests(unittest.IsolatedAsyncioTestCase):
    refresh = e2e.MakerE2E.refresh

    async def asyncSetUp(self):
        await e2e.MakerE2E.asyncSetUp(self)
        self.configure(max_hold_s=600, stop_loss_bps=30, exit_reduce_offset_bps=8, quiet_s=.125)
        self.bot.manager.quiet_s = .125
        self.entry = None
        async def positions():
            row = dict(market=MARKET, quantity=self.exchange.position, unrealized_pnl=D(0),
                       mark_price=D(100), time_ms=int(self.clock.wall()*1000))
            if self.entry is not None:
                row['averageEntryPrice'] = self.entry
            return [row]
        self.exchange.get_positions = positions
        original_place = self.exchange.place_order
        async def place(body, **kwargs):
            self.clock.now += .25
            return await original_place(body, **kwargs)
        self.exchange.place_order = place

    def configure(self, **changes):
        self.settings = replace(self.settings, **changes)
        self.bot.settings = self.bot.engine.settings = self.settings

    async def position(self, quantity, entry=None):
        self.clock.now += .25
        self.exchange.position = D(quantity)
        self.entry = D(entry) if entry is not None else None
        await self.bot.reconcile_once()
        await self.bot.reconcile_once()
        if entry is not None:
            self.bot.state.position_details[MARKET]['averageEntryPrice'] = D(entry)
        self.refresh()

    def iocs(self):
        return [c[1] for c in self.exchange.calls if c[0] == 'place'
                and c[1]['timeInForce'] == 'IOC']

    async def test_first_fill_starts_timer_and_same_sign_keeps_it(self):
        await self.bot.tick()
        self.clock.now += 10
        await self.bot.record_fill(dict(fill_id='first', market=MARKET, side='buy',
            quantity=D(1), time_ms=int(self.clock.wall()*1000)))
        started = self.bot.position_started_at
        self.assertEqual(started, self.clock())
        self.clock.now += 20
        await self.position('2')
        self.assertEqual(self.bot.position_started_at, started)
        await self.position('1')
        self.assertEqual(self.bot.position_started_at, started)
        await self.position('-1')
        self.assertEqual(self.bot.position_started_at, self.clock())
        await self.position('0')
        self.assertIsNone(self.bot.position_started_at)

    async def test_reducing_quote_uses_eight_bps(self):
        for quantity, side, price in (('1', 'sell', D('100.08')), ('-1', 'buy', D('99.92'))):
            with self.subTest(quantity=quantity):
                await self.position(quantity)
                decision = self.bot.engine.evaluate(self.bot.book, self.bot.state,
                                                    self.bot.rules, now=self.clock())
                self.assertTrue(decision.eligible, decision.reason)
                quote = next(q for q in decision.quotes if q.side == side)
                self.assertEqual(quote.price, price)

    async def test_timeout_cancels_verifies_and_exits_full_position_then_quotes(self):
        await self.position('1')
        started = self.bot.position_started_at
        await self.bot.tick()
        self.assertEqual(len(self.bot.state.active_orders()), 2)
        calls = []
        original = self.exchange.get_open_orders
        async def verified(*args, **kwargs):
            rows = await original(*args, **kwargs)
            calls.append(('verify', len(rows)))
            return rows
        original_place = self.exchange.place_order
        async def place(body, **kwargs):
            if body['timeInForce'] == 'IOC':
                self.assertIn(('verify', 0), calls)
                self.assertTrue(all(o.time_in_force == 'IOC' for o in self.bot.state.active_orders()))
            return await original_place(body, **kwargs)
        self.exchange.get_open_orders = verified
        self.exchange.place_order = place
        self.clock.now = started + 600
        self.refresh()
        self.exchange.ioc_sizes = [D(1)]
        await self.bot.tick()
        self.assertEqual(len(self.iocs()), 1)
        self.assertEqual(self.iocs()[0]['size'], '1')
        self.assertTrue(self.iocs()[0]['reduceOnly'])
        self.assertFalse(self.iocs()[0]['postOnly'])
        self.assertEqual(self.bot.state.positions[MARKET], 0)
        self.assertIsNone(self.bot.position_started_at)
        self.assertEqual(self.bot.pause_counts['持仓超时离场'], 1)
        self.assertIn('持仓超时离场', self.bot.observation_metrics()['守卫及停报原因'])
        self.refresh()
        await self.bot.tick()
        self.assertEqual(len(self.bot.state.active_orders()), 2)

    async def test_stop_loss_long_short_and_unknown_entry(self):
        for quantity, entry, expected in (('1', '100.4', True), ('-1', '99.6', True),
                                        ('1', '99.6', False), ('-1', '100.4', False),
                                        ('1', None, False)):
            await self.asyncSetUp()
            await self.position(quantity, entry)
            self.exchange.ioc_sizes = [D(1)]
            await self.bot.tick()
            self.assertEqual(bool(self.iocs()), expected, (quantity, entry))
            if expected:
                self.assertEqual(self.bot.pause_counts['止损离场'], 1)

    async def test_partial_or_no_fill_backs_off_and_alerts_once(self):
        for fill in (D(0), D('.4')):
            await self.asyncSetUp()
            await self.position('1')
            self.clock.now += 600
            self.refresh()
            self.exchange.ioc_sizes = [fill]
            await self.bot.tick()
            self.assertIsNone(self.bot.halt.exit_code)
            self.assertEqual(len(self.iocs()), 1)
            self.refresh()
            await self.bot.tick()
            self.assertEqual(len(self.iocs()), 1)
            self.clock.now += 30
            self.refresh()
            await self.bot.tick()
            self.assertEqual(len(self.iocs()), 2)
            self.assertEqual(self.bot.pause_counts['持仓超时离场'], 1)
            self.assertEqual(sum('未完全成交' in msg for msg in self.alerts.messages), 1)

    async def test_prepare_resume_pending_retries_next_tick(self):
        await self.position('1')
        self.clock.now += 600
        self.refresh()
        with patch.object(self.bot.manager, 'prepare_resume', return_value=False):
            await self.bot.tick()
        self.assertIsNone(self.bot.halt.exit_code)
        self.assertFalse(self.iocs())
        self.exchange.ioc_sizes = [D(1)]
        self.refresh()
        await self.bot.tick()
        self.assertEqual(len(self.iocs()), 1)

    async def test_restart_existing_position_starts_timer(self):
        self.exchange.position = D(1)
        self.clock.now += 1000
        bot = Maker(self.settings, api=self.exchange, alerter=self.alerts,
                    rules=self.bot.rules, files=e2e.Files(), monotonic=self.clock,
                    clock=self.clock.wall, sleep=self.clock.sleep)
        await bot.startup()
        bot.state.deadman_ack()
        await bot.reconcile_once()
        self.assertEqual(bot.position_started_at, self.clock())

    async def test_dry_run_logs_intent_without_writes(self):
        await self.position('1')
        self.configure(mode='dry-run')
        self.clock.now += 600
        self.refresh()
        with patch.object(self.bot, 'log') as log:
            await self.bot.tick()
        self.assertFalse(self.exchange.calls)
        self.assertTrue(any(c.args[0].get('原因') == '持仓超时离场' for c in log.call_args_list))

    async def test_due_exit_with_stale_book_still_cancels_resting_orders(self):
        await self.position('1')
        await self.bot.tick()
        self.assertEqual(len(self.bot.state.active_orders()), 2)
        self.clock.now += 600
        await self.bot.tick()
        self.assertFalse(self.iocs())
        self.assertFalse(self.bot.state.active_orders())

    async def test_timeout_in_soft_cap_and_inventory_reduce_modes(self):
        for quantity in ('4', '6'):
            await self.asyncSetUp()
            await self.position(quantity)
            decision = self.bot.engine.evaluate(self.bot.book, self.bot.state,
                                                self.bot.rules, now=self.clock())
            if quantity == '6':
                self.assertTrue(decision.inventory_reducing)
            self.clock.now += 600
            self.refresh()
            self.exchange.ioc_sizes = [D(quantity)]
            await self.bot.tick()
            self.assertEqual(D(self.iocs()[0]['size']), D(quantity))
            self.assertEqual(self.exchange.position, 0)
            self.assertIsNone(self.bot.halt.exit_code)

    async def test_timeout_in_sibling_mode_ignores_peer_position(self):
        self.configure(sibling_markets=('RDDT-USD.P',),
                       peer_heartbeat_paths={'RDDT-USD.P': 'peer/heartbeat.json'})
        self.bot._heartbeat_ready = True
        positions = self.exchange.get_positions
        async def with_peer():
            return await positions() + [dict(market='RDDT-USD.P', quantity=D(5))]
        self.exchange.get_positions = with_peer
        await self.position('1')
        self.clock.now += 600
        self.refresh()
        self.exchange.ioc_sizes = [D(1)]
        await self.bot.tick()
        self.assertEqual(self.exchange.position, 0)
        self.assertEqual(self.iocs()[0]['market'], MARKET)
        self.assertIsNone(self.bot.halt.exit_code)

    async def test_outside_window_or_halted_does_not_exit(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        await self.position('1')
        self.clock.now += 600
        self.clock.epoch = datetime(2026, 11, 10, 10, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        self.refresh()
        await self.bot.tick()
        self.assertFalse(self.iocs())
        self.clock.epoch = datetime(2026, 11, 7, 10, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        await self.bot.trip_halt('测试熔断')
        self.refresh()
        await self.bot.tick()
        self.assertFalse(self.iocs())

    async def test_exit_reconcile_failure_still_halts(self):
        await self.position('1')
        self.clock.now += 600
        self.refresh()
        with patch.object(self.bot, 'reconcile_once', return_value=False):
            await self.bot.tick()
        self.assertIsNotNone(self.bot.halt.exit_code)
        self.assertFalse(self.iocs())

    async def test_flip_does_not_use_previous_episode_entry(self):
        await self.position('1', '90')
        self.clock.now += 1
        self.exchange.position = D(-1)
        self.entry = D(100)
        await self.bot.record_fill(dict(fill_id='flip', market=MARKET, side='sell',
            quantity=D(2), time_ms=int(self.clock.wall()*1000)))
        self.refresh()
        await self.bot.tick()
        self.assertFalse(self.iocs())
        self.assertIsNone(self.bot.position_exit_reason)

    async def test_due_exit_without_center_stops_and_keeps_exit_pending(self):
        await self.position('1')
        await self.bot.tick()
        self.clock.now += 600
        self.refresh()
        with patch.object(self.bot, 'reduction_center', return_value=None):
            await self.bot.tick()
        self.assertFalse(self.bot.state.active_orders())
        self.assertFalse(self.iocs())
        self.assertEqual(self.bot.position_exit_reason, '持仓超时离场')
        self.assertIsNone(self.bot.halt.exit_code)

    async def test_stop_loss_reacts_to_adverse_mark_move_before_timeout(self):
        for quantity, mark, expected in (('1', '99.69', True), ('-1', '100.31', True),
                                         ('1', '100.31', False), ('-1', '99.69', False)):
            await self.asyncSetUp()
            await self.position(quantity, '100')
            self.clock.now += 10
            self.refresh()
            self.bot.engine.mark = (D(mark), self.clock())
            self.exchange.ioc_sizes = [D(1)]
            await self.bot.tick()
            self.assertEqual(bool(self.iocs()), expected, (quantity, mark))
            self.assertLess(self.clock() - 100, 600)

    async def test_disabled_exits_hold_without_ioc_after_timeout(self):
        self.configure(max_hold_s=None, stop_loss_bps=None, exit_reduce_offset_bps=None)
        await self.position('1', '110')
        self.clock.now += 1000
        self.refresh()
        await self.bot.tick()
        self.assertFalse(self.iocs())
        self.assertEqual({o.side for o in self.bot.state.active_orders()}, {'buy', 'sell'})
        self.assertEqual(self.bot.pause_counts['持仓超时离场'], 0)
        self.assertEqual(self.bot.pause_counts['止损离场'], 0)
