"""实盘风险模块只使用内存时钟与文件系统。"""

import asyncio
import errno
import json
import os
import unittest
from decimal import Decimal as D

from pendle import maker_risk as risk
from pendle.ondo_api import BALANCES_PATH, GateClosed, ORDERS_PATH, SendGate


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class ExposureTests(unittest.TestCase):
    def market(self, position='0', name='IBM', price='100', pnl='0'):
        return risk.MarketRisk(name, D(position), D(price), D('.1'), D(pnl))

    def order(self, state='OPEN', side='buy', remaining='1', market='IBM', price='100'):
        return risk.RiskOrder(market, side, D(remaining), D(price), state)

    def check(self, *, markets=None, orders=(), side='buy', quantity='1', price='100',
              equity='1000', available='1000', limits=None, reduce_only=False):
        # 这些用例独立验证持仓、账户与保证金边界，避免先被默认 600 美元硬上限截断。
        return risk.RiskManager(limits or risk.RiskLimits(hard_cap_usd=D('1200'))).check_order(
            'IBM', side, D(quantity), D(price), markets or [self.market()], orders,
            D(equity), D(available), reduce_only=reduce_only)

    def test_all_nonterminal_states_reserve_remaining_quantity(self):
        states = ('INTENT', 'SENT', 'ACKED', 'OPEN', 'PARTIAL', 'UNKNOWN', 'CANCEL_SENT')
        for state in states:
            with self.subTest(state=state):
                with self.assertRaises(risk.RiskRejected):
                    self.check(markets=[self.market('10')], orders=[self.order(state)],
                               quantity='1.0001')

    def test_exact_position_limit_allowed_and_one_increment_rejected(self):
        report = self.check(markets=[self.market('10')], orders=[self.order()])
        self.assertEqual(report.exposures['IBM'], D('1200'))
        with self.assertRaises(risk.RiskRejected):
            self.check(markets=[self.market('10')], orders=[self.order()], quantity='1.001')

    def test_sell_side_includes_short_position(self):
        self.assertEqual(self.check(markets=[self.market('-10')],
                                   orders=[self.order(side='sell')], side='sell')
                         .exposures['IBM'], D('1200'))
        with self.assertRaises(risk.RiskRejected):
            self.check(markets=[self.market('-10')], orders=[self.order(side='sell')],
                       side='sell', quantity='1.001')

    def test_terminal_orders_release_reservation(self):
        for state in ('FILLED', 'CANCELED', 'REJECTED', 'CONFIRMED_ABSENT', 'ABSENT'):
            with self.subTest(state=state):
                self.assertEqual(self.check(markets=[self.market('11')],
                                            orders=[self.order(state)]).account_exposure,
                                 D('1200'))

    def test_opposite_orders_do_not_net_each_other(self):
        report = self.check(orders=[self.order(remaining='7'),
                                   self.order(side='sell', remaining='8')])
        self.assertEqual(report.exposures['IBM'], D('800'))

    def test_accounts_sum_market_worst_cases_without_cross_market_netting(self):
        markets = [self.market('5'), self.market('-6', name='MSFT')]
        self.assertEqual(self.check(markets=markets).account_exposure, D('1200'))
        with self.assertRaises(risk.RiskRejected):
            self.check(markets=markets, quantity='1.001')

    def test_margin_uses_larger_side_plus_floating_loss(self):
        report = self.check(markets=[self.market(pnl='-10')],
                            orders=[self.order(remaining='2'),
                                    self.order(side='sell', remaining='5')])
        self.assertEqual(report.margin_required, D('60'))
        self.assertEqual(report.margin_ratio, D('.06'))

    def test_margin_boundary_allowed_then_rejected(self):
        self.assertEqual(self.check(quantity='3', equity='100').margin_ratio, D('.3'))
        with self.assertRaises(risk.RiskRejected):
            self.check(quantity='3.001', equity='100')

    def test_available_balance_checks_incremental_margin(self):
        self.check(markets=[self.market('5')], quantity='1', available='10')
        with self.assertRaises(risk.RiskRejected):
            self.check(markets=[self.market('5')], quantity='1.001', available='10')

    def test_higher_order_price_is_conservative(self):
        with self.assertRaises(risk.RiskRejected):
            self.check(markets=[self.market('10')],
                       orders=[self.order(remaining='1', price='150')])

    def test_adding_side_reserves_minimum_increment(self):
        manager = risk.RiskManager(risk.RiskLimits())
        quantity = manager.limit_adding_quantity('IBM', 'buy', D('4'), D('.01'),
                                                [self.market('4')], [self.order()])
        self.assertEqual(quantity, D('.99'))
        self.assertEqual(manager.limit_adding_quantity('IBM', 'sell', D('4'), D('.01'),
                                                       [self.market('4')], []), D('4'))

    def test_adding_quantity_floors_to_increment_and_never_negative(self):
        manager = risk.RiskManager(risk.RiskLimits())
        self.assertEqual(manager.limit_adding_quantity('IBM', 'buy', D('4'), D('.03'),
                                                       [self.market('4.991')], []), D('.96'))
        self.assertEqual(manager.limit_adding_quantity('IBM', 'buy', D('4'), D('.01'),
                                                       [self.market('6')], []), D('0'))

    def test_adding_quantity_uses_proposed_price_for_budget(self):
        manager = risk.RiskManager(risk.RiskLimits())
        quantity = manager.limit_adding_quantity('IBM', 'buy', D('4'), D('.01'),
                                                [self.market('4')], [self.order()],
                                                price=D('200'))
        self.assertEqual(quantity, D('.49'))

    def test_opposite_side_that_flips_position_is_limited_by_final_signed_endpoint(self):
        manager = risk.RiskManager(risk.RiskLimits(quote_notional=D('800')))
        for position, side in (('1', 'sell'), ('-1', 'buy')):
            with self.subTest(position=position, side=side):
                quantity = manager.limit_adding_quantity('IBM', side, D('8'), D('.01'),
                                                         [self.market(position)], [])
                self.assertEqual(quantity, D('6.99'))
                report = manager.check_order('IBM', side, quantity, D('100'),
                    [self.market(position)], [], D('1000'), D('1000'))
                self.assertEqual(report.exposures['IBM'], D('599'))

    def test_true_reduction_remains_available_above_hard_cap(self):
        manager = risk.RiskManager(risk.RiskLimits())
        for position, side in (('8', 'sell'), ('-8', 'buy')):
            with self.subTest(position=position, side=side):
                self.assertEqual(manager.limit_adding_quantity('IBM', side, D('4'), D('.01'),
                                                               [self.market(position)], []),
                                 D('4'))

    def test_opposite_side_limits_include_existing_orders_and_proposed_price(self):
        manager = risk.RiskManager(risk.RiskLimits(quote_notional=D('800')))
        quantity = manager.limit_adding_quantity('IBM', 'sell', D('8'), D('.01'),
            [self.market('1')], [self.order(side='sell', remaining='2')], price=D('200'))
        self.assertEqual(quantity, D('2.49'))

    def test_hard_limits_have_no_bypass(self):
        for name, value in (('quote_notional', '800.01'), ('max_position_usd', '1200.01'),
                            ('max_account_exposure_usd', '1500.01')):
            with self.subTest(name=name), self.assertRaises(risk.RiskError):
                risk.RiskLimits(**{name: D(value)})
        with self.assertRaises(TypeError):
            risk.RiskLimits(bypass=True)

    def test_bad_numbers_unknown_order_states_or_missing_markets_fail_closed(self):
        for quantity in ('NaN', 'Infinity', '-1', '0'):
            with self.subTest(quantity=quantity), self.assertRaises(risk.RiskError):
                self.check(quantity=quantity)
        with self.assertRaises(risk.RiskError):
            self.order(state='MYSTERY')
        with self.assertRaises(risk.RiskError):
            self.check(orders=[self.order(market='MSFT')])
        with self.assertRaises(risk.RiskError):
            self.check(equity='0')

    def test_reduce_only_cannot_flip_or_add_but_can_exceed_quote_amount(self):
        self.check(markets=[self.market('10')], side='sell', quantity='9', reduce_only=True)
        for side, quantity in (('buy', '1'), ('sell', '11')):
            with self.subTest(side=side), self.assertRaises(risk.RiskRejected):
                self.check(markets=[self.market('10')], side=side, quantity=quantity,
                           reduce_only=True)
        with self.assertRaises(risk.RiskRejected):
            self.check(quantity='5')


class MemoryFS:
    """模拟 open/write/fsync/rename/flock，不触碰真实文件系统。"""

    def __init__(self):
        self.files, self.handles, self.events, self.locks = {}, {}, [], set()
        self.next_fd = 10
        self.fail_fsync = None
        self.fail_write = False

    def makedirs(self, path, exist_ok=False):
        self.events.append(('mkdir', path))

    def open(self, path, flags, mode=0o777):
        path = str(path)
        if flags & os.O_CREAT:
            if flags & os.O_EXCL and path in self.files:
                raise FileExistsError(path)
            self.files.setdefault(path, b'')
        elif not flags & getattr(os, 'O_DIRECTORY', 0) and path not in self.files:
            raise FileNotFoundError(path)
        fd = self.next_fd
        self.next_fd += 1
        self.handles[fd] = [path, 0]
        self.events.append(('open', path, flags, mode))
        return fd

    def write(self, fd, payload):
        if self.fail_write:
            raise OSError('模拟磁盘满')
        path = self.handles[fd][0]
        self.files[path] = self.files[path] + payload
        self.events.append(('write', path))
        return len(payload)

    def read(self, fd, size):
        path, offset = self.handles[fd]
        data = self.files[path][offset:offset + size]
        self.handles[fd][1] += len(data)
        return data

    def fsync(self, fd):
        path = self.handles[fd][0]
        self.events.append(('fsync', path))
        if self.fail_fsync == path:
            raise OSError('模拟同步失败')

    def close(self, fd):
        self.events.append(('close', self.handles[fd][0]))
        self.handles.pop(fd)

    def replace(self, source, target):
        self.files[target] = self.files.pop(source)
        self.events.append(('replace', source, target))

    def unlink(self, path):
        self.files.pop(path, None)

    def flock(self, fd, operation):
        path = self.handles[fd][0]
        if operation & risk.fcntl.LOCK_UN:
            self.locks.remove(path)
        elif path in self.locks:
            raise BlockingIOError(errno.EAGAIN, '已有实例')
        else:
            self.locks.add(path)


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.fs = MemoryFS()
        self.files = risk.DurableFiles(fs=self.fs, flock=self.fs.flock)

    def test_halt_file_and_parent_directory_are_fsynced(self):
        self.files.create_halt('/maker/halt', {'reason': '超限'})
        self.assertEqual(json.loads(self.files.read_text('/maker/halt')), {'reason': '超限'})
        self.assertEqual([row[1] for row in self.fs.events if row[0] == 'fsync'],
                         ['/maker/halt', '/maker'])

    def test_existing_halt_is_never_overwritten(self):
        self.files.create_halt('/maker/halt', {'reason': '首次'})
        with self.assertRaises(FileExistsError):
            self.files.create_halt('/maker/halt', {'reason': '第二次'})
        self.assertEqual(json.loads(self.files.read_text('/maker/halt'))['reason'], '首次')

    def test_single_instance_refuses_second_then_releases_without_unlink(self):
        first = risk.SingleInstanceLock('/maker/account.lock', files=self.files)
        with first:
            with self.assertRaises(risk.AlreadyRunning):
                risk.SingleInstanceLock('/maker/account.lock', files=self.files).acquire()
        with risk.SingleInstanceLock('/maker/account.lock', files=self.files):
            self.assertTrue(self.files.exists('/maker/account.lock'))
        self.assertIn('/maker/account.lock', self.fs.files)
        self.assertFalse(self.fs.handles)

    def test_window_pnl_includes_fees_and_funding_and_survives_restart(self):
        ledger = risk.WindowLossLedger('2026-W45', '/maker/window', files=self.files)
        self.assertTrue(ledger.record('fill-1', realized_pnl='-4', fee='1', funding='-.5'))
        self.assertFalse(ledger.record('fill-1', realized_pnl='-4', fee='1', funding='-.5'))
        ledger.set_unrealized('-2', source_ms=100)
        recovered = risk.WindowLossLedger('2026-W45', '/maker/window', files=self.files)
        self.assertEqual(recovered.loss_usd, D('7.5'))
        self.assertFalse(recovered.exceeds('7.5'))
        self.assertTrue(recovered.exceeds('7.499'))
        self.assertFalse(recovered.record('fill-1', realized_pnl='-4', fee='1', funding='-.5'))

    def test_midnight_cannot_reset_window_or_event_deduplication(self):
        ledger = risk.WindowLossLedger('same-window', '/maker/window', files=self.files)
        ledger.record('before-midnight', realized_pnl='-4')
        ledger.record('after-midnight', realized_pnl='-3')
        restarted = risk.WindowLossLedger('same-window', '/maker/window', files=self.files)
        self.assertEqual(restarted.loss_usd, D('7'))
        with self.assertRaises(risk.RiskError):
            risk.WindowLossLedger('another-window', '/maker/window', files=self.files)

    def test_late_unrealized_snapshot_does_not_overwrite_newer_loss(self):
        ledger = risk.WindowLossLedger('window', '/maker/window', files=self.files)
        self.assertTrue(ledger.set_unrealized('-10', source_ms=200))
        self.assertFalse(ledger.set_unrealized('50', source_ms=100))
        self.assertEqual(ledger.loss_usd, D('10'))
        with self.assertRaises(risk.RiskError):
            ledger.set_unrealized('-11', source_ms=200)

    def test_conflicting_duplicate_event_or_corrupt_state_fails_closed(self):
        ledger = risk.WindowLossLedger('window', '/maker/window', files=self.files)
        ledger.record('fill', realized_pnl='-1')
        with self.assertRaises(risk.RiskError):
            ledger.record('fill', realized_pnl='-2')
        self.fs.files['/maker/window'] = b'{'
        with self.assertRaises(risk.RiskError):
            risk.WindowLossLedger('window', '/maker/window', files=self.files)

    def test_failed_persistence_does_not_commit_ledger_in_memory(self):
        ledger = risk.WindowLossLedger('window', '/maker/window', files=self.files)
        self.fs.fail_write = True
        with self.assertRaises(OSError):
            ledger.record('fill', realized_pnl='-5')
        self.assertEqual(ledger.loss_usd, D('0'))

    def test_rebate_is_signed_fee_and_survives_restart(self):
        ledger = risk.WindowLossLedger('window', '/maker/window', files=self.files)
        ledger.record('fill', realized_pnl='-10', fee='-2', funding='1')
        recovered = risk.WindowLossLedger('window', '/maker/window', files=self.files)
        self.assertEqual(recovered.loss_usd, D('7'))

    def test_boolean_financial_fields_are_rejected(self):
        for factory in (lambda: risk.RiskLimits(quote_notional=True),
                        lambda: risk.MarketRisk('IBM', True, 100),
                        lambda: risk.MarketRisk('IBM', 0, 100, True),
                        lambda: risk.RiskOrder('IBM', 'buy', True, 100, 'OPEN')):
            with self.subTest(factory=factory), self.assertRaises(risk.RiskError):
                factory()
        ledger = risk.WindowLossLedger('window', '/maker/window', files=self.files)
        with self.assertRaises(risk.RiskError):
            ledger.record('fill', fee=True)


class FakeGate:
    def __init__(self, events):
        self.events, self.closed, self.finished = events, False, False

    @property
    def is_open(self):
        return not self.closed

    def close(self):
        self.closed = True
        self.events.append(('gate_close',))

    def finish_stop(self):
        self.finished = True
        self.events.append(('gate_finish',))


class GuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock, self.events = FakeClock(), []

        async def stop(reason):
            self.events.append(('stop', reason))
            return True

        self.guard = risk.Guard(10, 5, stop=stop,
            block=lambda: self.events.append(('block',)),
            resume=lambda: self.events.append(('resume',)), monotonic=self.clock)

    async def test_guard_blocks_before_stop_and_waits_cooldown_then_stability(self):
        await self.guard.trigger('盘口过期')
        self.assertEqual(self.events, [('block',), ('stop', '盘口过期')])
        self.clock.now = 9
        self.assertFalse(await self.guard.observe(True))
        self.clock.now = 10
        self.assertFalse(await self.guard.observe(True))
        self.assertEqual(self.guard.state, 'STABILIZING')
        self.clock.now = 14.999
        self.assertFalse(await self.guard.observe(True))
        self.clock.now = 15
        self.assertTrue(await self.guard.observe(True))
        self.assertEqual(self.events[-1], ('resume',))

    async def test_new_trigger_resets_cooldown_and_continuous_stable_timer(self):
        await self.guard.trigger('断线')
        self.clock.now = 10
        await self.guard.observe(True)
        self.clock.now = 13
        await self.guard.observe(False, '再次断线')
        self.clock.now = 23
        self.assertFalse(await self.guard.observe(True))
        self.clock.now = 28
        self.assertTrue(await self.guard.observe(True))

    async def test_failed_stop_cannot_allow_guard_recovery(self):
        async def fail(reason):
            return False

        guard = risk.Guard(1, 1, stop=fail, monotonic=self.clock)
        await guard.trigger('未确认撤单')
        self.clock.now = 100
        self.assertFalse(await guard.observe(True))
        self.assertEqual(guard.state, 'STOP_FAILED')

    async def test_old_delayed_stop_cannot_override_newer_failed_stop(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def stop(reason):
            if reason == '首次':
                entered.set()
                await release.wait()
                return True
            return False

        guard = risk.Guard(1, 1, stop=stop, monotonic=self.clock)
        old = asyncio.create_task(guard.trigger('首次'))
        await entered.wait()
        await guard.trigger('更新触发')
        release.set()
        await old
        self.assertEqual(guard.state, 'STOP_FAILED')


class HaltTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fs, self.clock = MemoryFS(), FakeClock()
        self.files = risk.DurableFiles(fs=self.fs, flock=self.fs.flock)
        self.gate = FakeGate(self.fs.events)
        self.alerts = []

        async def alert(message):
            self.alerts.append(message)
            self.fs.events.append(('alert',))

        self.alert = alert

    def halt(self, stop):
        return risk.HaltController('/maker/halt', gate=self.gate, stop=stop,
            alert=self.alert, files=self.files, clock=self.clock)

    async def test_halt_closes_before_disk_and_fsyncs_before_first_cancel(self):
        async def stop(reason):
            self.assertTrue(self.gate.closed)
            self.assertIn(('fsync', '/maker/halt'), self.fs.events)
            self.assertIn(('fsync', '/maker'), self.fs.events)
            self.fs.events.append(('cancel',))
            return True

        halt = self.halt(stop)
        result = await halt.trigger('亏损超限')
        self.assertEqual(self.fs.events[0], ('gate_close',))
        self.assertTrue(result.persisted)
        self.assertEqual(result.exit_code, 76)
        self.assertTrue(self.gate.finished)
        self.assertTrue(self.alerts)
        with self.assertRaises(risk.RiskError):
            halt.assert_can_start()

    async def test_write_and_directory_fsync_failures_still_stop_alert_exit_76(self):
        for failure in ('write', '/maker/halt', '/maker'):
            with self.subTest(failure=failure):
                self.setUp()
                self.fs.fail_write = failure == 'write'
                self.fs.fail_fsync = failure

                async def stop(reason):
                    self.fs.events.append(('cancel',))
                    return True

                result = await self.halt(stop).trigger('锁失败')
                self.assertFalse(result.persisted)
                self.assertEqual(result.exit_code, 76)
                self.assertIn(('cancel',), self.fs.events)
                self.assertTrue(self.alerts)
                self.assertTrue(self.gate.finished)

    async def test_cancelled_trigger_finishes_stop_and_repeated_trigger_is_idempotent(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def stop(reason):
            entered.set()
            await release.wait()
            self.fs.events.append(('cancel',))
            return True

        halt = self.halt(stop)
        triggering = asyncio.create_task(halt.trigger('超限'))
        await entered.wait()
        triggering.cancel()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await triggering
        result = await halt.trigger('重复通知')
        self.assertTrue(result.persisted)
        self.assertEqual(self.fs.events.count(('cancel',)), 1)
        self.assertEqual(len(self.alerts), 1)
        self.assertTrue(self.gate.finished)

    async def test_stop_or_alert_failure_still_completely_closes_gate(self):
        async def stop(reason):
            raise OSError('撤单失败')

        async def alert(message):
            raise OSError('告警失败')

        halt = self.halt(stop)
        halt.alert = alert
        result = await halt.trigger('超限')
        self.assertTrue(self.gate.finished)
        self.assertFalse(result.stopped)
        self.assertEqual(result.exit_code, 76)

    async def test_repeated_cancellation_cannot_interrupt_halt_cleanup(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def stop(reason):
            entered.set()
            await release.wait()
            self.fs.events.append(('cancel',))
            return True

        halt = self.halt(stop)
        triggering = asyncio.create_task(halt.trigger('超限'))
        await entered.wait()
        triggering.cancel()
        await asyncio.sleep(0)
        triggering.cancel()
        await asyncio.sleep(0)
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await triggering
        self.assertIn(('cancel',), self.fs.events)
        self.assertTrue(self.alerts)
        self.assertTrue(self.gate.finished)


class MonitorHaltTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fs, self.clock = MemoryFS(), FakeClock()
        self.files = risk.DurableFiles(fs=self.fs, flock=self.fs.flock)
        events = self.fs.events

        class RecordedGate(SendGate):
            def halt_trading(self):
                events.append(('halt_trading',))
                super().halt_trading()

        self.gate, self.alerts = RecordedGate(), []

        async def alert(message):
            self.alerts.append(message)
        self.alert = alert

    def halt(self, stop):
        return risk.HaltController('/maker/halt', gate=self.gate, stop=stop,
            alert=self.alert, files=self.files, clock=self.clock, monitor_after_halt=True)

    async def test_success_halts_posts_before_disk_and_keeps_monitoring_after_fsync_and_stop(self):
        async def stop(reason):
            self.assertTrue(self.gate.is_open)
            with self.assertRaises(GateClosed):
                self.gate.check_rest('POST', ORDERS_PATH)
            self.assertIn(('fsync', '/maker/halt'), self.fs.events)
            self.assertIn(('fsync', '/maker'), self.fs.events)
            self.fs.events.append(('cancel',))
            return True

        controller = self.halt(stop)
        result = await controller.trigger('窗口亏损超过上限')
        self.assertEqual(self.fs.events[0], ('halt_trading',))
        self.assertTrue(result.persisted)
        self.assertTrue(result.stopped)
        self.assertEqual(result.exit_code, 76)
        self.assertEqual(controller.exit_code, 76)
        self.assertTrue(self.gate.is_open)
        self.gate.check_rest('GET', BALANCES_PATH)
        with self.assertRaises(GateClosed):
            self.gate.check_rest('POST', ORDERS_PATH)
        with self.assertRaises(risk.RiskError):
            controller.assert_can_start()

    async def test_write_or_fsync_failure_permanently_seals_all_communication(self):
        for failure in ('write', '/maker/halt', '/maker'):
            with self.subTest(failure=failure):
                self.setUp()
                self.fs.fail_write = failure == 'write'
                self.fs.fail_fsync = failure

                async def stop(reason):
                    self.fs.events.append(('cancel',))
                    return True

                result = await self.halt(stop).trigger('锁写入失败')
                self.assertFalse(result.persisted)
                self.assertIn(('cancel',), self.fs.events)
                self.assertFalse(self.gate.is_open)
                with self.assertRaises(GateClosed):
                    self.gate.check_rest('GET', BALANCES_PATH)
                with self.assertRaises(GateClosed):
                    self.gate.check_rest('DELETE', ORDERS_PATH+'?market=IBM', stop=True)

    async def test_unconfirmed_or_failed_stop_permanently_seals_gate(self):
        for raises in (False, True):
            with self.subTest(raises=raises):
                self.setUp()

                async def stop(reason):
                    if raises:
                        raise OSError('停止失败')
                    return False

                result = await self.halt(stop).trigger('停止未确认')
                self.assertTrue(result.persisted)
                self.assertFalse(result.stopped)
                self.assertFalse(self.gate.is_open)
                with self.assertRaises(GateClosed):
                    self.gate.check_rest('GET', BALANCES_PATH)

    async def test_watchdog_seal_cannot_be_reopened_by_successful_monitor_halt(self):
        async def stop(reason):
            return True

        async def ping():
            self.fail('不健康时不能续期')

        watchdog = risk.Watchdog(gate=self.gate, stop=stop, ping=ping,
            alert=self.alert, reconciler_interval_s=30, monotonic=self.clock)
        self.assertFalse(await watchdog.check_once())
        self.assertFalse(self.gate.is_open)
        result = await self.halt(stop).trigger('Watchdog 已封锁之后的风险熔断')
        self.assertTrue(result.persisted and result.stopped)
        self.assertFalse(self.gate.is_open)
        with self.assertRaises(GateClosed):
            self.gate.check_rest('GET', BALANCES_PATH)
        with self.assertRaises(GateClosed):
            self.gate.check_rest('DELETE', ORDERS_PATH+'?market=IBM', stop=True)


class WatchdogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock, self.events = FakeClock(), []
        self.gate = FakeGate(self.events)

        async def stop(reason):
            self.events.append(('stop', reason))
            return True

        async def ping():
            self.events.append(('ping',))

        async def alert(reason):
            self.events.append(('alert', reason))

        self.stop, self.ping, self.alert = stop, ping, alert

    def watchdog(self, **kwargs):
        async def sleep(delay):
            await asyncio.Future()

        kwargs.setdefault('sleep', sleep)
        return risk.Watchdog(gate=self.gate, stop=self.stop, ping=self.ping,
            alert=self.alert, reconciler_interval_s=300, monotonic=self.clock, **kwargs)

    def healthy(self, watchdog, *, pong=True, reconciler=True):
        watchdog.beat('Strategy')
        watchdog.beat('OrderManager')
        if reconciler:
            watchdog.beat('Reconciler')
        if pong:
            watchdog.pong()

    async def test_missing_task_heartbeat_fails_closed_without_first_ping(self):
        watchdog = self.watchdog()
        watchdog.beat('Strategy')
        self.assertFalse(await watchdog.check_once())
        self.assertTrue(self.gate.closed)
        self.assertTrue(self.gate.finished)
        self.assertFalse(any(row[0] == 'ping' for row in self.events))
        self.assertEqual(self.events[0], ('gate_close',))

    async def test_individual_reconciler_threshold_is_its_period_plus_30(self):
        watchdog = self.watchdog()
        self.healthy(watchdog)
        self.clock.now = 330
        self.healthy(watchdog, reconciler=False)
        self.assertTrue(await watchdog.check_once())
        self.clock.now = 330.001
        self.healthy(watchdog, reconciler=False)
        self.assertFalse(await watchdog.check_once())
        self.assertIn('Reconciler', watchdog.failure_reason)
        self.assertEqual(self.events.count(('ping',)), 1)

    async def test_strategy_and_order_manager_expire_only_above_15_seconds(self):
        for name in ('Strategy', 'OrderManager'):
            with self.subTest(name=name):
                self.setUp()
                watchdog = self.watchdog()
                self.healthy(watchdog)
                self.clock.now = 15
                watchdog.pong()
                self.assertTrue(await watchdog.check_once())
                self.clock.now = 15.001
                watchdog.beat('OrderManager' if name == 'Strategy' else 'Strategy')
                watchdog.pong()
                self.assertFalse(await watchdog.check_once())
                self.assertIn(name, watchdog.failure_reason)

    async def test_private_feed_business_activity_cannot_replace_pong(self):
        watchdog = self.watchdog()
        self.healthy(watchdog)
        self.clock.now = 16
        self.healthy(watchdog, pong=False)
        with self.assertRaises(risk.RiskError):
            watchdog.beat('PrivateFeed')
        self.assertFalse(await watchdog.check_once())
        self.assertIn('PrivateFeed', watchdog.failure_reason)
        self.assertNotIn(('ping',), self.events)

    async def test_pending_stop_prevents_concurrent_check_from_renewing(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def stop(reason):
            self.assertTrue(self.gate.closed)
            entered.set()
            await release.wait()
            return True

        self.stop = stop
        watchdog = self.watchdog()
        self.healthy(watchdog)
        self.clock.now = 16
        stopping = asyncio.create_task(watchdog.check_once())
        await entered.wait()
        self.healthy(watchdog)
        self.assertFalse(await watchdog.check_once())
        self.assertNotIn(('ping',), self.events)
        release.set()
        self.assertFalse(await stopping)
        self.assertTrue(self.gate.finished)

    async def test_stop_failure_still_finishes_gate_and_never_recovers_implicitly(self):
        async def stop(reason):
            raise OSError('撤单失败')

        self.stop = stop
        watchdog = self.watchdog()
        self.assertFalse(await watchdog.check_once())
        self.healthy(watchdog)
        self.assertFalse(await watchdog.check_once())
        self.assertTrue(self.gate.finished)
        self.assertNotIn(('ping',), self.events)

    async def test_ping_failure_triggers_local_stop(self):
        async def ping():
            raise OSError('发送失败')

        self.ping = ping
        watchdog = self.watchdog()
        self.healthy(watchdog)
        self.assertFalse(await watchdog.check_once())
        self.assertTrue(self.gate.finished)
        self.assertIn('续期', watchdog.failure_reason)

    async def test_run_uses_injected_sleep_and_checks_every_ten_seconds(self):
        async def sleep(delay):
            if delay == 5:
                await asyncio.Future()
                return
            self.assertEqual(delay, 10)
            self.clock.now += 16

        watchdog = self.watchdog(sleep=sleep)
        self.healthy(watchdog)
        await watchdog.run()
        self.assertEqual(self.events.count(('ping',)), 1)
        self.assertTrue(self.gate.finished)

    async def test_repeated_cancellation_cannot_interrupt_watchdog_stop(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def stop(reason):
            entered.set()
            await release.wait()
            self.events.append(('cancel',))
            return True

        self.stop = stop
        watchdog = self.watchdog()
        checking = asyncio.create_task(watchdog.check_once())
        await entered.wait()
        checking.cancel()
        await asyncio.sleep(0)
        checking.cancel()
        await asyncio.sleep(0)
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await checking
        self.assertIn(('cancel',), self.events)
        self.assertTrue(self.gate.finished)

    async def test_hung_ping_timeout_closes_gate_without_waiting_for_cancel_cooperation(self):
        entered, release, timer = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def ping():
            self.events.append(('ping',))
            entered.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    self.events.append(('ping_ignored_cancel',))

        async def sleep(delay):
            self.assertEqual(delay, 5)
            await timer.wait()

        self.ping = ping
        watchdog = self.watchdog(sleep=sleep)
        self.healthy(watchdog)
        checking = asyncio.create_task(watchdog.check_once())
        await entered.wait()
        self.clock.now = 16
        timer.set()
        # 若实现仍在无界等待 ping，用事件循环让测试给出明确断言后再收尾。
        for _ in range(10):
            await asyncio.sleep(0)
        try:
            self.assertTrue(checking.done(), '续期发送卡住时必须有界执行本地停止')
            self.assertFalse(await checking)
            self.assertTrue(self.gate.closed)
            self.assertTrue(self.gate.finished)
            self.assertIn('续期', watchdog.failure_reason)
            self.assertFalse(await watchdog.check_once())
            self.assertEqual(self.events.count(('ping',)), 1)
        finally:
            release.set()
            await checking

    async def test_cancelled_healthy_watchdog_run_closes_gate_before_exiting(self):
        entered = asyncio.Event()

        async def sleep(delay):
            if delay == 10:
                entered.set()
            await asyncio.Future()

        watchdog = self.watchdog(sleep=sleep)
        self.healthy(watchdog)
        running = asyncio.create_task(watchdog.run())
        await entered.wait()
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running
        self.assertTrue(self.gate.closed)
        self.assertTrue(self.gate.finished)
        self.assertTrue(any(row[0] == 'stop' for row in self.events))

    async def test_cancelled_ping_check_closes_gate_before_exiting(self):
        entered = asyncio.Event()

        async def ping():
            entered.set()
            await asyncio.Future()

        self.ping = ping
        watchdog = self.watchdog()
        self.healthy(watchdog)
        checking = asyncio.create_task(watchdog.check_once())
        await entered.wait()
        checking.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await checking
        self.assertTrue(self.gate.finished)

    async def test_unexpected_watchdog_sleep_failure_stops_before_propagating(self):
        async def sleep(delay):
            if delay == 10:
                raise OSError('定时器失败')
            await asyncio.Future()

        watchdog = self.watchdog(sleep=sleep)
        self.healthy(watchdog)
        with self.assertRaises(OSError):
            await watchdog.run()
        self.assertTrue(self.gate.closed)
        self.assertTrue(self.gate.finished)


class ExchangeMarginRatioTests(unittest.TestCase):
    def check(self, ratio, *, quantity='1', available='1000', position='5', pnl='-30'):
        return risk.RiskManager().check_order('IBM', 'buy', quantity, '100',
            [risk.MarketRisk('IBM', position, '100', unrealized_pnl=pnl)], [],
            '200', available, exchange_margin_ratio=ratio)

    def test_exchange_ratio_overrides_higher_local_estimate(self):
        report = self.check('.1')
        self.assertEqual(report.margin_required, 90)
        self.assertEqual(report.margin_ratio, D('.15'))

    def test_exchange_ratio_blocks_when_local_estimate_would_allow(self):
        with self.assertRaises(risk.RiskRejected):
            self.check('.29', position='0', pnl='0')

    def test_exchange_ratio_exact_boundary_allowed(self):
        self.assertEqual(self.check('.25').margin_ratio, D('.3'))
        with self.assertRaises(risk.RiskRejected):
            self.check('.2501')

    def test_exchange_ratio_never_bypasses_available_margin(self):
        with self.assertRaises(risk.RiskRejected):
            self.check('.01', available='9')

    def test_invalid_exchange_ratio_fails_closed(self):
        for ratio in ('NaN', 'Infinity', '-.1', True, 'not-a-number'):
            with self.subTest(ratio=ratio), self.assertRaises(risk.RiskError):
                self.check(ratio)

    def test_missing_exchange_ratio_keeps_conservative_estimate(self):
        with self.assertRaises(risk.RiskRejected):
            self.check(None)


class EmergencyReductionRiskTests(unittest.TestCase):
    def check(self, *, position='20', side='sell', quantity='5', price='100',
              orders=(), reduce_only=True, ratio='.8'):
        return risk.RiskManager().check_order('IBM', side, quantity, price,
            [risk.MarketRisk('IBM', position, '100')], orders, '100', '0',
            reduce_only=reduce_only, exchange_margin_ratio=ratio)

    def test_true_reduction_with_no_live_orders_is_allowed_above_existing_limits(self):
        for position, side in (('20', 'sell'), ('-20', 'buy')):
            with self.subTest(position=position):
                report = self.check(position=position, side=side)
                self.assertEqual(report.account_exposure, D(2000))
                self.assertEqual(report.margin_required, D(200))
                self.assertEqual(report.margin_ratio, D('.8'))
        self.assertEqual(self.check(ratio=None).margin_ratio, D(2))

    def test_ordinary_order_never_receives_emergency_exemption(self):
        with self.assertRaises(risk.RiskRejected):
            self.check(reduce_only=False, quantity='1')

    def test_live_order_or_exposure_increase_blocks_exemption(self):
        active = risk.RiskOrder('IBM', 'buy', D(1), D(100), 'OPEN')
        for orders in ((active,), iter((active,))):
            with self.subTest(orders=orders), self.assertRaises(risk.RiskRejected):
                self.check(orders=orders)
        with self.assertRaises(risk.RiskRejected):
            self.check(price='1000')
        for side, quantity in (('buy', '5'), ('sell', '21')):
            with self.subTest(side=side), self.assertRaises(risk.RiskRejected):
                self.check(side=side, quantity=quantity)

    def test_only_actual_boolean_reduce_only_can_receive_exemption(self):
        for value in ('true', 1):
            with self.subTest(value=value), self.assertRaises(risk.RiskError):
                self.check(reduce_only=value)
