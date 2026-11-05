"""M27 resume races through the real Maker builder and in-memory exchange transport."""

import asyncio
import unittest
from dataclasses import replace
from decimal import Decimal as D

import test_m3_wiring as wiring
from test_maker import MARKET
from test_ondo_api import settle


class M27ResumeTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = wiring.M3WiringTests.asyncSetUp
    wall = wiring.M3WiringTests.wall
    drive = wiring.M3WiringTests.drive
    ready = wiring.M3WiringTests.ready
    publish = wiring.M3WiringTests.publish

    async def asyncTearDown(self):
        if hasattr(self, 'drain_release'):
            self.drain_release.set()
            await self.drive(self.drain_sweep)
        await wiring.M3WiringTests.asyncTearDown(self)

    async def start_drain_sweep(self):
        """Pause a real orphan sweep at its remote zero-order confirmation."""
        entered, self.drain_release = asyncio.Event(), asyncio.Event()
        transport = self.bot.api.transport

        async def delayed_snapshot(request):
            if request.method == 'GET' and request.path_query.partition('?')[0].endswith('/orders'):
                entered.set()
                await self.drain_release.wait()
            return await transport(request)

        self.bot.api.transport = delayed_snapshot
        self.drain_sweep = asyncio.create_task(self.bot.manager.drain())
        await self.drive(entered.wait())
        self.bot.api.transport = transport
        self.assertFalse(self.bot.manager._stop_task.done())

    def assert_waiting_without_orders_or_halt(self, stop_task):
        self.assertTrue(self.bot.state.stopping)
        self.assertEqual(self.bot.state.market_mode(MARKET), 'STOPPING')
        self.assertIs(self.bot.manager._stop_task, stop_task)
        self.assertFalse(self.bot.state.active_orders())
        self.assertFalse(self.remote)
        self.assertNotIn(str(self.settings.halt_path), self.files.rows)
        self.assertIsNone(self.bot.halt.exit_code)
        self.assertFalse(self.bot.stop_event.is_set())

    async def finish_sweep_and_quote(self):
        self.drain_release.set()
        self.assertTrue(await self.drive(self.drain_sweep))
        self.publish()
        await self.drive(self.bot.tick())
        self.assertFalse(self.bot.state.stopping)
        self.assertEqual(self.bot.guard.state, 'RUNNING')
        self.assertEqual(self.bot.state.market_mode(MARKET), 'RUNNING')
        self.assertEqual(len(self.remote), 2)
        self.assertTrue(self.bot.qualified)
        self.assertNotIn(str(self.settings.halt_path), self.files.rows)

    async def test_guard_stability_waits_for_drain_then_next_tick_quotes(self):
        await self.ready()
        self.position = self.bot.state.positions[MARKET] = D('-.07')
        self.assertTrue(await self.drive(self.bot.guard.trigger('竞争量超过判据')))
        self.assertEqual(self.bot.guard.state, 'COOLDOWN')
        self.clock.advance(self.settings.cooldown_s)
        self.publish()
        await self.drive(self.bot.tick())
        self.assertEqual(self.bot.guard.state, 'STABILIZING')
        self.clock.advance(self.settings.stable_s)
        self.publish()
        await self.start_drain_sweep()
        stop_task = self.bot.manager._stop_task

        await self.drive(self.bot.tick())

        self.assertEqual(self.bot.guard.state, 'STABILIZING')
        self.assert_waiting_without_orders_or_halt(stop_task)
        self.assertFalse(any(request.method == 'POST' for request in self.requests))
        await self.finish_sweep_and_quote()

    async def test_stopping_tick_waits_for_drain_then_next_tick_quotes(self):
        await self.ready()
        self.assertTrue(await self.drive(self.bot.stop_orders('普通停止')))
        self.assertEqual(self.bot.guard.state, 'RUNNING')
        await self.start_drain_sweep()
        stop_task = self.bot.manager._stop_task

        await self.drive(self.bot.tick())

        self.assert_waiting_without_orders_or_halt(stop_task)
        self.assertFalse(any(request.method == 'POST' for request in self.requests))
        await self.finish_sweep_and_quote()

    async def test_requote_waits_for_concurrent_drain_then_next_tick_quotes(self):
        await self.ready()
        await self.drive(self.bot.tick())
        self.assertEqual(len(self.remote), 2)
        self.clock.advance(self.settings.min_requote_interval_s + 1)
        self.bot.settings = self.bot.engine.settings = replace(self.settings, offset_bps=D(16))
        self.publish()
        stop_orders = self.bot.stop_orders

        async def stop_then_sweep(reason):
            stopped = await stop_orders(reason)
            await self.start_drain_sweep()
            return stopped

        self.bot.stop_orders = stop_then_sweep
        await self.drive(self.bot.tick())

        self.assert_waiting_without_orders_or_halt(self.bot.manager._stop_task)
        self.assertEqual(sum(request.method == 'POST' for request in self.requests), 2)
        self.bot.stop_orders = stop_orders
        await self.finish_sweep_and_quote()

    async def test_flatten_resume_wait_halts_instead_of_skipping_reduction(self):
        await self.ready()
        self.position = self.bot.state.positions[MARKET] = D(7)
        stop_orders = self.bot.stop_orders

        async def stop_then_sweep(reason):
            stopped = await stop_orders(reason)
            if reason == '达到硬上限，先撤单再 IOC 减仓':
                await self.start_drain_sweep()
            return stopped

        self.bot.stop_orders = stop_then_sweep
        flatten = asyncio.create_task(self.bot.flatten(D(100)))
        self.background.append(flatten)
        for _ in range(200):
            await settle()
            if flatten.done() or str(self.settings.halt_path) in self.files.rows:
                break
            self.clock.advance(.01)
        try:
            self.assertIn(str(self.settings.halt_path), self.files.rows)
            self.assertTrue(self.bot.gate.trading_halted)
            self.assertTrue(self.bot.state.stopping)
            self.assertFalse(any(request.method == 'POST' for request in self.requests))
        finally:
            if hasattr(self, 'drain_release'):
                self.drain_release.set()
            result = await self.drive(flatten)
        self.assertFalse(result)
        self.assertEqual(self.bot.halt.exit_code, 76)
