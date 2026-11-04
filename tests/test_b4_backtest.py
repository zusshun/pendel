"""B4 分侧基差、参考价回退及事件时间因果回归。"""

import unittest
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path

from pendle.backtest import run_backtest
from pendle.bt_config import BacktestSettings
from tests.test_backtest import bn, ondo


class B4BacktestTests(unittest.TestCase):
    def setUp(self):
        self.settings = BacktestSettings(quote_notional=100, quote_offset_bps=100,
            max_inventory_usd=1000, data_latency_ms=0, place_latency_ms=0,
            cancel_latency_ms=0, max_ref_age_s=10000, guard_move_bps=10000,
            requote_bps=10000, reward_two_sided=False, pickoff_enabled=False)

    def replay(self, trades=(), prices=(), end=60001, **changes):
        seed = [ondo(0, '99', side='sell', ident='bid'),
                ondo(0, '101', side='buy', ident='ask')]
        return run_backtest(seed + list(trades), [bn(0)] + list(prices),
            replace(self.settings, **changes), symbol='INTC', start_ms=0, end_ms=end)

    def test_bimodal_basis_uses_separate_side_medians(self):
        result = self.replay([ondo(1000, '103'), ondo(2000, '105'),
            ondo(3000, '97', side='sell')], basis_samples=3)
        self.assertEqual(result['minutes'][-1]['center'], D('100.5'))

    def test_one_side_alone_cannot_create_center_even_with_fallback(self):
        result = run_backtest([ondo(0), ondo(1000, '101')], [bn(0)],
            replace(self.settings, max_ref_age_s=1), symbol='INTC',
            start_ms=0, end_ms=60001)
        self.assertIsNone(result['minutes'][-1]['center'])
        self.assertEqual(result['totals']['online_ms'], 0)

    def test_basis_sides_need_not_arrive_within_pair_age(self):
        result = run_backtest([ondo(0, '99', side='sell'), ondo(31000, '101')],
            [bn(0), bn(31000)], self.settings, symbol='INTC', start_ms=0,
            end_ms=60001)
        self.assertEqual(result['minutes'][-1]['center'], D(100))
        self.assertGreater(result['totals']['online_ms'], 0)

    def test_each_basis_sample_expires_independently(self):
        result = self.replay([ondo(1000, '103'),
            ondo(1000, '97', side='sell', ident='new-sell'), ondo(2000, '107')],
            max_basis_age_s='1.5', end=2001)
        current = next(row for row in result['center_prices'] if row['time_ms'] == 2000)
        self.assertEqual(current['price'], D('101'))

    def test_default_fallback_switches_expires_and_recovers(self):
        result = self.replay(prices=[bn(3000)], end=4001,
            max_ref_age_s=1, max_ondo_mid_age_s=2)
        sources = [(row['time_ns'], row.get('center_source'))
                   for row in result['center_prices']]
        self.assertIn((1000000001, 'ondo_mid'), sources)
        self.assertIn((2000000001, None), sources)
        self.assertIn((3000000000, 'binance_basis'), sources)
        self.assertEqual(result['totals']['fallback_ms'], D(1000))
        self.assertEqual(result['totals']['online_breakdown_ms']['fallback_online'], D(1000))
        self.assertEqual(sum(result['totals']['online_breakdown_ms'].values()), D(4001))
        self.assertEqual(result['ref_fallback'], 'ondo_mid')

    def test_none_fallback_preserves_reference_stop(self):
        result = self.replay(end=60001, max_ref_age_s=1, ref_fallback='none')
        self.assertEqual(result['totals']['fallback_ms'], 0)
        self.assertIsNone(result['minutes'][-1]['center'])
        self.assertEqual(result['totals']['online_breakdown_ms']['no_reference'],
                         D('59000.999999'))

    def test_fallback_uses_recent_medians_per_side_and_latest_sample_limit(self):
        result = self.replay([ondo(2000, '103'), ondo(3000, '105'),
            ondo(4000, '97', side='sell')], end=60001, basis_samples=2,
            max_ref_age_s=1)
        self.assertEqual(result['minutes'][-1]['center'], D(101))
        self.assertEqual(result['minutes'][-1]['center_source'], 'ondo_mid')

    def test_fallback_disables_guard_and_does_not_compare_across_outage(self):
        result = self.replay(prices=[bn(500, '100.3'), bn(5000, '101')],
            max_ref_age_s=1, guard_move_bps=20, guard_window_s=10,
            guard_cooldown_s=30, end=60001)
        self.assertEqual(result['totals']['guard_triggers'], 1)
        self.assertEqual(result['totals']['guard_pause_ms'], D('1000.000001'))
        self.assertFalse(result['minutes'][-1]['guard_available'])
        self.assertFalse(result['minutes'][-1]['guard_paused'])
        self.assertGreater(result['totals']['online_breakdown_ms']['fallback_online'], 0)

    def test_old_guard_cancel_cannot_cross_fallback_into_new_pause(self):
        prices = [bn(500, '100.3'), bn(5000, '101')]
        prices.extend(bn(ms, '101.3') for ms in range(5500, 16001, 500))
        result = self.replay(prices=prices, end=16001, max_ref_age_s=1,
            guard_move_bps=20, guard_window_s=10, guard_cooldown_s=30,
            guard_latency_ms=10000)
        cancellations = [event['time_ms'] for event in result['events']
                         if event['type'] == 'cancel_requested']
        self.assertTrue(cancellations)
        # 500ms 旧段的撤单本应在 10500ms 到期，但回退已使它失效。
        # 新段从 5500ms 开始；后续触发延长冷却，不能推迟本段首次撤单。
        self.assertEqual(min(cancellations), 15500)

    def test_fallback_trade_labels_and_edge_are_cumulative_at_boundaries(self):
        result = self.replay([ondo(2000, '98', '.5', 'sell')],
            max_ref_age_s=1, data_latency_ms=100, end=60001)
        fill = result['fills'][0]
        self.assertTrue(fill['ref_fallback'])
        self.assertEqual(fill['center_source'], 'ondo_mid')
        self.assertEqual(fill['center'], D(100))
        self.assertEqual(result['totals']['fallback_fill_count'], 1)
        self.assertEqual(result['totals']['fallback_edge'], D('.5'))
        self.assertEqual(result['totals']['fallback_edge_net'], D('.49505'))
        self.assertEqual(result['minutes'][-1]['before_events']['fallback_fill_count'], 1)
        self.assertEqual(result['minutes'][-1]['fallback_ms'], D('58999.999999'))

    def test_stale_visible_reference_cannot_trigger_pickoff_in_fallback(self):
        # 旧买样本到期令回退中心升至 102.5，旧卖单 102.01 已有套利空间。
        # 迟到且过期的币安消息不能据此把回退中心当作外部可套利参考价。
        result = run_backtest([ondo(0, '99'),
            ondo(500, '99', side='sell', ident='sell'),
            ondo(500, '107', ident='buy'), ondo(1000, '105')], [bn(0), bn(1600)],
            replace(self.settings, max_ref_age_s='.1', max_ondo_mid_age_s='1.5',
                    data_latency_ms=200, pickoff_enabled=True),
            symbol='INTC', start_ms=1000, end_ms=1801)
        self.assertEqual(result['totals']['pickoff_count'], 0)
        self.assertTrue(any(event['type'] == 'order_sent' and event['price'] == D('102.01')
                            for event in result['events']))
        self.assertGreater(result['totals']['fallback_ms'], 0)

    def test_connection_gap_stops_fallback_quotes(self):
        result = run_backtest([ondo(0, '99', side='sell'), ondo(0, '101')],
            [bn(0)], replace(self.settings, max_ref_age_s=1), symbol='INTC',
            start_ms=0, end_ms=5000, gaps=[{'source': 'ondo', 'market': '*',
                'channel': '*', 'start_ms': 2000, 'end_ms': 4000}])
        self.assertEqual(result['totals']['online_breakdown_ms']['data_gap'], D(2000))
        self.assertEqual(len(result['coverage']['gaps']), 1)

    def test_fallback_fill_does_not_invent_a_stale_proxy_hedge(self):
        result = self.replay([ondo(2000, '98', '.5', 'sell')],
            max_ref_age_s=1, data_latency_ms=100, hedge_mode='lighter_proxy',
            hedge_latency_ms=500, end=3000)
        self.assertTrue(result['fills'][0]['ref_fallback'])
        self.assertFalse(result['coverage']['pnl_determinate'])
        self.assertTrue(any(event['type'] == 'hedge_unresolved'
                            for event in result['events']))
        self.assertFalse(any(event['type'] == 'hedge' for event in result['events']))
        self.assertEqual(result['totals']['cancelled_hedge_quantity'], D('.5'))

    def test_future_and_not_yet_visible_trades_cannot_change_fallback(self):
        trades = [ondo(59950, '104', side='buy')]
        before = self.replay(trades, max_ref_age_s=1, data_latency_ms=100)
        future = self.replay(trades + [ondo(61000, '110', side='buy')],
            prices=[bn(61000, '110')], max_ref_age_s=1, data_latency_ms=100)
        self.assertEqual(before, future)
        self.assertEqual(before['minutes'][-1]['center'], D(100))


class B4ConfigTests(unittest.TestCase):
    def test_fallback_defaults_example_and_round_trip(self):
        settings = BacktestSettings()
        self.assertEqual(getattr(settings, 'ref_fallback', None), 'ondo_mid')
        self.assertEqual(getattr(settings, 'max_ondo_mid_age_s', None), D(1800))
        changed = BacktestSettings.from_dict({'ref_fallback': 'none',
                                             'max_ondo_mid_age_s': '123.5'})
        self.assertEqual(BacktestSettings.from_dict(changed.to_dict()), changed)
        example = BacktestSettings.from_file(Path(__file__).parent.parent / 'backtest.example.toml')
        self.assertEqual(example.ref_fallback, settings.ref_fallback)
        self.assertEqual(example.max_ondo_mid_age_s, settings.max_ondo_mid_age_s)

    def test_invalid_fallback_settings_are_rejected(self):
        for values in ({'ref_fallback': 'last'}, {'ref_fallback': True},
                       {'max_ondo_mid_age_s': 0}, {'max_ondo_mid_age_s': 'NaN'}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                BacktestSettings.from_dict(values)
