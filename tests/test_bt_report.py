"""报告的北京时间、奖励边界与事后分析行为测试。"""

import importlib
import importlib.util
import unittest
from datetime import datetime
from decimal import Decimal

from pendle.bt_config import BacktestSettings

D = Decimal


def ms(value):
    return int(datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp() * 1000)


def result_fixture(start='2026-10-30T15:59:00Z', end='2026-10-30T16:02:00Z'):
    start_ms, end_ms = ms(start), ms(end)
    minutes = [dict(time_ms=start_ms + n * 60000, my_weight=D('100'),
                    two_sided=True, has_inventory=True, online=n != 1,
                    guard_paused=n == 1, ondo_position=D('1'),
                    lighter_position=D('0'), center=D('100'), reward_mid=D('100'),
                    pnl_mark=D(str(-n)), pnl_liquidate=D(str(-n - 1))) for n in range(3)]
    return dict(symbol='INTC', start_ms=start_ms, end_ms=end_ms, minutes=minutes,
                events=[], fills=[dict(time_ms=start_ms + 1000, side='buy', price=D('99'),
                                      quantity=D('1'), notional=D('99'), fee=D('.01'), trade_id='a',
                                      center=D('100'))],
                totals=dict(fill_count=1, fill_notional=D('99'), maker_fees=D('.01'),
                            hedge_fees=D('.02'), hedge_cost=D('.03'), basis_pnl=D('.04'),
                            funding_cost=D('.05'), guard_triggers=1, guard_pause_ms=60000,
                            online_ms=120000, max_ondo_notional=D('1600'),
                            max_lighter_notional=D('800'), post_only_rejections=2),
                valuation=dict(mark={'pnl': D('-2')}, liquidate={'pnl': D('-3')}),
                coverage={'complete': False, 'gaps': [{'start_ms': start_ms, 'end_ms': start_ms + 1}]},
                binance_prices=[{'time_ms': start_ms, 'price': D('100')},
                                {'time_ms': start_ms + 11000, 'price': D('101')},
                                {'time_ms': start_ms + 61000, 'price': D('102')},
                                {'time_ms': start_ms + 179999, 'price': D('103')}],
                center_prices=[{'time_ms': start_ms, 'price': D('100'), 'binance_price': D('100')},
                               {'time_ms': start_ms + 11000, 'price': D('101'), 'binance_price': D('101')},
                               {'time_ms': start_ms + 61000, 'price': D('102'), 'binance_price': D('102')},
                               {'time_ms': start_ms + 179999, 'price': D('103'), 'binance_price': D('103')}],
                ondo_trades=[{'time_ns': (start_ms + 1000) * 1000000, 'price': D('99'),
                              'size': D('2'), 'cost': D('198'), 'aggressor_side': 'sell'}])


def observation(at, comp='100', market='INTC-USD.P', complete=True):
    return dict(market=market, received_ms=ms(at), source='fixture', mid=D('100'),
                bid=D('99.9'), ask=D('100.1'), spread_bps=D('20'),
                flat_bid_usd=D(comp) / 2, flat_ask_usd=D(comp) / 2,
                linear_bid_usd=D(comp) / 4, linear_ask_usd=D(comp) / 4,
                payload={'band_bps': '25'},
                book_band_complete=complete,
                tick_depth={'bids': [{'price': '100', 'usd': '0'}, {'price': '99.9', 'usd': '20'}],
                            'asks': [{'price': '100', 'usd': '0'}, {'price': '100.1', 'usd': '30'}]})


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('pendle.bt_report'), '尚未实现回测报告模块')
        self.report = importlib.import_module('pendle.bt_report')
        self.settings = BacktestSettings(pool_usdc_per_week='10080', reward_markets=1,
                                         comp_scenarios=(0, 100), max_ref_age_s='10')

    def test_beijing_hour_rolls_over_midnight_and_weekend(self):
        before = self.report.beijing_bucket(ms('2026-10-30T15:59:59Z'))
        after = self.report.beijing_bucket(ms('2026-10-30T16:00:00Z'))
        self.assertEqual(before, ('weekday', 23))
        self.assertEqual(after, ('weekend', 0))

    def test_named_period_wraps_week_and_excludes_endpoint(self):
        period = self.settings.named_periods['周末']
        self.assertFalse(self.report.in_named_period(ms('2026-10-31T00:59:59Z'), period))
        self.assertTrue(self.report.in_named_period(ms('2026-10-31T01:00:00Z'), period))
        self.assertTrue(self.report.in_named_period(ms('2026-11-01T20:59:59Z'), period))
        self.assertFalse(self.report.in_named_period(ms('2026-11-01T21:00:00Z'), period))

    def test_report_splits_hours_and_keeps_named_period(self):
        value = self.report.build_report(result_fixture(), self.settings)
        buckets = {(row['day_type'], row['hour']): row for row in value['hourly']}
        self.assertEqual(buckets[('weekday', 23)]['fill_count'], 1)
        self.assertEqual(buckets[('weekend', 0)]['fill_count'], 0)
        self.assertEqual(buckets[('weekday', 23)]['active_notional'], D('198'))
        self.assertEqual(buckets[('weekend', 0)]['online_rate'], D('.5'))
        self.assertIn('周末', value['named_periods'])
        self.assertEqual(value['named_periods']['周末']['minute_count'], 0)

    def test_markout_is_signed_and_horizon_censored(self):
        value = self.report.build_report(result_fixture(), self.settings)
        self.assertEqual(value['markout']['10s']['mean_bps'], D('2') / D('99') * 10000)
        self.assertEqual(value['markout']['60s']['mean_bps'], D('3') / D('99') * 10000)
        self.assertEqual(value['markout']['300s']['sample_count'], 0)
        self.assertIsNone(value['markout']['300s']['mean_bps'])

    def test_sell_markout_is_profitable_when_reference_falls(self):
        value = result_fixture()
        value['fills'][0].update(side='sell', price=D('102'))
        report = self.report.build_report(value, self.settings)
        self.assertEqual(report['markout']['10s']['mean_bps'], D('1') / D('102') * 10000)

    def test_stale_markout_reference_is_missing(self):
        value = result_fixture()
        value['binance_prices'] = value['binance_prices'][:1]
        value['center_prices'] = value['center_prices'][:1]
        report = self.report.build_report(value, self.settings)
        self.assertEqual(report['markout']['60s']['sample_count'], 0)

    def test_rewards_cstar_and_required_pool_both_valuations(self):
        report = self.report.build_report(result_fixture(), self.settings)
        self.assertEqual(report['valuation']['mark']['c_star']['status'], 'break_even')
        self.assertEqual(report['valuation']['mark']['c_star']['comp'], D('50'))
        self.assertEqual(report['valuation']['liquidate']['c_star']['comp'], D('0'))
        self.assertEqual(report['scenarios'][1]['reward'], D('1.5'))
        self.assertEqual(report['scenarios'][1]['net_pnl_mark'], D('-.5'))
        self.assertEqual(report['scenarios'][1]['required_pool_per_market_mark'], D('13440'))

    def test_cstar_boundaries_are_exposed_without_numeric_fabrication(self):
        for pnl, weights, expected in [('1', [0, 0, 0], 'no_rewards_needed'),
                                        ('-1', [0, 0, 0], 'never_eligible'),
                                        ('-4', [100, 100, 100], 'unprofitable_even_at_zero_comp')]:
            value = result_fixture()
            value['valuation']['mark']['pnl'] = D(pnl)
            for minute, weight in zip(value['minutes'], weights):
                minute['my_weight'] = D(weight)
            with self.subTest(expected=expected):
                self.assertEqual(self.report.build_report(value, self.settings)['valuation']['mark']
                                 ['c_star']['status'], expected)

    def test_observed_comp_uses_hour_day_type_without_fill_from_other_buckets(self):
        settings = BacktestSettings(comp_source='observed', pool_usdc_per_week='10080', reward_markets=1)
        rows = [observation('2026-10-30T15:10:00Z', '100'),
                observation('2026-10-30T15:20:00Z', '300'),
                observation('2026-10-31T15:20:00Z', '9999')]
        value = self.report.build_report(result_fixture(), settings, depth_observations=rows)
        observed = value['scenarios'][0]
        self.assertEqual(observed['comp_source'], 'observed')
        self.assertIsNone(observed['reward'])
        self.assertEqual(observed['known_reward'], D('1') / 3)
        self.assertEqual(observed['missing_comp_minutes'], 2)
        self.assertTrue(observed['partial'])

    def test_incomplete_book_marks_reward_upper_bound_and_live_gate(self):
        rows = [observation('2026-10-30T15:20:00Z', '10', complete=False)]
        value = self.report.build_report(result_fixture(), self.settings, depth_observations=rows)
        self.assertTrue(value['competition']['uncertain'])
        self.assertFalse(value['launch_criterion']['eligible_now'])
        self.assertIn('周末', value['launch_criterion']['description'])

    def test_platform_nominal_ratios_costs_and_coverage_are_preserved(self):
        value = self.report.build_report(result_fixture(), self.settings)
        self.assertEqual(value['max_notional_to_principal'], {'ondo': D('1.6'), 'lighter': D('.8')})
        self.assertEqual(value['totals']['hedge_cost'], D('.03'))
        self.assertFalse(value['coverage']['complete'])
        self.assertEqual(len(value['coverage']['gaps']), 1)
        self.assertEqual(value['status'], 'ok')

    def test_sensitivity_lists_grid_and_uncomputed_values(self):
        result = result_fixture()
        other = result_fixture()
        other['at_price_fill_ratio'] = D('0')
        other['totals']['fill_count'] = 0
        value = self.report.build_report(result, self.settings, sensitivity=[other])
        self.assertEqual([row['at_price_fill_ratio'] for row in value['sensitivity']], [D('0'), D('.5'), D('1')])
        self.assertEqual(value['sensitivity'][0]['fill_count'], 0)
        self.assertEqual(value['sensitivity'][2]['status'], 'not_run')

    def test_volatility_uses_chronological_binance_prices(self):
        value = self.report.build_report(result_fixture(), self.settings)
        self.assertGreater(value['realized_volatility_bps'], 0)

    def test_markdown_handles_multiple_symbols_rejections_and_assumptions(self):
        report = self.report.build_report(result_fixture(), self.settings)
        value = self.report.markdown_summary([report, {'symbol': 'BB', 'status': 'rejected', 'reason': '映射偏离'}])
        for expected in ('INTC', 'BB', '映射偏离', 'C*', '10s', '保证金', '未经官方确认', '周末', '竞争量', '同价'):
            self.assertIn(expected, value)
        self.assertGreaterEqual(len(report['assumptions']), 9)

    def test_competition_ranking_is_by_reward_and_reports_sample_p90(self):
        rows = [observation('2026-10-30T15:00:00Z', '100'),
                observation('2026-10-30T15:01:00Z', '300'),
                observation('2026-10-30T15:02:00Z', '900'),
                observation('2026-10-30T15:02:00Z', '1000', market='CRCL-USD.P', complete=False)]
        report = self.report.competition_report(rows, self.settings)
        populated = [row for row in report['rows'] if row['sample_count']]
        self.assertEqual(populated[0]['market'], 'INTC-USD.P')
        self.assertEqual(populated[0]['comp_median'], D('300'))
        self.assertEqual(populated[0]['comp_p90'], D('900'))
        self.assertEqual(populated[0]['top_depth_median'], D('50'))
        self.assertTrue(populated[1]['uncertain'])
        self.assertEqual(populated[0]['sample_count'], 3)
        self.assertEqual(len(report['rows']), 96)
        self.assertIn('不确定', self.report.competition_markdown(report))
        self.assertIn('缺失', self.report.competition_markdown(report))

    def test_competition_weekend_and_weekday_never_mix(self):
        rows = [observation('2026-10-30T15:00:00Z', '100'), observation('2026-10-31T15:00:00Z', '900')]
        report = self.report.competition_report(rows, self.settings)
        populated = {(row['day_type'], row['hour']): row for row in report['rows'] if row['sample_count']}
        self.assertEqual(populated[('weekday', 23)]['comp_median'], D('100'))
        self.assertEqual(populated[('weekend', 23)]['comp_median'], D('900'))

    def test_no_data_produces_missing_metrics_and_no_false_launch(self):
        value = result_fixture()
        value.update(minutes=[], fills=[], binance_prices=[], center_prices=[], ondo_trades=[])
        report = self.report.build_report(value, self.settings)
        self.assertIsNone(report['realized_volatility_bps'])
        self.assertFalse(report['launch_criterion']['eligible_now'])
        self.assertEqual(self.report.competition_report([], self.settings)['rows'], [])

    def test_terminal_partial_minute_pnl_is_allocated_and_periods_reconcile(self):
        value = result_fixture('2026-10-31T00:59:00Z', '2026-10-31T01:01:30Z')
        value['minutes'] = value['minutes'][:2]
        value['valuation']['mark']['pnl'] = D('-7')
        value['valuation']['liquidate']['pnl'] = D('-8')
        report = self.report.build_report(value, self.settings)
        self.assertEqual(sum(row['valuation']['mark']['pnl'] for row in report['hourly']), D('-7'))
        self.assertEqual(report['named_periods']['周末']['valuation']['mark']['pnl'], D('-6'))
        self.assertEqual(report['launch_criterion']['weekend_c_star']['mark']['status'],
                         'unprofitable_even_at_zero_comp')

    def test_indeterminate_pnl_never_publishes_cstar_net_or_launch(self):
        value = result_fixture()
        value['coverage']['pnl_determinate'] = False
        report = self.report.build_report(value, self.settings)
        self.assertIsNone(report['valuation']['mark']['pnl'])
        self.assertEqual(report['valuation']['mark']['c_star']['status'], 'unavailable')
        self.assertIsNone(report['scenarios'][0]['net_pnl_mark'])
        self.assertIsNone(report['scenarios'][0]['required_pool_per_market_mark'])
        self.assertTrue(all(row['valuation']['mark']['pnl'] is None for row in report['hourly']))
        self.assertFalse(report['launch_criterion']['eligible_now'])
        self.assertIn('损益不可判定', self.report.markdown_summary([report]))

    def test_inventory_pnl_adds_costs_and_excludes_basis_without_double_count(self):
        report = self.report.build_report(result_fixture(), self.settings)
        self.assertAlmostEqual(sum(row['inventory_pnl'] for row in report['hourly']), D('-1.93'))

    def test_unrecoverable_best_level_depth_is_missing(self):
        row = observation('2026-10-30T15:00:00Z')
        row['tick_depth']['bids'] = []
        report = self.report.competition_report([row], self.settings)
        item = next(row for row in report['rows'] if row['sample_count'])
        self.assertIsNone(item['top_depth_median'])
        self.assertEqual(item['top_depth_sample_count'], 0)

    def test_cumulative_online_and_guard_counters_use_exact_period_deltas(self):
        value = result_fixture()
        for i, minute in enumerate(value['minutes']):
            minute.update(online_ms=D(i * 15000), guard_pause_ms=D(i * 10000), guard_triggers=i)
        report = self.report.build_report(value, self.settings)
        self.assertEqual(sum(row['online_ms'] for row in report['hourly']), D('120000'))
        self.assertEqual(sum(row['guard_pause_ms'] for row in report['hourly']), D('60000'))
        self.assertEqual(sum(row['guard_triggers'] for row in report['hourly']), 1)

    def test_zero_volatility_is_not_reported_as_missing(self):
        value = result_fixture()
        value['binance_prices'] = value['binance_prices'][:1]
        value['center_prices'] = value['center_prices'][:1]
        report = self.report.build_report(value, self.settings)
        self.assertEqual(report['realized_volatility_bps'], D(0))
        self.assertEqual(next(row for row in report['hourly'] if row['hour'] == 23)
                         ['realized_volatility_bps'], D(0))

    def test_missing_observed_comp_does_not_invent_full_window_pnl(self):
        settings = BacktestSettings(comp_source='observed')
        report = self.report.build_report(result_fixture(), settings)
        self.assertIsNone(report['scenarios'][0]['reward'])
        self.assertEqual(report['scenarios'][0]['known_reward'], D(0))
        self.assertIsNone(report['scenarios'][0]['net_pnl_mark'])
        self.assertIsNone(report['scenarios'][0]['required_pool_per_market_mark'])

    def test_missing_comp_for_ineligible_minute_still_has_known_zero_reward(self):
        settings = BacktestSettings(comp_source='observed')
        value = result_fixture()
        for row in value['minutes']:
            row['my_weight'] = D(0)
        report = self.report.build_report(value, settings)
        self.assertEqual(report['scenarios'][0]['reward'], D(0))
        self.assertFalse(report['scenarios'][0]['partial'])
        self.assertEqual(report['scenarios'][0]['missing_comp_minutes'], 0)

    def test_no_weekend_sample_cannot_produce_live_launch_signal(self):
        report = self.report.build_report(result_fixture(), self.settings,
            depth_observations=[observation('2026-11-04T00:00:00Z', '1')])
        self.assertFalse(report['launch_criterion']['eligible_now'])

    def test_sensitivity_accepts_cli_ratio_result_shape(self):
        value = result_fixture()
        report = self.report.build_report(value, self.settings, sensitivity=[{'ratio': D(1), 'result': value}])
        self.assertEqual(report['sensitivity'][2]['status'], 'ok')
        self.assertEqual(report['sensitivity'][2]['fill_count'], 1)

    def test_engine_assumptions_survive_json_and_markdown(self):
        value = result_fixture()
        value['assumptions'] = ['引擎额外归因假设']
        report = self.report.build_report(value, self.settings)
        self.assertIn('引擎额外归因假设', report['assumptions'])
        self.assertIn('引擎额外归因假设', self.report.markdown_summary([report]))

    def test_boundary_fill_fee_and_pnl_stay_in_new_named_period(self):
        value = result_fixture('2026-10-31T00:59:00Z', '2026-10-31T01:02:00Z')
        boundary = value['start_ms'] + 60000
        value['fills'][0].update(time_ms=boundary, fee=D('1'))
        value['totals']['maker_fees'] = D('1')
        value['valuation']['mark']['pnl'] = D('-1')
        value['valuation']['liquidate']['pnl'] = D('-1')
        for i, minute in enumerate(value['minutes']):
            minute.update(pnl_mark=D(0 if i == 0 else -1), pnl_liquidate=D(0 if i == 0 else -1),
                          maker_fees=D(0 if i == 0 else 1))
            minute['before_events'] = {'pnl_mark': D(0 if i < 2 else -1),
                                       'pnl_liquidate': D(0 if i < 2 else -1),
                                       'maker_fees': D(0 if i < 2 else 1)}
        report = self.report.build_report(value, self.settings)
        old = next(row for row in report['hourly'] if row['hour'] == 8)
        new = report['named_periods']['周末']
        self.assertEqual(old['maker_fees'], D(0))
        self.assertEqual(old['valuation']['mark']['pnl'], D(0))
        self.assertEqual(new['maker_fees'], D(1))
        self.assertEqual(new['valuation']['mark']['pnl'], D(-1))
        self.assertEqual(new['fill_count'], 1)

    def test_historical_scan_cannot_claim_current_live_eligibility(self):
        value = result_fixture('2026-10-31T01:00:00Z', '2026-10-31T01:03:00Z')
        value['valuation']['mark']['pnl'] = D(2)
        value['valuation']['liquidate']['pnl'] = D(1)
        for row in value['minutes']:
            row['pnl_mark'], row['pnl_liquidate'] = D(2), D(1)
        report = self.report.build_report(value, self.settings,
            depth_observations=[observation('2026-11-04T00:00:00Z', '1')])
        self.assertFalse(report['launch_criterion']['eligible_now'])
        self.assertTrue(report['launch_criterion']['requires_live_check'])
        self.assertFalse(report['launch_criterion']['sample_comparison_eligible'])
        self.assertEqual(report['launch_criterion']['weekend_c_star']['edge']['status'], 'unavailable')


class B2ReportTests(unittest.TestCase):
    def setUp(self):
        self.report = importlib.import_module('pendle.bt_report')
        self.settings = BacktestSettings(pool_usdc_per_week='10080', reward_markets=1,
                                         comp_scenarios=(100, 300), max_ref_age_s='10')

    def fixture(self):
        value = result_fixture('2026-10-31T01:00:00Z', '2026-10-31T01:06:00Z')
        first = value['minutes'][0]
        value['minutes'] = [{**first, 'time_ms': value['start_ms'] + i * 60000,
                             'pnl_mark': D(i) / 2, 'pnl_liquidate': D(i) / 3}
                            for i in range(6)]
        value['valuation'] = dict(mark={'pnl': D(3)}, liquidate={'pnl': D(2)})
        value['totals'].update(maker_fees=D('.2'), hedge_fees=D('.1'), funding_cost=D('.2'),
                               hedge_cost=D('.5'), online_ms=300000, guard_triggers=8,
                               guard_pause_segments=2, online_breakdown_ms={
                                   'online': 300000, 'no_reference': 20000, 'no_basis': 10000,
                                   'guard': 10000, 'post_only': 5000, 'requote': 5000,
                                   'inventory_limit': 5000, 'data_gap': 5000})
        value['center_prices'] = [dict(time_ms=value['start_ms'] + offset,
                                      price=D(price), binance_price=D('200'))
                                  for offset, price in ((0, '100'), (11000, '99'),
                                                        (61000, '98'), (301000, '98'))]
        return value

    def test_markout_uses_center_and_preserves_raw_binance_as_auxiliary(self):
        report = self.report.build_report(self.fixture(), self.settings)
        self.assertEqual(report['markout']['10s']['mean_bps'], D(0))
        self.assertEqual(report['markout']['300s']['total_usd'], D(-1))
        self.assertEqual(report['markout']['10s']['reference'], 'quote_center')
        self.assertEqual(report['markout']['10s']['binance_mean_bps'], D(101) / 99 * 10000)

    def test_decomposition_reconciles_mark_and_edge_excludes_inventory_luck(self):
        report = self.report.build_report(self.fixture(), self.settings)
        parts = report['pnl_decomposition']
        self.assertEqual(parts['spread_capture'], D(1))
        self.assertEqual(parts['adverse_selection'], D(-2))
        self.assertEqual(parts['inventory_drift'], D('4.5'))
        self.assertEqual(parts['edge_pnl'], D(-2))
        self.assertEqual(parts['spread_capture'] + parts['adverse_selection']
                         + parts['inventory_drift'] - parts['fees'], report['valuation']['mark']['pnl'])
        self.assertAlmostEqual(report['valuation']['edge']['c_star']['comp'], D(200))
        self.assertEqual(report['valuation']['mark']['c_star']['status'], 'no_rewards_needed')
        self.assertAlmostEqual(report['named_periods']['周末']['valuation']['edge']['c_star']['comp'], D(200))
        self.assertEqual(sum(row['pnl_decomposition']['spread_capture'] for row in report['hourly']), D(1))

    def test_missing_300s_center_keeps_edge_unavailable_instead_of_zero(self):
        value = self.fixture()
        value['center_prices'][-1]['price'] = None
        report = self.report.build_report(value, self.settings)
        self.assertIsNone(report['pnl_decomposition']['adverse_selection'])
        self.assertIsNone(report['valuation']['edge']['pnl'])
        self.assertEqual(report['valuation']['edge']['c_star']['status'], 'unavailable')
        self.assertEqual(report['pnl_decomposition']['missing_count'], 1)
        self.assertIsNone(report['scenarios'][0]['net_pnl_edge'])

    def test_weekday_proxy_uses_matching_weight_and_half_open_time_window(self):
        rows = [observation('2026-11-03T20:59:00Z', '9999'),
                observation('2026-11-03T21:00:00Z', '100'),
                observation('2026-11-04T13:29:59Z', '300'),
                observation('2026-11-04T13:30:00Z', '9999'),
                observation('2026-11-01T00:00:00Z', '9999')]
        settings = BacktestSettings(pool_usdc_per_week='10080', reward_markets=1,
                                    reward_weight='linear', max_ref_age_s='10')
        report = self.report.build_report(self.fixture(), settings, depth_observations=rows)
        proxy = report['weekday_proxy']
        self.assertEqual(proxy['sample_count'], 2)
        self.assertEqual(proxy['comp_median'], D(100))
        self.assertEqual(proxy['weekend']['reward'], D(3))
        self.assertEqual(proxy['weekend']['net_pnl_edge'], D(1))
        self.assertEqual(proxy['weekend']['net_pnl_mark'], D(6))
        self.assertEqual(proxy['weekend']['c_star_comparison']['edge'], 'below_c_star')
        self.assertIn('工作日代理，非周末实测', proxy['label'])

    def test_summary_pairs_hedges_and_shows_weekend_three_cstars_and_pause_counters(self):
        value = self.fixture()
        none = self.report.build_report(value, self.settings)
        settings = BacktestSettings(pool_usdc_per_week='10080', reward_markets=1,
                                    hedge_mode='lighter_proxy', max_ref_age_s='10')
        hedge = self.report.build_report(value, settings)
        text = self.report.markdown_summary([none, hedge])
        first = text.split('## INTC')[0]
        for expected in ('edge C*', '账面 C*', '强平 C*', 'none', 'lighter_proxy'):
            self.assertIn(expected, first)
        for expected in ('无参考价', '无基差', 'post-only', '撤单或重报', '库存上限',
                         '原始触发', '暂停段数', '工作日代理，非周末实测', '价差收益', '逆向选择', '库存漂移'):
            self.assertIn(expected, text)
        self.assertEqual(sum(none['online_breakdown_ms'].values()), value['end_ms'] - value['start_ms'])
        self.assertEqual(none['guard_pause_segments'], 2)


    def test_center_expiry_one_nanosecond_after_target_is_not_applied_early(self):
        value = self.fixture()
        for row in value['center_prices']:
            row['time_ns'] = row['time_ms'] * 1000000
        last = value['center_prices'][-1]
        value['center_prices'].append({**last, 'time_ns': last['time_ns'] + 1, 'price': None})
        report = self.report.build_report(value, self.settings)
        self.assertEqual(report['markout']['300s']['total_usd'], D(-1))
        self.assertEqual(report['pnl_decomposition']['adverse_selection'], D(-2))

    def test_valid_constant_center_trajectory_does_not_expire_from_row_age(self):
        value = self.fixture()
        row = value['center_prices'][0]
        value['center_prices'] = [{**row, 'time_ns': row['time_ms'] * 1000000}]
        report = self.report.build_report(value, self.settings)
        self.assertEqual(report['markout']['300s']['sample_count'], 1)
        self.assertEqual(report['pnl_decomposition']['adverse_selection'], D(0))


    def test_indeterminate_hedge_without_collector_gaps_names_stale_reference(self):
        value = self.fixture()
        value['coverage'].update(pnl_determinate=False, gaps=[])
        settings = BacktestSettings(hedge_mode='lighter_proxy')
        text = self.report.markdown_summary([self.report.build_report(value, settings)])
        self.assertIn('损益不可判定：对冲执行时参考价失效，无法确认对冲', text)
        self.assertNotIn('损益不可判定：持仓或挂单跨数据断档', text)

    def test_indeterminate_pnl_with_gaps_mentions_gaps_or_insufficient_reference(self):
        value = self.fixture()
        value['coverage']['pnl_determinate'] = False
        text = self.report.markdown_summary([self.report.build_report(value, self.settings)])
        self.assertIn('损益不可判定：持仓或挂单跨数据断档，或参考价不足', text)


class B4ReportTests(unittest.TestCase):
    setUp = B2ReportTests.setUp
    fixture = B2ReportTests.fixture

    def test_fallback_duration_and_fills_are_reported_with_exact_period_deltas(self):
        value = self.fixture()
        value['fills'][0]['ref_fallback'] = True
        value['totals'].update(fallback_ms=90000, fallback_fill_count=1,
                               fallback_edge=D(1), fallback_edge_net=D('.99'))
        value['totals']['online_breakdown_ms']['online'] -= 60000
        value['totals']['online_breakdown_ms']['fallback_online'] = 60000
        for i, minute in enumerate(value['minutes']):
            minute['before_events'] = {
                'fallback_ms': min(i * 30000, 90000),
                'fallback_fill_count': int(i > 0),
                'fallback_edge': D(int(i > 0)),
                'fallback_edge_net': D('.99') if i > 0 else D(0)}
        report = self.report.build_report(value, self.settings)
        self.assertEqual(report.get('ref_fallback'), 'ondo_mid')
        self.assertEqual(report['fallback_rate'], D('.25'))
        self.assertEqual(report['fallback_fill_count'], 1)
        self.assertEqual(report['fallback_edge'], D(1))
        self.assertEqual(report['fallback_edge_net'], D('.99'))
        self.assertEqual(report['fallback_pnl_decomposition']['edge_pnl'], D('-1.01'))
        self.assertEqual(report['named_periods']['周末']['fallback_ms'], 90000)
        self.assertEqual(sum(row['fallback_ms'] for row in report['hourly']), 90000)
        self.assertEqual(sum(row['fallback_edge_net'] for row in report['hourly']), D('.99'))
        text = self.report.markdown_summary([report])
        for expected in ('回退时长占比', '回退在线', '回退成交数', '回退成交时 edge', '回退 300s edge'):
            self.assertIn(expected, text)

    def test_first_table_compares_both_fallbacks_and_details_only_default(self):
        reports = []
        rows = [observation('2026-11-04T01:00:00Z', '100')]
        for fallback in ('ondo_mid', 'none'):
            value = self.fixture()
            value['ref_fallback'] = fallback
            report = self.report.build_report(value, self.settings, depth_observations=rows)
            report['ref_fallback'] = fallback
            reports.append(report)
        text = self.report.markdown_summary(reports)
        first = text.split('300s 中心缺失时')[0]
        for expected in ('ondo_mid', '| none |', '回退口径', '周末奖励（0bps）', '周末 edge 净收益', '回退时长占比'):
            self.assertIn(expected, first)
        self.assertEqual(text.count('## INTC / none'), 1)
        self.assertIn('## INTC / none / ondo_mid', text)
