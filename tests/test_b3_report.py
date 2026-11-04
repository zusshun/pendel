"""B3 regression tests for reward errors, delayed estimates, and arbitrage taker disclosures."""

import unittest
from dataclasses import replace
from decimal import Decimal as D

from pendle.bt_config import BacktestSettings
from pendle.bt_report import build_report, markdown_summary
from tests.test_bt_report import observation, result_fixture


class B3ReportTests(unittest.TestCase):
    def setUp(self):
        self.settings = BacktestSettings(pool_usdc_per_week='10080', reward_markets=1,
                                         comp_scenarios=(100,), max_ref_age_s='10')

    def fixture(self):
        value = result_fixture('2026-10-31T01:00:00Z', '2026-10-31T01:07:00Z')
        value['center_prices'] = [
            {'time_ns': (value['start_ms'] + offset) * 1000000,
             'price': None if price is None else D(price), 'binance_price': D(100)}
            for offset, price in ((0, '100'), (11000, '101'), (61000, '102'),
                                  (301000, None), (331000, '98'), (332000, '110'))]
        return value

    def test_invalid_target_uses_first_later_valid_center_and_marks_delayed(self):
        value = self.fixture()
        report = build_report(value, self.settings)
        row = report['markout']['300s']
        self.assertEqual(row['sample_count'], 1)
        self.assertEqual(row['missing_count'], 0)
        self.assertEqual(row['delayed_count'], 1)
        self.assertEqual(row['total_usd'], D(-1))
        self.assertEqual(report['pnl_decomposition']['delayed_count'], 1)
        self.assertTrue(report['pnl_decomposition']['complete'])
        self.assertIsNotNone(report['valuation']['edge']['pnl'])
        self.assertNotEqual(report['valuation']['edge']['c_star']['status'], 'unavailable')
        self.assertEqual(report['named_periods']['周末']['markout']['300s']['delayed_count'], 1)
        self.assertIn('含延后估计 1 笔', markdown_summary([report]))

    def test_delay_limit_includes_boundary_but_excludes_one_nanosecond_later(self):
        value = self.fixture()
        settings = replace(self.settings, markout_max_delay_s=D(30))
        report = build_report(value, settings)
        self.assertEqual(report['markout']['300s']['sample_count'], 1)
        value['center_prices'][4]['time_ns'] += 1
        after = build_report(value, settings)
        self.assertEqual(after['markout']['300s']['sample_count'], 0)
        self.assertEqual(after['markout']['300s']['delayed_count'], 0)
        self.assertEqual(after['pnl_decomposition']['missing_count'], 1)
        self.assertIsNone(after['valuation']['edge']['pnl'])
        self.assertEqual(after['valuation']['edge']['c_star']['status'], 'unavailable')

    def test_delayed_center_never_extends_past_report_window(self):
        value = self.fixture()
        value['center_prices'] = value['center_prices'][:4]
        value['center_prices'].append({'time_ns': value['end_ms'] * 1000000,
                                      'price': D(98), 'binance_price': D(100)})
        report = build_report(value, self.settings)
        self.assertEqual(report['markout']['300s']['sample_count'], 0)
        self.assertIsNone(report['valuation']['edge']['pnl'])

    def test_weekend_reward_errors_use_actual_minute_weights_and_proxy_comp(self):
        value = self.fixture()
        for minute in value['minutes']:
            minute['reward_weights_by_error'] = {'0': D(100), '10': D(50), '20': D(0)}
        rows = [observation('2026-11-03T21:00:00Z', '100')]
        report = build_report(value, self.settings, depth_observations=rows)
        scenarios = report['reward_mid_error_scenarios']
        self.assertEqual([row['reward_mid_error_bps'] for row in scenarios], [D(0), D(10), D(20)])
        for row, amount in zip(scenarios, (D('1.5'), D(1), D(0))):
            self.assertAlmostEqual(row['weekday_proxy']['weekend']['reward'], amount)
            self.assertAlmostEqual(row['named_periods']['周末']['scenarios'][0]['reward'], amount)
        self.assertAlmostEqual(report['named_periods']['周末']['reward_mid_error_scenarios'][1]
                               ['scenarios'][0]['reward'], D(1))
        self.assertIn('中间价误差 bps', markdown_summary([report]))

    def test_reward_error_scenarios_respect_configured_list(self):
        value = self.fixture()
        for minute in value['minutes']:
            minute['reward_weights_by_error'] = {'0': D(100), '20': D(0)}
        report = build_report(value, replace(self.settings, reward_mid_error_scenarios=(0, 20)))
        self.assertEqual([row['reward_mid_error_bps'] for row in report['reward_mid_error_scenarios']],
                         [D(0), D(20)])

    def test_reward_error_proxy_cstar_comparison_uses_scenario_weights(self):
        value = self.fixture()
        for minute in value['minutes']:
            minute['reward_weights_by_error'] = {'0': D(100), '10': D(50), '20': D(0)}
        report = build_report(value, self.settings,
                              depth_observations=[observation('2026-11-03T21:00:00Z')])
        comparisons = [row['weekday_proxy']['weekend']['c_star_comparison']['edge']
                       for row in report['reward_mid_error_scenarios']]
        self.assertEqual(comparisons, ['below_c_star', 'above_c_star', 'never_eligible'])

    def test_missing_error_weights_stay_unknown_in_legacy_result(self):
        report = build_report(self.fixture(), self.settings,
                              depth_observations=[observation('2026-11-03T21:00:00Z')])
        self.assertEqual(report['reward_mid_error_scenarios'][0]['weekday_proxy']['weekend']
                         ['reward'], D('1.5'))
        self.assertIsNone(report['reward_mid_error_scenarios'][1]['weekday_proxy']['weekend']['reward'])
        self.assertEqual(report['reward_mid_error_scenarios'][1]['weekday_proxy']['weekend']
                         ['c_star_comparison']['edge'], 'unavailable')

    def test_pickoff_discloses_mark_attribution_and_delayed_edge(self):
        value = self.fixture()
        value['fills'][0]['fill_type'] = 'pickoff'
        value['totals'].update(pickoff_count=1, pickoff_pnl=D('-4.5'))
        report = build_report(value, self.settings)
        self.assertEqual(report['pickoff']['count'], 1)
        self.assertEqual(report['pickoff']['pnl'], D('-4.5'))
        self.assertEqual(report['pickoff']['edge_pnl'], D('-1.01'))
        self.assertEqual(report['pickoff']['delayed_count'], 1)
        self.assertIn('不含共享对冲和资金费', markdown_summary([report]))

    def test_liquidation_spread_source_is_preserved_and_shown(self):
        for source in ('depth_observations_median', 'config'):
            value = self.fixture()
            value['valuation']['liquidate'].update(ondo_half_spread_bps=D('55.5'),
                                                   ondo_half_spread_source=source,
                                                   ondo_half_spread_sample_count=7)
            report = build_report(value, self.settings)
            self.assertEqual(report['valuation']['liquidate']['ondo_half_spread_source'], source)
            text = markdown_summary([report])
            self.assertIn('Ondo 强平半价差 55.5 bps', text)
            self.assertIn('实测价差中位数/2' if source == 'depth_observations_median' else '配置回退', text)


if __name__ == '__main__':
    unittest.main()
