"""Behavior tests for event replay latency, causality, and accounting."""

import unittest
from dataclasses import replace
from decimal import Decimal as D

from pendle.bt_config import BacktestSettings
try:
    from pendle.backtest import run_backtest, validate_mapping
except ImportError:
    run_backtest = validate_mapping = None


def ondo(ms, price='100', size='1', side='buy', ident=None):
    return {'id': ident or str(ms), 'market': 'INTC-USD.P',
            'time_ns': ms * 1000000, 'price': D(price), 'size': D(size),
            'cost': D(price) * D(size), 'aggressor_side': side}


def bn(ms, price='100'):
    return {'agg_id': ms, 'symbol': 'INTCUSDT', 'transact_time': ms,
            'price': D(price), 'quantity': D('1'), 'is_buyer_maker': False}


class BacktestTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(callable(run_backtest), '必须实现事件回放函数')
        self.settings = BacktestSettings(quote_notional=100, quote_offset_bps=100,
            max_inventory_usd=1000, data_latency_ms=0, place_latency_ms=0,
            cancel_latency_ms=0, max_ref_age_s=10000, guard_move_bps=10000,
            requote_bps=10000, reward_two_sided=False, pickoff_enabled=False)

    def run_bt(self, trades=(), prices=(), end=10000, **changes):
        # Warm up with visible fills on both sides to establish the center; each test changes only its target events.
        seed = [ondo(0), ondo(0, side='sell', ident='seed-sell')]
        return run_backtest(seed + list(trades), [bn(0)] + list(prices),
                            replace(self.settings, **changes), symbol='INTC',
                            start_ms=0, end_ms=end)

    def test_future_append_does_not_change_any_output(self):
        a = self.run_bt([ondo(1000, '98', side='sell')])
        b = self.run_bt([ondo(1000, '98', side='sell'), ondo(20000, '300')],
                        [bn(20000, '300')])
        self.assertEqual(a, b)

    def test_sparse_ondo_basis_survives_with_fresh_binance_reference(self):
        result = self.run_bt(prices=[bn(600000)], end=660001, band_bps=200)
        self.assertGreater(result['minutes'][-1]['my_weight'], 0)
        self.assertEqual(result['minutes'][-1]['center'], D(100))

    def test_latest_basis_sample_expires_at_maximum_age(self):
        result = self.run_bt(prices=[bn(600000)], end=660001, max_basis_age_s=600)
        self.assertEqual(result['minutes'][-1]['my_weight'], 0)
        self.assertIsNone(result['minutes'][-1]['center'])

    def test_basis_uses_last_n_samples_median_without_time_window(self):
        result = self.run_bt([ondo(1000, '101'), ondo(2000, '102')],
                             end=60001, basis_samples=2)
        # The median of the last two taker buys is 101.5; the taker sell median is 100.
        self.assertEqual(result['minutes'][-1]['center'], D('100.75'))

    def test_basis_pair_age_is_independent_of_reference_expiry(self):
        result = self.run_bt([ondo(31000, '104')], [bn(60000)], end=60001,
                             basis_pair_max_age_s=30, max_ref_age_s=120)
        self.assertEqual(result['minutes'][-1]['center'], D('100'))

    def test_fills_capture_decision_center_before_trade_becomes_visible(self):
        result = self.run_bt([ondo(1000, '98', '.5', 'sell')], data_latency_ms=100)
        self.assertEqual(result['fills'][0].get('center'), D('100'))

    def test_online_breakdown_conserves_every_millisecond(self):
        result = self.run_bt(end=10000, max_ref_age_s=1, place_latency_ms=500,
                             cancel_latency_ms=500, ref_fallback='none')
        parts = result['totals'].get('online_breakdown_ms', {})
        self.assertEqual(sum(parts.values(), D(0)), D(10000))
        self.assertEqual(parts['online'], D('500.000001'))
        self.assertEqual(parts['no_reference'], D('8999.999999'))
        self.assertEqual(parts['requote'], D(500))
        self.assertEqual(parts['online'], result['totals']['online_ms'])

    def test_expired_on_arrival_reference_cannot_rewind_clock(self):
        result = self.run_bt([ondo(200)], [bn(200)], end=1000,
                             data_latency_ms=100, max_ref_age_s='.05', ref_fallback='none')
        self.assertEqual(sum(result['totals']['online_breakdown_ms'].values()), D(1000))
        self.assertEqual(result['totals']['online_breakdown_ms']['no_reference'], D(1000))

    def test_post_only_refusal_is_not_online(self):
        result = self.run_bt([ondo(0, side='sell', ident='second')],
                             quote_offset_bps=0, end=10000)
        parts = result['totals'].get('online_breakdown_ms', {})
        self.assertEqual(parts.get('post_only'), D(10000))
        self.assertEqual(result['totals']['online_ms'], 0)

    def test_guard_counts_raw_triggers_and_distinct_pause_segments(self):
        result = self.run_bt(prices=[bn(900, '101'), bn(1000, '101'),
            bn(1100, '101')], end=5000, guard_move_bps=20,
            guard_window_s=1, guard_cooldown_s=2)
        self.assertEqual(result['totals']['guard_triggers'], 2)
        self.assertEqual(result['totals'].get('guard_pause_segments'), 1)

    def test_guard_pause_segment_can_begin_during_warmup(self):
        result = run_backtest([ondo(500), ondo(4000)], [bn(0), bn(900, '101')],
            replace(self.settings, guard_move_bps=20, guard_cooldown_s=2),
            symbol='INTC', start_ms=1000, end_ms=5000)
        self.assertEqual(result['totals'].get('guard_pause_segments'), 1)
        self.assertEqual(result['totals']['guard_triggers'], 0)

    def test_guard_resuming_at_window_start_has_no_pause_segment(self):
        result = run_backtest([ondo(500), ondo(3000)], [bn(0), bn(900, '101')],
            replace(self.settings, guard_move_bps=20, guard_cooldown_s=2),
            symbol='INTC', start_ms=2900, end_ms=4000)
        self.assertEqual(result['totals']['guard_pause_ms'], 0)
        self.assertEqual(result['totals']['guard_pause_segments'], 0)

    def test_touching_guard_intervals_are_one_pause_segment(self):
        result = self.run_bt(prices=[bn(900, '101'), bn(2900, '102')],
                             guard_move_bps=20, guard_window_s=10,
                             guard_cooldown_s=2, end=5000)
        self.assertEqual(result['totals']['guard_triggers'], 2)
        self.assertEqual(result['totals']['guard_pause_ms'], D(4000))
        self.assertEqual(result['totals']['guard_pause_segments'], 1)

    def test_single_trade_volume_is_conserved(self):
        result = self.run_bt([ondo(1000, '98', '0.7', 'sell')])
        fills = [f for f in result['fills'] if f['trade_id'] == '1000']
        self.assertEqual(sum((f['quantity'] for f in fills), D(0)), D('0.7'))

    def test_no_fill_before_order_effective(self):
        result = self.run_bt([ondo(100, '98', side='sell'),
                              ondo(600, '98', side='sell')], place_latency_ms=500)
        self.assertEqual([f['time_ms'] for f in result['fills']], [600])

    def test_data_latency_cannot_retroactively_fill(self):
        result = self.run_bt([ondo(100, '98', side='sell')], data_latency_ms=500)
        self.assertEqual(result['fills'], [])

    def test_cancel_delay_still_allows_old_order_fill(self):
        result = self.run_bt([ondo(1200, '98', side='sell')], [bn(1000, '102')],
                             requote_bps=1, cancel_latency_ms=500)
        self.assertEqual(result['fills'][0]['time_ms'], 1200)
        self.assertEqual(result['fills'][0]['price'], D('99'))

    def test_same_time_cancel_precedes_trade(self):
        result = self.run_bt([ondo(1500, '98', side='sell')], [bn(1000, '102')],
                             requote_bps=1, cancel_latency_ms=500, place_latency_ms=1000)
        self.assertEqual(result['fills'], [])

    def test_post_only_rejection_is_counted(self):
        result = self.run_bt(quote_offset_bps=0)
        self.assertGreater(result['totals']['post_only_rejections'], 0)
        self.assertTrue(any(e['type'] == 'post_only_reject' for e in result['events']))

    def test_pending_orders_count_toward_inventory_limit(self):
        result = self.run_bt([ondo(1000, '98', '20', 'sell')],
                             quote_notional=800, max_inventory_usd=100,
                             place_latency_ms=500)
        self.assertLessEqual(result['totals']['max_ondo_notional'], D('102'))
        for event in result['events']:
            if event['type'] == 'order_sent':
                self.assertLessEqual(event['potential_notional'], D(100))

    def test_at_price_ratio_controls_allocatable_budget(self):
        quantities = []
        for ratio in (0, D('.5'), 1):
            result = self.run_bt([ondo(1000, '99', '1', 'sell')],
                                 at_price_fill_ratio=ratio)
            quantities.append(sum((f['quantity'] for f in result['fills']), D(0)))
        self.assertEqual(quantities, [D(0), D('.5'), D(1)])

    def test_hedges_net_opposite_fills_before_due(self):
        result = self.run_bt([ondo(1000, '98', '0.5', 'sell'),
                              ondo(1200, '102', '0.5', 'buy')],
                             hedge_mode='lighter_proxy', hedge_latency_ms=500)
        self.assertFalse(any(e['type'] == 'hedge' for e in result['events']))
        self.assertEqual(result['valuation']['mark']['lighter_position'], 0)

    def test_pending_hedge_is_cancelled_at_end(self):
        result = self.run_bt([ondo(9900, '98', '0.5', 'sell')],
                             hedge_mode='lighter_proxy', hedge_latency_ms=500)
        self.assertEqual(result['valuation']['mark']['lighter_position'], 0)
        self.assertEqual(result['totals']['cancelled_hedge_quantity'], D('0.5'))

    def test_hedge_executes_after_latency_with_fees(self):
        result = self.run_bt([ondo(1000, '98', '0.5', 'sell')],
                             hedge_mode='lighter_proxy', hedge_latency_ms=500)
        hedges = [e for e in result['events'] if e['type'] == 'hedge']
        self.assertEqual(hedges[0]['time_ms'], 1500)
        self.assertEqual(hedges[0]['quantity'], D('-.5'))
        self.assertGreater(result['totals']['hedge_fees'], 0)

    def test_guard_recovers_after_quiet_cooldown(self):
        result = self.run_bt(prices=[bn(1000, '101'), bn(5000, '101')],
                             guard_move_bps=20, guard_window_s=1,
                             guard_cooldown_s=2, guard_latency_ms=100)
        types = [e['type'] for e in result['events']]
        self.assertIn('guard_trigger', types)
        self.assertIn('guard_resume', types)
        self.assertGreater(result['totals']['guard_pause_ms'], 0)
        self.assertTrue(any(e['type'] == 'order_sent' and e['time_ms'] >= 3000
                            for e in result['events']))

    def test_both_end_valuations_charge_only_actual_positions(self):
        result = self.run_bt([ondo(1000, '98', '0.5', 'sell')])
        mark, liquid = result['valuation']['mark'], result['valuation']['liquidate']
        self.assertGreater(mark['pnl'], liquid['pnl'])
        self.assertEqual(mark['ondo_position'], D('.5'))
        self.assertEqual(mark['lighter_position'], 0)

    def test_minute_snapshot_precedes_new_decision(self):
        result = self.run_bt(end=120001, band_bps=200)
        self.assertEqual(result['minutes'][0]['my_weight'], 0)
        self.assertGreater(result['minutes'][1]['my_weight'], 0)
        self.assertIn('pnl_mark', result['minutes'][1])

    def test_collector_gap_marks_unresolved_risk(self):
        result = run_backtest([ondo(0), ondo(0, side='sell', ident='seed-sell'),
            ondo(1000, '98', side='sell')], [bn(0)],
            self.settings, symbol='INTC', start_ms=0, end_ms=10000,
            gaps=[{'start_ms': 1500, 'end_ms': 5000, 'reason': '断线'}])
        self.assertFalse(result['coverage']['pnl_determinate'])

    def test_mapping_rejects_wrong_asset(self):
        with self.assertRaisesRegex(ValueError, '映射'):
            run_backtest([ondo(2, '100')], [bn(1, '1')], self.settings,
                         symbol='INTC', start_ms=0, end_ms=10000)

    def test_mapping_uses_window_without_future_prices(self):
        dev = validate_mapping([ondo(1)], [bn(0), bn(20000, '1')],
                               start_ms=0, end_ms=10000, max_mapping_dev_bps=500)
        self.assertEqual(dev, 0)

    def test_reference_expiry_stops_new_quotes(self):
        result = self.run_bt(end=120001, max_ref_age_s=1, ref_fallback='none')
        self.assertEqual(result['minutes'][1]['my_weight'], 0)

    def test_funding_is_accrued_over_actual_holding_time(self):
        plain = self.run_bt([ondo(1000, '98', '0.5', 'sell')], end=3601000)
        funded = self.run_bt([ondo(1000, '98', '0.5', 'sell')], end=3601000,
                             ondo_funding_bps_per_hour=1)
        self.assertGreater(funded['totals']['funding_cost'], 0)
        self.assertLess(funded['valuation']['mark']['pnl'], plain['valuation']['mark']['pnl'])

    def test_guard_request_respects_guard_latency(self):
        result = self.run_bt([ondo(1100, '98', '.5', 'sell')], [bn(1000, '101')],
                             guard_move_bps=20, guard_latency_ms=500,
                             cancel_latency_ms=0)
        self.assertTrue(result['fills'], '守卫请求延迟期间旧单仍可成交')
        self.assertEqual(result['fills'][0]['time_ms'], 1100)
        requested = [e for e in result['events'] if e['type'] == 'cancel_requested']
        self.assertEqual(requested[0]['time_ms'], 1500)

    def test_potential_inventory_uses_order_limit_not_center(self):
        result = self.run_bt(max_inventory_usd=100, quote_notional=800)
        sent = [e for e in result['events'] if e['type'] == 'order_sent']
        for event in sent:
            self.assertLessEqual(event['quantity'] * event['price'], D(100))

    def test_future_depth_cannot_change_reward_mid(self):
        rows = [ondo(0), ondo(0, side='sell', ident='seed-sell')]
        prices = [bn(0)]
        settings = replace(self.settings, band_bps=200)
        base = run_backtest(rows, prices, settings, symbol='INTC', start_ms=0, end_ms=120001)
        extra = run_backtest(rows, prices, settings, symbol='INTC', start_ms=0, end_ms=120001,
            depth_observations=[{'received_ms': 900000, 'mid': D('300')}])
        self.assertEqual(base, extra)

    def test_multiple_orders_share_single_trade_budget(self):
        from pendle.backtest import _Replay
        replay = _Replay([], [], self.settings, 'INTC', 0, 10000, [], [])
        replay.reference = replay.center = D(100)
        replay.reference_time = 0
        for ident in (1, 2):
            replay.orders[ident] = {'id': ident, 'side': 'buy', 'price': D(99),
                                   'quantity': D(1), 'active': True, 'cancel_time': None}
        replay.match(1000000000, ondo(1000, '98', '1.5', 'sell'))
        self.assertEqual(sum((f['quantity'] for f in replay.fills), D(0)), D('1.5'))

    def test_liquidation_fee_uses_adverse_execution_price(self):
        result = self.run_bt([ondo(1000, '98', '.5', 'sell')],
                             ondo_taker_fee_bps=10, ondo_half_spread_bps=100)
        mark, liquid = result['valuation']['mark'], result['valuation']['liquidate']
        exit_price = mark['ondo_price'] * D('.99')
        cost = D('.5') * (mark['ondo_price'] - exit_price) + D('.5') * exit_price * D('.001')
        self.assertEqual(mark['pnl'] - liquid['pnl'], cost)

    def test_zero_cancel_latency_replaces_at_same_timestamp(self):
        result = self.run_bt([ondo(1000, '98', '.5', 'sell')], [bn(2000, '101')],
                             requote_bps=1, cancel_latency_ms=0)
        cancels = [e for e in result['events'] if e['type'] == 'cancel_effective']
        self.assertTrue(cancels)
        for time_ms in {e['time_ms'] for e in cancels}:
            self.assertTrue(any(e['type'] == 'order_sent' and e['time_ms'] == time_ms
                                for e in result['events']))

    def test_constant_basis_has_no_drift_pnl_at_hedge_entry(self):
        result = self.run_bt([ondo(1000, '98', '.5', 'sell')],
                             hedge_mode='lighter_proxy', hedge_latency_ms=0,
                             lighter_basis_bps=10)
        self.assertEqual(result['totals']['basis_pnl'], 0)

    def test_hedge_waits_for_visible_fill_notification(self):
        result = self.run_bt([ondo(1000, '98', '.5', 'sell')],
                             data_latency_ms=500, hedge_mode='lighter_proxy',
                             hedge_latency_ms=100)
        hedges = [e for e in result['events'] if e['type'] == 'hedge']
        self.assertEqual(hedges[0]['time_ms'], 1600)

    def test_unnotified_fill_prevents_new_orders(self):
        result = self.run_bt([ondo(1000, '98', '2', 'sell')], [bn(1100, '101')],
                             data_latency_ms=500, requote_bps=1)
        self.assertFalse(any(e['type'] == 'order_sent' and 1000 <= e['time_ms'] < 1500
                             for e in result['events']))

    def test_minute_captures_counters_before_boundary_events(self):
        result = self.run_bt([ondo(60000, '98', '.5', 'sell')], end=120001)
        minute = result['minutes'][1]
        self.assertIn('before_events', minute)
        self.assertEqual(minute['before_events']['maker_fees'], 0)
        self.assertGreater(minute['maker_fees'], 0)
        self.assertEqual(minute['before_events']['pnl_mark'], 0)


if __name__ == '__main__':
    unittest.main()
