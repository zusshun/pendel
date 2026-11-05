"""Sibling safety scenarios use an in-memory exchange and the real Maker paths."""

import asyncio
import json
import os
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import patch

from pendle.maker import Maker, MarketRules
from pendle.maker_config import MakerSettings
from pendle.maker_orders import Order, OrderStatus, OrdersBlocked
from pendle.maker_risk import DurableFiles, RiskRejected
from test_m5_observations import MemoryLogger
from test_maker import MARKET, book
from test_maker_e2e import Alerts, Clock, Exchange, Files, publish_exchange_book
from test_maker_supervisor import WS

PEER = 'RDDT-USD.P'


class SiblingMakerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock, self.files, self.alerts = Clock(), Files(), Alerts()
        self.api = Exchange(self.clock, self.files)
        self.peer_orders, self.peer_calls = [{'orderId': 'peer-order'}], []
        self.fail_cancel, self.fail_verify = False, False
        cancel, orders = self.api.cancel_all, self.api.get_open_orders

        async def cancel_all(market, **kwargs):
            if market != PEER:
                return await cancel(market, **kwargs)
            self.peer_calls.append(('cancel', market))
            if self.fail_cancel:
                raise OSError('代撤故障')
            self.peer_orders.clear()
            return {}

        async def get_orders(market, **kwargs):
            if market != PEER:
                return await orders(market, **kwargs)
            self.peer_calls.append(('verify', market))
            if self.fail_verify:
                raise OSError('核零故障')
            return list(self.peer_orders)

        self.api.cancel_all, self.api.get_open_orders = cancel_all, get_orders
        self.settings = replace(MakerSettings(), mode='sandbox', quiet_s=.01,
            sibling_markets=[PEER], peer_heartbeat_paths={PEER: 'peer/heartbeat.json'})
        self.bot = Maker(self.settings, api=self.api, alerter=self.alerts,
            rules=MarketRules('.01', '.001'), files=self.files, logger=MemoryLogger(),
            monotonic=self.clock, clock=self.clock.wall, sleep=self.clock.sleep)
        await self.bot.startup()
        self.bot.state.deadman_ack()
        self.assertTrue(await self.bot.reconcile_once())
        self.refresh()
        self.ws = WS()
        self.ws.on_send = self.bot.on_private
        self.bot.private_ws, self.bot.private_logged_in = self.ws, True

    def refresh(self):
        self.bot.book = book(at=int(self.clock.wall()*1000))
        self.bot.book_seen = self.clock()
        self.bot.engine.mark = (D(100), self.clock())
        self.bot.engine.in_price_discovery = False
        self.bot.engine.price_discovery_seen = self.clock()

    def peer_heartbeat(self, **changes):
        row = dict(market=PEER, pid=123, time_ms=int(self.clock.wall()*1000))
        row.update(changes)
        self.files.write_json(self.settings.peer_heartbeat_paths[PEER], row)

    async def watchdog_once(self, *, healthy=True):
        for name in self.bot.watchdog.thresholds:
            self.bot.watchdog.last_seen[name] = self.clock()
        if not healthy:
            self.bot.watchdog.last_seen['Strategy'] -= 100
        count = 0

        async def iteration(delay):
            nonlocal count
            count += 1
            if count > 1:
                raise asyncio.CancelledError
            await asyncio.sleep(0)

        self.bot.sleep = iteration
        try:
            await self.bot.watchdog_loop()
        except asyncio.CancelledError:
            pass
        finally:
            self.bot.sleep = self.clock.sleep

    async def test_first_heartbeat_is_written_by_healthy_watchdog_before_first_order(self):
        path = str(self.settings.heartbeat_path)
        self.assertNotIn(path, self.files.rows)
        await self.bot.tick()
        self.assertFalse(self.bot.state.active_orders())
        await self.watchdog_once()
        self.assertEqual(self.files.rows[path], {
            'market': MARKET, 'pid': os.getpid(), 'time_ms': int(self.clock.wall()*1000)})
        def publish():
            self.assertIn(path, self.files.rows)
            publish_exchange_book(self.bot, self.api, self.clock)

        self.api.on_quote = publish
        self.refresh()
        await self.bot.tick()
        self.assertEqual(len(self.bot.state.active_orders()), 2)

    async def test_synchronous_send_callback_blocks_before_watchdog_heartbeat(self):
        with self.assertRaisesRegex(OrdersBlocked, '心跳'):
            self.bot.check_order(Order('early', MARKET, 'buy', D(4), D('99.88')),
                                 self.bot.state)

    async def test_unhealthy_watchdog_does_not_write_or_refresh_heartbeat(self):
        await self.watchdog_once()
        before = dict(self.files.rows[str(self.settings.heartbeat_path)])
        self.clock.now += 1
        await self.watchdog_once(healthy=False)
        self.assertEqual(self.files.rows[str(self.settings.heartbeat_path)], before)
        self.assertFalse(self.bot.gate.is_open)

    async def test_gate_closing_during_ping_prevents_a_final_fresh_heartbeat(self):
        async def ping_then_close():
            self.bot.gate.close()

        self.bot.watchdog.ping = ping_then_close
        await self.watchdog_once()
        self.assertNotIn(str(self.settings.heartbeat_path), self.files.rows)

    async def test_peer_kill_cancels_and_verifies_without_importing_peer_orders(self):
        self.assertTrue(await self.bot.check_peer_once(PEER))
        self.assertEqual(self.peer_calls, [('cancel', PEER), ('verify', PEER)])
        self.assertEqual(self.peer_orders, [])
        self.assertEqual(self.bot.state.markets, (MARKET,))
        self.assertFalse(self.bot.state.orders)
        self.assertTrue(self.bot.gate.is_open)
        self.assertIsNone(self.bot.halt.exit_code)
        self.assertEqual(len(self.alerts.messages), 1)

    async def test_stale_peer_is_swept_at_most_every_five_seconds_and_alerted_once(self):
        started = self.clock()
        await self.bot.check_peer_once(PEER)
        for seconds in (0, 1, 3.99):
            self.clock.now = started+seconds
            await self.bot.check_peer_once(PEER)
        self.assertEqual(len(self.peer_calls), 2)
        self.clock.now = started+5
        await self.bot.check_peer_once(PEER)
        self.assertEqual(len(self.peer_calls), 4)
        self.assertEqual(len(self.alerts.messages), 1)

    async def test_fresh_peer_recovers_with_info_log_and_new_stale_episode_alerts(self):
        await self.bot.check_peer_once(PEER)
        self.peer_heartbeat()
        self.assertTrue(await self.bot.check_peer_once(PEER))
        self.assertEqual(len(self.peer_calls), 2)
        self.assertTrue(any(row.get('事件') == '对端心跳恢复' for row in self.bot.logger.rows))
        self.clock.now += 21
        await self.bot.check_peer_once(PEER)
        self.assertEqual(len(self.alerts.messages), 2)

    async def test_invalid_peer_heartbeats_are_all_stale(self):
        for row in (None, [], {}, {'market': MARKET, 'pid': 123, 'time_ms': int(self.clock.wall()*1000)},
                    {'market': PEER, 'pid': 123, 'time_ms': 'bad'},
                    {'market': PEER, 'pid': 123, 'time_ms': True},
                    {'market': PEER, 'pid': 123, 'time_ms': float('nan')},
                    {'market': PEER, 'pid': 123, 'time_ms': int((self.clock.wall()-21)*1000)},
                    {'market': PEER, 'time_ms': int(self.clock.wall()*1000)}):
            with self.subTest(row=row):
                self.peer_heartbeat()
                await self.bot.check_peer_once(PEER)
                self.files.rows[str(self.settings.peer_heartbeat_paths[PEER])] = row
                self.clock.now += 5
                before = len(self.peer_calls)
                await self.bot.check_peer_once(PEER)
                self.assertEqual(len(self.peer_calls), before+2)

    async def test_unreadable_peer_heartbeat_is_stale(self):
        original = self.files.read_text

        def unreadable(path):
            if path == self.settings.peer_heartbeat_paths[PEER]:
                raise PermissionError
            return original(path)

        self.files.read_text = unreadable
        self.assertTrue(await self.bot.check_peer_once(PEER))
        self.assertEqual(len(self.peer_calls), 2)

    async def test_cancel_and_verify_failures_halt_after_timeout_and_keep_deadman_armed(self):
        for failure in ('fail_cancel', 'fail_verify'):
            with self.subTest(failure=failure):
                if self.bot.stop_event.is_set():
                    await self.asyncSetUp()
                setattr(self, failure, True)
                self.assertFalse(await self.bot.check_peer_once(PEER))
                self.assertIsNone(self.bot.halt.exit_code)
                self.clock.now += self.settings.peer_cancel_timeout_s
                self.assertFalse(await self.bot.check_peer_once(PEER))
                self.assertEqual(self.bot.halt.exit_code, 76)
                self.assertTrue(self.bot.stop_event.is_set())
                self.assertFalse(self.bot.gate.is_open)
                self.assertIn(str(self.settings.halt_path), self.files.rows)
                before = list(self.ws.sent)
                await self.bot.watchdog_loop()
                self.assertEqual(self.ws.sent, before)
                await self.bot.shutdown()
                self.assertFalse(any(row.get('timeout_seconds') == 0 for row in self.ws.sent))

    async def test_nonempty_verification_is_failure_even_after_successful_cancel(self):
        async def ignored_cancel(market, **kwargs):
            self.peer_calls.append(('cancel', market))

        self.api.cancel_all = ignored_cancel
        self.assertFalse(await self.bot.check_peer_once(PEER))
        self.clock.now += 15
        self.assertFalse(await self.bot.check_peer_once(PEER))
        self.assertEqual(self.bot.exit_code, 76)

    async def test_hung_peer_cancel_is_bounded_by_peer_timeout(self):
        self.bot.settings = replace(self.settings, peer_cancel_timeout_s=.01)
        cancelled = asyncio.Event()
        original = self.api.cancel_all

        async def hung(market, **kwargs):
            if market != PEER:
                return await original(market, **kwargs)
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.api.cancel_all = hung
        self.assertFalse(await self.bot.check_peer_once(PEER))
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.bot.exit_code, 76)
        self.assertFalse(self.bot.gate.is_open)

    async def test_sibling_rest_positions_use_own_static_budget_and_do_not_halt(self):
        original = self.api.get_positions

        async def positions():
            return await original() + [dict(market=PEER, quantity=D('999'), unrealized_pnl=D('-999'))]

        self.api.get_positions = positions
        self.assertTrue(await self.bot.reconcile_once())
        await self.watchdog_once()
        self.refresh()
        self.bot.check_order(Order('allowed', MARKET, 'buy', D(4), D('99.88')), self.bot.state)
        self.assertEqual(self.bot.state.markets, (MARKET,))
        self.assertEqual(self.bot.unrealized(), 0)
        self.assertIsNone(self.bot.halt.exit_code)
        await self.bot.stop_orders('普通停止')
        self.assertFalse(self.peer_calls)

    async def test_non_sibling_rest_positions_still_halt_and_sync_send_rejects(self):
        row = dict(market='OTHER-USD.P', quantity=D(1), unrealized_pnl=D(0))
        original = self.api.get_positions

        async def positions():
            return await original() + [row]

        self.api.get_positions = positions
        self.assertFalse(await self.bot.reconcile_once())
        self.assertEqual(self.bot.halt.exit_code, 76)
        self.bot._heartbeat_ready = True
        with self.assertRaisesRegex(RiskRejected, '其他市场'):
            self.bot.check_order(Order('blocked', MARKET, 'buy', D(4), D('99.88')), self.bot.state)

    async def test_private_sibling_rows_never_enter_own_state_or_halt(self):
        for channel, row in (
                ('positionsPerps', dict(market=PEER, direction='long', netQuantity='999')),
                ('ordersPerps', dict(market=PEER, orderId='peer', status='OPEN')),
                ('fillsPerps', dict(market=PEER, id='peer-fill'))):
            await self.bot.on_private({'channel': channel, 'data': [row]})
        self.assertIsNone(self.bot.halt.exit_code)
        self.assertEqual(self.bot.state.markets, (MARKET,))
        self.assertFalse(self.bot.state.orders)
        self.assertFalse(self.bot.state.fills)

    async def test_private_incomplete_sibling_position_does_not_reject_own_state(self):
        await self.bot.on_private({'channel': 'positionsPerps', 'data': [
            {'market': MARKET, 'direction': 'neutral', 'netQuantity': '0'},
            {'market': PEER, 'direction': 'invalid', 'netQuantity': 'NaN'}]})
        self.assertFalse(any('关键字段' in message for message in self.alerts.messages))
        self.assertTrue(self.bot.state.deadman_acked)

    async def test_funding_is_not_recorded_in_sibling_window_ledger(self):
        before = dict(self.bot.ledger.events)
        for total in ('100', '99', '101'):
            self.bot.state.balances['totalFundingPayments'] = total
            self.bot.record_funding()
        self.assertEqual(self.bot.ledger.events, before)

    async def test_graceful_exit_sweeps_own_market_without_disabling_deadman(self):
        await self.bot.stop_orders('先前普通停止')
        before = len([c for c in self.api.calls if c[0] == 'cancel_all'])
        await self.bot.shutdown()
        self.assertEqual(len([c for c in self.api.calls if c[0] == 'cancel_all']), before+1)
        self.assertFalse(any(row.get('timeout_seconds') == 0 for row in self.ws.sent))
        self.assertFalse(self.peer_calls)

    async def test_sibling_empty_preserves_funding_and_no_heartbeat_or_peer_calls(self):
        self.bot.settings = self.bot.engine.settings = replace(self.settings,
            sibling_markets=(), peer_heartbeat_paths={})
        self.bot.state.balances['totalFundingPayments'] = '100'
        self.bot.record_funding()
        self.bot.state.balances['totalFundingPayments'] = '101'
        self.bot.record_funding()
        self.assertEqual(self.bot.ledger.loss_usd, 1)
        await self.watchdog_once()
        self.assertNotIn(str(self.settings.heartbeat_path), self.files.rows)
        await self.bot.shutdown()
        self.assertTrue(any(row.get('timeout_seconds') == 0 for row in self.ws.sent))
        self.assertFalse(self.peer_calls)


class SiblingHeartbeatAtomicTests(unittest.TestCase):
    def test_heartbeat_write_uses_temporary_file_and_atomic_replace(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = MakerSettings(data_dir=Path(directory),
                sibling_markets=[PEER], peer_heartbeat_paths={PEER: Path(directory)/'peer.json'})
            bot = Maker(settings, api=None, alerter=Alerts())
            replacements = []
            original = os.replace

            def rename(source, target):
                replacements.append((source, target))
                self.assertNotEqual(source, target)
                self.assertEqual(json.loads(Path(source).read_text())['market'], MARKET)
                return original(source, target)

            with patch('os.replace', side_effect=rename):
                bot.write_heartbeat()
            self.assertEqual(len(replacements), 1)
            row = json.loads(settings.heartbeat_path.read_text())
            self.assertEqual(set(row), {'market', 'pid', 'time_ms'})
            self.assertEqual(list(Path(directory).glob('*.tmp-*')), [])
