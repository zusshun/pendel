"""Final batch D regression tests for report metrics and data applicability."""

import copy
import unittest
from dataclasses import replace
from decimal import Decimal as D

from pendle.bt_config import BacktestSettings
from pendle.bt_report import build_report, competition_report, markdown_summary
from tests.test_bt_report import observation, result_fixture


class DReportTests(unittest.TestCase):
    def setUp(self):
        self.settings = BacktestSettings(pool_usdc_per_week='10080', reward_markets=1,
                                         comp_source='observed', max_ref_age_s='120')

    def fixture(self):
        value = result_fixture('2026-10-31T01:00:00Z', '2026-10-31T01:07:00Z')
        value['fills'][0]['ref_fallback'] = True
        value['center_prices'] = [
            {'time_ns': (value['start_ms'] + offset) * 1000000,
             'price': D(price), 'center_source': source,
             'binance_price': D(100) if source == 'binance_basis' else None,
             'ref_fallback': source == 'ondo_mid'}
            for offset, price, source in ((0, '100', 'ondo_mid'),
                (301000, '98', 'ondo_mid'), (330000, '105', 'binance_basis'),
                (331000, '110', 'binance_basis'))]
        return value

    def depth(self, at='2026-11-03T21:00:00Z', band='25', market='INTC-USD.P'):
        return {**observation(at, '100', market=market), 'payload': {'band_bps': band}}

    def test_band_mismatch_invalidates_competition_and_completeness(self):
        rows = [self.depth(), self.depth('2026-11-03T21:00:01Z', band='20'),
                self.depth(market='BB-USD.P')]
        report = competition_report(rows, self.settings)
        invalid = next(row for row in report['rows']
                       if row['market'] == 'INTC-USD.P' and row['sample_count'])
        self.assertIsNone(invalid['comp_median'])
        self.assertIsNone(invalid['flat_comp_p90'])
        self.assertIsNone(invalid['linear_comp_median'])
        self.assertIsNone(invalid['completeness_median'])
        self.assertIsNone(invalid['completeness_p90'])
        self.assertIsNone(invalid['estimated_reward_per_hour'])
        self.assertFalse(invalid['available'])
        self.assertIn('band', invalid['unavailable_reason'])
        self.assertIn('不一致', invalid['unavailable_reason'])
        valid = next(row for row in report['rows']
                     if row['market'] == 'BB-USD.P' and row['sample_count'])
        self.assertEqual(valid['comp_median'], D(100))

    def test_band_mismatch_disables_observed_scenarios_proxy_and_latest_gate(self):
        rows = [self.depth('2026-10-31T01:00:00Z'), self.depth(band='20')]
        report = build_report(self.fixture(), self.settings, depth_observations=rows)
        self.assertIsNone(report['scenarios'][0]['reward'])
        self.assertIsNone(report['named_periods']['周末']['scenarios'][0]['reward'])
        self.assertFalse(report['competition']['available'])
        self.assertIn('不一致', report['competition']['unavailable_reason'])
        self.assertIsNone(report['weekday_proxy']['comp_median'])
        self.assertIsNone(report['weekday_proxy']['weekend']['reward'])
        self.assertFalse(report['weekday_proxy']['available'])
        self.assertIsNone(report['launch_criterion']['observed_comp'])
        self.assertIsNone(report['launch_criterion']['book_band_complete'])
        self.assertFalse(report['launch_criterion']['sample_comparison_eligible'])
        self.assertIn('band', markdown_summary([report]))

    def test_unknown_band_is_unavailable_and_top_level_band_is_supported(self):
        row = self.depth()
        del row['payload']['band_bps']
        report = build_report(self.fixture(), self.settings, depth_observations=[row])
        self.assertIsNone(report['weekday_proxy']['comp_median'])
        self.assertIn('缺失', report['weekday_proxy']['unavailable_reason'])
        row['band_bps'] = D(25)
        after = build_report(self.fixture(), self.settings, depth_observations=[row])
        self.assertEqual(after['weekday_proxy']['comp_median'], D(100))
        self.assertTrue(after['weekday_proxy']['available'])

    def test_pickoff_reports_immediate_and_hold_to_end_pnl_separately(self):
        value = self.fixture()
        value['fills'][0]['fill_type'] = 'pickoff'
        value['totals'].update(pickoff_count=1, pickoff_pnl=D('-1.8'),
                               pickoff_hold_to_end_pnl=D('22.7'))
        report = build_report(value, self.settings)
        self.assertEqual(report['pickoff']['pnl'], D('-1.8'))
        self.assertEqual(report['pickoff'].get('hold_to_end_pnl'), D('22.7'))
        self.assertIn('成交时中心价', report['pickoff']['pnl_description'])
        text = markdown_summary([report])
        self.assertIn('pickoff 即时损益', text)
        self.assertIn('pickoff 持有至期末损益', text)

    def test_fallback_markout_uses_first_binance_basis_center_after_300s(self):
        value = self.fixture()
        before = build_report(value, self.settings)
        metric = before.get('fallback_binance_300s_markout', {})
        self.assertEqual(metric.get('sample_count'), 1)
        self.assertEqual(metric['total_usd'], D(6))
        self.assertEqual(metric['mean_bps'], D(6) / 99 * 10000)
        self.assertEqual(metric['reference'], 'binance_basis')
        self.assertEqual(metric['delayed_count'], 1)
        self.assertEqual(before['named_periods']['周末']['fallback_binance_300s_markout'], metric)
        self.assertIn('以之后第一个币安推算 center 为基准的 300s markout',
                      markdown_summary([before]))
        changed = copy.deepcopy(value)
        changed['center_prices'][2]['price'] = D(1000)
        after = build_report(changed, self.settings)
        self.assertNotEqual(after['fallback_binance_300s_markout'], metric)
        self.assertEqual(after['valuation']['edge'], before['valuation']['edge'])
        self.assertEqual(after['pnl_decomposition'], before['pnl_decomposition'])

    def test_fallback_binance_markout_respects_delay_limit_and_window(self):
        value = self.fixture()
        settings = replace(self.settings, markout_max_delay_s=D(29))
        at_boundary = build_report(value, settings)
        self.assertEqual(at_boundary.get('fallback_binance_300s_markout', {}).get('sample_count'), 1)
        value['center_prices'][2]['time_ns'] += 1
        late = build_report(value, settings)['fallback_binance_300s_markout']
        self.assertEqual(late['sample_count'], 0)
        self.assertEqual(late['missing_count'], 1)
        self.assertIsNone(late['mean_bps'])
        value['center_prices'][2]['time_ns'] = value['end_ms'] * 1000000
        value['center_prices'] = value['center_prices'][:3]
        outside = build_report(value, self.settings)['fallback_binance_300s_markout']
        self.assertEqual(outside['sample_count'], 0)

    def test_nonfallback_and_unknown_center_sources_are_never_auxiliary_samples(self):
        value = self.fixture()
        value['fills'].append({**value['fills'][0], 'ref_fallback': False, 'trade_id': 'b'})
        for center in value['center_prices'][2:]:
            center.pop('center_source')
        report = build_report(value, self.settings)
        metric = report.get('fallback_binance_300s_markout', {})
        self.assertEqual(metric.get('sample_count'), 0)
        self.assertEqual(metric['missing_count'], 1)


if __name__ == '__main__':
    unittest.main()
