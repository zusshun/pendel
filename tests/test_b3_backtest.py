"""B3 regressions for two-sided basis, arbitrage taking, and valuation boundaries."""

import unittest
from dataclasses import replace
from decimal import Decimal as D

from pendle.backtest import run_backtest
from pendle.bt_config import BacktestSettings
from tests.test_backtest import bn, ondo


class B3BacktestTests(unittest.TestCase):
    def setUp(self):
        self.settings = BacktestSettings(quote_notional=100, quote_offset_bps=100,
            max_inventory_usd=1000, data_latency_ms=0, place_latency_ms=0,
            cancel_latency_ms=0, max_ref_age_s=10000, guard_move_bps=10000,
            requote_bps=10000, reward_two_sided=False)

    def replay(self, trades=(), prices=(), end=60001, depths=(), **changes):
        return run_backtest([ondo(0, '99', side='sell', ident='bid'),
                             ondo(0, '101', side='buy', ident='ask')] + list(trades),
            [bn(0)] + list(prices), replace(self.settings, **changes),
            symbol='INTC', start_ms=0, end_ms=end, depth_observations=depths)

    def test_basis_is_midpoint_of_latest_visible_aggressor_sides(self):
        result = self.replay([ondo(1000, '105', side='buy')], basis_samples=1)
        self.assertEqual(result['minutes'][-1]['center'], D(102))

    def test_one_sided_trades_do_not_create_basis_or_quotes(self):
        result = run_backtest([ondo(0), ondo(1000, '105')], [bn(0)],
            self.settings, symbol='INTC', start_ms=0, end_ms=60001)
        self.assertIsNone(result['minutes'][-1]['center'])
        self.assertFalse(any(e['type'] == 'order_sent' for e in result['events']))

    def test_retained_opposite_side_need_not_match_new_trade_time(self):
        result = self.replay([ondo(31000, '109', side='buy')],
                             [bn(31000)], basis_samples=1)
        self.assertEqual(result['minutes'][-1]['center'], D(104))

    def test_pickoff_waits_until_binance_price_is_visible(self):
        before = self.replay(prices=[bn(1000, '98')], end=1100,
                             data_latency_ms=100)
        after = self.replay(prices=[bn(1000, '98')], end=1101,
                            data_latency_ms=100)
        self.assertEqual(before['fills'], [])
        self.assertEqual(len(after['fills']), 1)
        fill = after['fills'][0]
        self.assertEqual((fill['time_ms'], fill['fill_type'], fill['price']),
                         (1100, 'pickoff', D(99)))
        self.assertEqual(fill['quantity'], D(1))
        self.assertEqual(after['totals']['pickoff_count'], 1)
        self.assertEqual(after['totals']['pickoff_pnl'], D('-1.0099'))

    def test_pickoff_can_fill_during_guard_cancel_latency(self):
        result = self.replay(prices=[bn(1000, '99.7'), bn(1200, '98')],
            end=2000, guard_move_bps=20, guard_latency_ms=100,
            cancel_latency_ms=500, hedge_mode='lighter_proxy', hedge_latency_ms=100)
        self.assertTrue(result['fills'], '待撤单应被后续可见价格事件套利吃单')
        fill = result['fills'][0]
        requests = [e for e in result['events'] if e['type'] == 'cancel_requested'
                    and e['order_id'] == fill['order_id']]
        self.assertEqual(requests[0]['time_ms'], 1100)
        self.assertEqual(fill['time_ms'], 1200)
        self.assertLess(fill['time_ms'], requests[0]['time_ms'] + 500)
        self.assertTrue(any(e['type'] == 'hedge' for e in result['events']))

    def test_pickoff_uses_sell_threshold_symmetrically(self):
        result = self.replay(prices=[bn(1000, '102')], end=1001)
        self.assertEqual([(f['side'], f.get('fill_type')) for f in result['fills']],
                         [('sell', 'pickoff')])

    def test_pickoff_respects_disabled_setting_and_minimum_edge(self):
        disabled = self.replay(prices=[bn(1000, '98')], pickoff_enabled=False)
        below_edge = self.replay(prices=[bn(1000, '98.99')])
        self.assertEqual(disabled['fills'], [])
        self.assertEqual(below_edge['fills'], [])

    def test_pickoff_at_cancel_effective_nanosecond_is_excluded(self):
        result = self.replay(prices=[bn(1000, '99.7'), bn(1500, '98')],
            end=2000, guard_move_bps=20, guard_latency_ms=0, cancel_latency_ms=500)
        self.assertEqual(result['fills'], [])
        self.assertTrue(any(e['type'] == 'cancel_effective' and e['time_ms'] == 1500
                            for e in result['events']))

    def test_pickoff_at_activation_nanosecond_is_excluded(self):
        result = self.replay(prices=[bn(500, '98'), bn(501, '98')],
                             place_latency_ms=500, end=502)
        self.assertEqual([f['time_ms'] for f in result['fills']], [501])

    def test_fresh_ondo_sides_with_stale_binance_cannot_create_basis(self):
        result = run_backtest([ondo(31000, '99', side='sell', ident='bid'),
                              ondo(31000, '101', side='buy', ident='ask')], [bn(0)],
            self.settings, symbol='INTC', start_ms=0, end_ms=60001)
        self.assertIsNone(result['minutes'][-1]['center'])
        self.assertFalse(any(e['type'] == 'order_sent' for e in result['events']))

    def test_fill_at_activation_nanosecond_precedes_activation(self):
        result = self.replay([ondo(500, '98', side='sell'),
                              ondo(501, '98', side='sell')],
                             place_latency_ms=500, end=1000)
        self.assertEqual([f['time_ms'] for f in result['fills']], [501])

    def test_liquidation_half_spread_uses_symbol_median_and_config_fallback(self):
        trades = [ondo(1000, '98', '.5', 'sell')]
        rows = [{'market': 'INTC-USD.P', 'received_ms': 90000,
                 'mid': D(100), 'spread_bps': D(s)} for s in ('100', '120')]
        rows.append({'market': 'WEN-USD.P', 'received_ms': 90000,
                     'mid': D(100), 'spread_bps': D(999)})
        observed = self.replay(trades, depths=rows)['valuation']['liquidate']
        fallback = self.replay(trades)['valuation']['liquidate']
        self.assertEqual(observed.get('ondo_half_spread_bps'), D(55))
        self.assertEqual(observed.get('ondo_half_spread_source'), 'depth_observations_median')
        self.assertEqual(fallback.get('ondo_half_spread_bps'), D(1))
        self.assertEqual(fallback.get('ondo_half_spread_source'), 'config')
        self.assertLess(observed['pnl'], fallback['pnl'])

    def test_depth_mid_expires_and_reward_falls_back_to_current_center(self):
        result = self.replay(end=180001, depths=[{'received_ms': 0, 'mid': D('100.05')}])
        self.assertEqual(result['minutes'][2]['reward_mid'], D('100.05'))
        self.assertEqual(result['minutes'][3]['reward_mid'], D(100))

    def test_reward_weights_are_recomputed_for_each_mid_error(self):
        result = self.replay(quote_offset_bps=10)
        weights = result['minutes'][-1].get('reward_weights_by_error', {})
        self.assertGreater(weights.get('0', 0), 0)
        self.assertEqual(weights.get('10'), weights.get('0'))
        self.assertEqual(weights.get('20'), D(0))
