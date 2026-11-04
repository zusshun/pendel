"""B2 报告在真实时长、代理竞争量和部分缺样上的边界验证。"""

import copy
import unittest
from dataclasses import replace
from decimal import Decimal as D

from pendle.backtest import run_backtest
from pendle.bt_config import BacktestSettings
from pendle.bt_report import build_report
from tests.test_bt_report import ms, observation, result_fixture


def ondo(at, *, side='buy', ident=None):
    return {'id': ident or str(at), 'market': 'INTC-USD.P', 'time_ns': at * 1000000,
            'price': D(100), 'size': D(1), 'cost': D(100), 'aggressor_side': side}


def binance(at, price='100'):
    return {'agg_id': at, 'symbol': 'INTCUSDT', 'transact_time': at,
            'price': D(price), 'quantity': D(1), 'is_buyer_maker': False}


class B2ReportBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.settings = BacktestSettings(pool_usdc_per_week='10080', reward_markets=1,
                                         max_ref_age_s='120', comp_scenarios=(100,))

    def fixture(self):
        value = result_fixture('2026-10-31T01:00:00Z', '2026-10-31T01:07:00Z')
        first = value['minutes'][0]
        value['minutes'] = [{**first, 'time_ms': value['start_ms'] + i * 60000,
                             'pnl_mark': D(i) / 2, 'pnl_liquidate': D(i) / 3}
                            for i in range(7)]
        value['valuation'] = {'mark': {'pnl': D(3)}, 'liquidate': {'pnl': D(2)}}
        value['center_prices'] = [
            {'time_ns': (value['start_ms'] + offset) * 1000000,
             'price': None if price is None else D(price), 'binance_price': D(100)}
            for offset, price in ((0, '100'), (11000, '99'), (301000, '98'))]
        return value

    def test_engine_online_reasons_reconcile_across_hour_and_named_period(self):
        start = ms('2026-10-31T00:59:30Z')
        end = start + 105000
        settings = replace(self.settings, quote_notional=D(100), quote_offset_bps=D(100),
                           max_inventory_usd=D(1000), data_latency_ms=0,
                           place_latency_ms=500, cancel_latency_ms=500,
                           guard_move_bps=D(10000), requote_bps=D(10000), pickoff_enabled=False)
        cases = {
            'no_reference': {'settings': replace(settings, max_ref_age_s=D(40),
                                                  ref_fallback='none')},
            'fallback_online': {'settings': replace(settings, max_ref_age_s=D(40))},
            'no_basis': {'trades': [ondo(start + 80000)]},
            'guard': {'settings': replace(settings, guard_move_bps=D(20)),
                      'prices': [binance(start), binance(start + 24000), binance(start + 25000, '101'),
                                 binance(start + 26000, '101'), binance(start + 80000)]},
            'post_only': {'settings': replace(settings, quote_offset_bps=D(0)),
                          'trades': [ondo(start), ondo(start, side='sell', ident='sell')]},
            'inventory_limit': {'settings': replace(settings, quantity_step=D(1000))},
            'data_gap': {'gaps': [{'source': 'fixture', 'market': 'INTC-USD.P',
                                  'start_ms': start + 20000, 'end_ms': start + 50000}]},
            'requote': {},
            'online': {},
        }
        for expected, case in cases.items():
            with self.subTest(reason=expected):
                configured = case.get('settings', settings)
                result = run_backtest(case.get('trades', [ondo(start), ondo(start, side='sell', ident='sell')]),
                    case.get('prices', [binance(start), binance(start + 60000)]),
                    configured, symbol='INTC', start_ms=start, end_ms=end,
                    gaps=case.get('gaps', ()))
                report = build_report(result, configured)
                totals = report['online_breakdown_ms']
                self.assertGreater(totals[expected], 0)
                self.assertEqual(sum(totals.values()), end - start)
                self.assertEqual(totals['online'] + totals['fallback_online'],
                                 report['totals']['online_ms'])
                hours = {row['hour']: row for row in report['hourly']}
                self.assertEqual(set(hours), {8, 9})
                self.assertEqual(sum(hours[8]['online_breakdown_ms'].values()), D(30000))
                self.assertEqual(sum(hours[9]['online_breakdown_ms'].values()), D(75000))
                for reason, duration in totals.items():
                    self.assertEqual(sum(row['online_breakdown_ms'][reason]
                                         for row in hours.values()), duration, reason)
                    self.assertEqual(report['named_periods']['周末']['online_breakdown_ms'][reason],
                                     hours[9]['online_breakdown_ms'][reason], reason)

    def test_proxy_without_applicable_samples_preserves_missing_reward_and_net(self):
        rows = [observation('2026-11-03T20:59:59Z'),
                observation('2026-11-04T13:30:00Z'),
                observation('2026-11-01T00:00:00Z')]
        report = build_report(self.fixture(), self.settings, depth_observations=rows)
        proxy = report['weekday_proxy']
        self.assertEqual(proxy['sample_count'], 0)
        self.assertIsNone(proxy['comp_median'])
        self.assertIsNone(proxy['weekend']['reward'])
        for mode in ('edge', 'mark', 'liquidate'):
            self.assertIsNone(proxy['weekend']['net_pnl_' + mode])

    def test_proxy_excludes_other_markets_before_computing_median(self):
        rows = [observation('2026-11-03T21:00:00Z', '100'),
                observation('2026-11-03T21:00:01Z', '1000000', market='BB-USD.P')]
        report = build_report(self.fixture(), self.settings, depth_observations=rows)
        proxy = report['weekday_proxy']
        self.assertEqual(proxy['sample_count'], 1)
        self.assertEqual(proxy['comp_median'], D(100))
        self.assertEqual(proxy['weekend']['reward'], D('3.5'))

    def test_incomplete_proxy_book_marks_reward_as_upper_bound(self):
        rows = [observation('2026-11-03T21:00:00Z', '100'),
                observation('2026-11-03T21:00:01Z', '300', complete=False)]
        report = build_report(self.fixture(), self.settings, depth_observations=rows)
        proxy = report['weekday_proxy']
        self.assertEqual(proxy['sample_count'], 2)
        self.assertEqual(proxy['comp_median'], D(200))
        self.assertTrue(proxy['uncertain'])
        self.assertTrue(proxy['weekend']['reward_is_upper_bound'])
        self.assertIsNotNone(proxy['weekend']['reward'])
        self.assertIn('工作日代理，非周末实测', proxy['label'])

    def test_partial_300s_samples_never_publish_complete_edge_or_proxy_net(self):
        value = self.fixture()
        first = value['fills'][0]
        value['fills'].append({**first, 'time_ms': value['start_ms'] + 61000, 'trade_id': 'b'})
        value['totals'].update(fill_count=2, fill_notional=D(198))
        value['center_prices'] += [
            {'time_ns': (value['start_ms'] + 361000) * 1000000, 'price': None,
             'binance_price': D(100)},
            {'time_ns': (value['start_ms'] + 362000) * 1000000, 'price': D(1000),
             'binance_price': D(100)}]
        report = build_report(value, replace(self.settings, markout_max_delay_s=D(0)),
                              depth_observations=[observation('2026-11-03T21:00:00Z')])
        parts = report['pnl_decomposition']
        self.assertEqual(parts['fill_count'], 2)
        self.assertEqual(parts['sample_count'], 1)
        self.assertEqual(parts['missing_count'], 1)
        self.assertEqual(parts['coverage_ratio'], D('.5'))
        self.assertEqual(parts['observed_spread_capture'], D(2))
        self.assertEqual(parts['observed_adverse_selection'], D(-2))
        self.assertEqual(report['markout']['300s']['sample_count'], 1)
        self.assertEqual(report['markout']['300s']['total_usd'], D(-1))
        self.assertIsNone(parts['adverse_selection'])
        self.assertIsNone(parts['inventory_drift'])
        self.assertIsNone(report['valuation']['edge']['pnl'])
        self.assertEqual(report['valuation']['edge']['c_star']['status'], 'unavailable')
        self.assertIsNone(report['scenarios'][0]['net_pnl_edge'])
        weekend = report['named_periods']['周末']
        self.assertIsNone(weekend['valuation']['edge']['pnl'])
        self.assertEqual(weekend['valuation']['edge']['c_star']['status'], 'unavailable')
        self.assertIsNone(report['weekday_proxy']['weekend']['net_pnl_edge'])
        self.assertIsNotNone(report['weekday_proxy']['weekend']['reward'])

    def test_future_center_does_not_backfill_invalid_markout_target(self):
        value = self.fixture()
        value['center_prices'][2:2] = [
            {'time_ns': (value['start_ms'] + 61000) * 1000000, 'price': None,
             'binance_price': D(100)},
            {'time_ns': (value['start_ms'] + 62000) * 1000000, 'price': D(1000),
             'binance_price': D(100)}]
        settings = replace(self.settings, markout_max_delay_s=D(0))
        report = build_report(value, settings)
        self.assertEqual(report['markout']['60s']['sample_count'], 0)
        self.assertEqual(report['markout']['60s']['missing_count'], 1)
        self.assertIsNone(report['markout']['60s']['mean_bps'])
        self.assertEqual(report['markout']['60s']['binance_sample_count'], 1)
        extended = copy.deepcopy(value)
        extended['center_prices'].append({'time_ns': value['end_ms'] * 1000000,
                                         'price': D(99999), 'binance_price': D(99999)})
        after = build_report(extended, settings)
        self.assertEqual(after['markout'], report['markout'])
        self.assertEqual(after['pnl_decomposition'], report['pnl_decomposition'])


if __name__ == '__main__':
    unittest.main()
