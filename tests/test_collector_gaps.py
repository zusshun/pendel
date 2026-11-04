"""采集断档只影响依赖相应市场频道的回测，风险与时长完整披露。"""

import unittest
from dataclasses import replace
from decimal import Decimal as D

from pendle.backtest import run_backtest
from pendle.bt_cli import _backtests
from pendle.bt_config import BacktestSettings
from pendle.bt_report import build_report, markdown_summary
from pendle.history import HistoryStore


def ondo(at, identity=None, price='100', side='buy'):
    return dict(id=identity or str(at), market='INTC-USD.P', time_ns=at * 1000000,
                price=D(price), size=D('1'), cost=D(price), aggressor_side=side)


def binance(at):
    return dict(symbol='INTCUSDT', agg_id=at, transact_time=at, price=D('100'),
                quantity=D('1'), is_buyer_maker=False)


def gap(source='ondo', market='INTC-USD.P', channel='tradesPerps', start=0, end=5000):
    return dict(source=source, market=market, channel=channel,
                start_ms=start, end_ms=end, reason='断线')


class CollectorGapTests(unittest.TestCase):
    def setUp(self):
        self.settings = BacktestSettings(symbols=('INTC',), quote_notional=100,
            quote_offset_bps=100, max_inventory_usd=1000, data_latency_ms=0,
            place_latency_ms=0, cancel_latency_ms=0, max_ref_age_s=10000,
            guard_move_bps=10000, requote_bps=10000, reward_two_sided=False)

    def replay(self, gaps, settings=None, trades=(), end=10000):
        # 断档测试先提供双边成交，以满足中点基差的预热条件。
        return run_backtest([ondo(0, 'ask'), ondo(0, 'bid', side='sell'), *trades],
                            [binance(0)], settings or self.settings,
                            symbol='INTC', start_ms=0, end_ms=end, gaps=gaps)

    def test_gap_stops_new_orders_and_reopens_only_after_valid_message(self):
        result = self.replay([gap()])
        requested = [row['time_ms'] for row in result['events'] if row['type'] == 'order_sent']
        self.assertTrue(requested)
        self.assertTrue(all(at >= 5000 for at in requested))

    def test_unrelated_source_market_or_channel_does_not_pause_quotes(self):
        for unrelated in (gap(market='DRAM-USD.P'), gap(channel='contracts'),
                          gap(source='binance', market='INTCUSDT', channel='bookTicker'),
                          gap(source='lighter', market='INTC', channel='orderBookOrders')):
            with self.subTest(unrelated=unrelated):
                result = self.replay([unrelated])
                self.assertTrue(any(row['type'] == 'order_sent' and row['time_ms'] == 0
                                    for row in result['events']))
                self.assertEqual(result['coverage']['gaps'], [])

    def test_lighter_gap_pauses_only_proxy_hedging_mode(self):
        result = self.replay([gap('lighter', 'INTC', 'orderBookOrders')],
                             replace(self.settings, hedge_mode='lighter_proxy'))
        self.assertTrue(all(row['time_ms'] >= 5000 for row in result['events']
                            if row['type'] == 'order_sent'))

    def test_open_order_at_gap_start_marks_profit_unresolved_even_with_instant_cancel(self):
        result = self.replay([gap(start=1000)])
        self.assertFalse(result['coverage']['pnl_determinate'])

    def test_hedge_due_inside_gap_is_never_reported_as_successful(self):
        result = self.replay([gap(start=1000, end=5000)],
            replace(self.settings, hedge_mode='lighter_proxy', hedge_latency_ms=1000),
            [ondo(500, price='98', side='sell')])
        self.assertFalse(any(row['type'] == 'hedge' for row in result['events']))
        self.assertTrue(any(row['type'] == 'hedge_unresolved' for row in result['events']))
        self.assertFalse(result['coverage']['pnl_determinate'])

    def test_gap_duration_clips_open_intervals_and_deduplicates_sources(self):
        result = self.replay([gap(start=0, end=5000),
                              gap('binance', 'INTCUSDT', 'aggTrade', 2000, 7000),
                              gap(start=9000, end=None)])
        self.assertEqual(result['coverage'].get('gap_duration_ms'), 8000)
        self.assertEqual(result['coverage'].get('gap_duration_by_source_ms'),
                         {'ondo': 6000, 'binance': 5000})
        report = build_report(result, self.settings)
        self.assertIn('断档总时长：8 秒', markdown_summary(report))

    def test_cli_passes_lighter_gap_to_proxy_mode(self):
        with HistoryStore(':memory:') as store:
            store.upsert_contracts({'success': True, 'result': [dict(market='INTC-USD.P',
                quoteIncrement='.01', baseIncrement='.1')]}, 0, source='ondo_markets')
            store.upsert_ondo_trades([dict(id='a', market='INTC-USD.P',
                time='1970-01-01T00:00:00Z', price='100', size='1', cost='100',
                aggressor_side='buy')])
            store.upsert_binance_trades([{**binance(0), 'first_trade_id': 0, 'last_trade_id': 0}])
            store.upsert_collector_gaps([gap('lighter', 'INTC', 'orderBookOrders')])
            output = _backtests(store, self.settings, 0, 10000, include_events=True)
        reports = {row['hedge_mode']: row for row in output['reports']}
        self.assertEqual(reports['none']['coverage']['gaps'], [])
        self.assertEqual(len(reports['lighter_proxy']['coverage']['gaps']), 1)
