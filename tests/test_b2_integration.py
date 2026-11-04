"""Verify B2 accounting in both hedging modes using real event replay rather than manually supplied fees."""

import unittest
from dataclasses import replace
from datetime import datetime
from decimal import Decimal as D

from pendle.backtest import run_backtest
from pendle.bt_config import BacktestSettings
from pendle.bt_report import build_report


class B2IntegrationTests(unittest.TestCase):
    def test_real_buy_and_sell_fills_reconcile_under_both_hedge_modes(self):
        start = int(datetime.fromisoformat('2026-10-31T01:00:00+00:00').timestamp()*1000)

        def ondo(offset, price, side):
            return dict(id=str(offset) + side, market='INTC-USD.P',
                time_ns=(start+offset)*1000000, price=D(price),
                size=D('.5'), cost=D(price)*D('.5'), aggressor_side=side)

        def binance(offset, price):
            return dict(agg_id=offset, symbol='INTCUSDT', transact_time=start+offset,
                        price=D(price), quantity=D(1), is_buyer_maker=False)

        settings = BacktestSettings(quote_notional=100, quote_offset_bps=100,
            max_inventory_usd=1000, data_latency_ms=100, place_latency_ms=0,
            cancel_latency_ms=0, requote_bps=10000, guard_move_bps=10000,
            hedge_latency_ms=500, band_bps=200, ondo_funding_bps_per_hour=1,
            lighter_funding_bps_per_hour=1, pickoff_enabled=False)
        reports = {}
        for mode in ('none','lighter_proxy'):
            with self.subTest(mode=mode):
                params = replace(settings, hedge_mode=mode)
                result = run_backtest([ondo(0,'100','buy'), ondo(0,'100','sell'),
                    ondo(1000,'98','sell'),
                    ondo(2000,'102','buy')], [binance(0,'100'),binance(300000,'101')],
                    params, symbol='INTC', start_ms=start, end_ms=start+360001)
                self.assertEqual([fill['side'] for fill in result['fills']], ['buy','sell'])
                report = build_report(result, params)
                reports[mode] = report
                parts = report['pnl_decomposition']
                self.assertTrue(parts['complete'])
                # The two-sided midpoint basis gives the second decision a center of 99.5 and the 300-second center a value of 101.
                self.assertEqual(parts['spread_capture'], D('1.25'))
                self.assertEqual(parts['adverse_selection'], D('-.25'))
                self.assertAlmostEqual(parts['spread_capture']+parts['adverse_selection']
                    +parts['inventory_drift']-parts['fees'], report['valuation']['mark']['pnl'], places=20)
                self.assertEqual(parts['edge_pnl'], parts['spread_capture']
                    +parts['adverse_selection']-parts['fees']-parts['hedge_cost'])
                base = next(row for row in report['sensitivity']
                            if row['at_price_fill_ratio'] == params.at_price_fill_ratio)
                self.assertEqual(base.get('pnl_edge'), parts['edge_pnl'])
                self.assertEqual(base['c_star']['edge'], report['valuation']['edge']['c_star'])
                self.assertGreater(result['totals']['maker_fees'], 0)
                if mode == 'lighter_proxy':
                    self.assertGreater(result['totals']['hedge_fees'], 0)
                    self.assertGreater(result['totals']['hedge_cost'], 0)
                else:
                    self.assertEqual(result['totals']['hedge_fees'], 0)
                    self.assertEqual(result['totals']['hedge_cost'], 0)
        self.assertLess(reports['lighter_proxy']['valuation']['edge']['pnl'],
                        reports['none']['valuation']['edge']['pnl'])
