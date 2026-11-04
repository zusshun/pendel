"""M26 notification integration uses real Maker and Alerter with in-memory HTTP."""

import asyncio
import unittest
from dataclasses import replace
from decimal import Decimal as D

from pendle.alerts import Alerter, TelegramCredentials
from pendle.maker import HEALTHCHECK_WAIVER_WARNING, Maker, MarketRules
from pendle.maker_config import MakerSettings
from pendle.maker_orders import AccountState
from pendle.ondo_api import HttpResponse
from test_m5_observations import MemoryLogger
from test_maker import MARKET, book
from test_maker_e2e import Clock, Files


class M26MakerAlertTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock, self.files = Clock(), Files()
        self.sent, self.summaries = [], []

        async def http(method, url, *, json=None, timeout=10):
            self.sent.append((method, json))
            return HttpResponse(200, {'ok': True})

        self.alerter = Alerter(TelegramCredentials('dummy-token', 'dummy-chat'),
                               http=http, monotonic=self.clock)
        observe = self.alerter.observe

        async def record_summary(**kwargs):
            self.summaries.append(kwargs['summary'])
            await observe(**kwargs)

        self.alerter.observe = record_summary
        self.bot = Maker(replace(MakerSettings(), gate_interval_s=.01,
                                 cooldown_s=.01, stable_s=.01),
                         api=None, alerter=self.alerter, files=self.files,
                         logger=MemoryLogger(), rules=MarketRules('.01', '.001'),
                         monotonic=self.clock, clock=self.clock.wall,
                         sleep=self.clock.sleep)

    async def asyncTearDown(self):
        await asyncio.gather(*self.alerter._background_tasks)

    def refresh(self, *, eligible):
        self.bot.engine.settings = replace(self.bot.settings,
            gate_comp_max_usd=D('3500') if eligible else D(1))
        self.bot.book = book(at=int(self.clock.wall()*1000))
        self.bot.book_seen = self.clock()
        self.bot.engine.mark = (D(100), self.clock())
        self.bot.engine.in_price_discovery = False
        self.bot.engine.price_discovery_seen = self.clock()

    async def monitor_once(self):
        async def finish_iteration(delay):
            self.bot.stop_event.set()

        self.bot.sleep = finish_iteration
        await self.bot.monitor_loop()
        self.bot.stop_event.clear()
        await asyncio.sleep(0)
        return self.summaries[-1]

    async def test_gate_flips_produce_no_telegram_http_calls(self):
        statuses = []
        for eligible in (True, False, True, False, True):
            self.refresh(eligible=eligible)
            await self.bot.tick()
            await asyncio.sleep(0)
            statuses.append(self.bot.last_gate_status)
            self.clock.now += 1
        self.assertEqual(statuses, [True, False, True, False, True])
        self.assertEqual(self.sent, [])

    async def test_summary_inputs_preserve_signed_window_pnl_and_deduplicate_market_fills(self):
        self.bot.ledger = self.bot.ledger_for_window('2026-11-07T09:00:00+08:00')
        self.bot.ledger.record('profitable-fill', realized_pnl='10', fee='1')
        self.bot.ledger.record('rebate', fee='-.25')
        self.bot.ledger.record('funding-income', funding='2')
        self.bot.ledger.record('funding-expense', funding='-.5')
        self.bot.ledger.set_unrealized('-999', source_ms=1)
        self.bot.last_position_rows = [
            {'market': MARKET, 'quantity': D('1.5'), 'unrealized_pnl': D('3.5')},
            {'market': 'OTHER-USD.P', 'quantity': D(1), 'unrealized_pnl': D('-100')}]
        state = AccountState([MARKET, 'OTHER-USD.P'], monotonic=self.clock)
        state.apply_fill('one', MARKET, 'buy', '1', 100)
        self.assertFalse(state.apply_fill('one', MARKET, 'buy', '1', 100))
        state.apply_fill('two', MARKET, 'buy', '.5', 101)
        state.apply_fill('other', 'OTHER-USD.P', 'buy', '1', 102)
        self.bot.state = state
        summary = await self.monitor_once()
        self.assertEqual(summary.get('持仓'), '1.5')
        self.assertEqual(summary.get('未实现盈亏'), '3.5')
        self.assertEqual(summary.get('窗口已实现盈亏'), '10.75')
        self.assertEqual(summary.get('窗口总盈亏'), '14.25')
        self.assertEqual(summary.get('成交数'), 2)

    async def test_summary_inputs_report_negative_pnl_without_loss_sign_inversion(self):
        self.bot.ledger = self.bot.ledger_for_window('2026-11-07T09:00:00+08:00')
        self.bot.ledger.record('losing-fill', realized_pnl='-4', fee='.5')
        self.bot.ledger.record('funding-expense', funding='-1')
        self.bot.last_position_rows = [
            {'market': MARKET, 'quantity': D(0), 'unrealized_pnl': D('-2')}]
        summary = await self.monitor_once()
        self.assertEqual(summary.get('窗口已实现盈亏'), '-5.5')
        self.assertEqual(summary.get('窗口总盈亏'), '-7.5')

    async def test_summary_inputs_include_cumulative_eligible_seconds(self):
        self.bot.record_quote_state(True)
        self.clock.now += 10
        self.bot.record_quote_state(False, '竞争量超过判据')
        self.clock.now += 20
        summary = await self.monitor_once()
        self.assertEqual(summary.get('有资格时长秒'), 10)
        self.assertEqual(summary['观测时长秒'], 30)
        self.assertAlmostEqual(summary['有资格时长占比'], 1/3)
        self.assertEqual(summary['守卫及停报原因']['竞争量超过判据']['时长秒'], 20)

    async def test_summary_inputs_separate_current_gate_from_paused_qualification_and_keep_waiver(self):
        self.refresh(eligible=True)
        await self.bot.tick()
        self.bot.settings = replace(self.bot.settings, mode='live', guards_calibrated=True,
                                    sandbox_verified=True, healthcheck_waived=True)
        self.bot.record_quote_state(False, '账户同步屏障')
        summary = await self.monitor_once()
        self.assertFalse(summary['判据'])
        self.assertIs(summary.get('当前判据'), True)
        self.assertEqual(D(summary['竞争量flat']), D(2000))
        self.assertEqual(D(summary['竞争量linear']), D(1200))
        self.assertEqual(summary['外部心跳风险'], HEALTHCHECK_WAIVER_WARNING)

    async def assert_summary_hides_stale_decision(self):
        decision = self.bot.last_decision
        summary = await self.monitor_once()
        self.assertIsNone(summary['当前判据'])
        self.assertIsNone(summary['竞争量flat'])
        self.assertIsNone(summary['竞争量linear'])
        self.assertIs(self.bot.last_decision, decision)
        health = next(row for row in reversed(self.bot.logger.rows) if row['事件'] == '健康状态')
        self.assertEqual(D(health['竞争量flat']), D(2000))
        self.assertEqual(D(health['竞争量linear']), D(1200))

    async def test_summary_does_not_report_cached_gate_after_book_expires(self):
        self.refresh(eligible=True)
        await self.bot.tick()
        self.clock.now += self.bot.settings.max_book_age_s+1
        await self.bot.tick()
        await self.assert_summary_hides_stale_decision()

    async def test_summary_does_not_report_cached_gate_outside_trading_window(self):
        self.refresh(eligible=True)
        await self.bot.tick()
        self.clock.epoch += 2*86400
        await self.bot.tick()
        await self.assert_summary_hides_stale_decision()

    async def test_summary_does_not_report_cached_gate_after_send_gate_closes(self):
        self.refresh(eligible=True)
        await self.bot.tick()
        self.bot.gate.close()
        await self.bot.tick()
        await self.assert_summary_hides_stale_decision()

    async def test_summary_does_not_report_old_decision_with_new_fresh_book(self):
        self.refresh(eligible=True)
        await self.bot.tick()
        self.clock.now += self.bot.settings.max_book_age_s+1
        self.refresh(eligible=True)
        await self.assert_summary_hides_stale_decision()

    async def test_summary_does_not_report_cached_gate_after_mark_expires(self):
        self.refresh(eligible=True)
        await self.bot.tick()
        self.bot.engine.mark = (D(100), self.clock()-self.bot.settings.mark_quiet_max_s-1)
        await self.assert_summary_hides_stale_decision()

    async def test_summary_does_not_report_cached_gate_after_discovery_expires(self):
        self.refresh(eligible=True)
        await self.bot.tick()
        self.bot.engine.price_discovery_seen -= self.bot.settings.discovery_quiet_max_s+1
        await self.assert_summary_hides_stale_decision()

    async def test_summary_does_not_report_cached_gate_while_trading_halted(self):
        self.refresh(eligible=True)
        await self.bot.tick()
        self.bot.gate.halt_trading()
        await self.assert_summary_hides_stale_decision()

    async def test_summary_keeps_fresh_gate_while_orders_stopping(self):
        self.refresh(eligible=True)
        await self.bot.tick()
        self.bot.state.stopping = True
        summary = await self.monitor_once()
        self.assertIs(summary['当前判据'], True)
        self.assertEqual(D(summary['竞争量flat']), D(2000))
        self.assertEqual(D(summary['竞争量linear']), D(1200))

    async def test_summary_reports_fresh_failed_gate_and_competition_after_orders_stop(self):
        self.refresh(eligible=False)
        await self.bot.tick()
        self.assertTrue(self.bot.state.stopping)
        self.assertFalse(self.bot.last_decision.eligible)
        summary = await self.monitor_once()
        self.assertIs(summary['当前判据'], False)
        self.assertEqual(D(summary['竞争量flat']), D(2000))
        self.assertEqual(D(summary['竞争量linear']), D(1200))
