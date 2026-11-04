"""批次 D：即时套利损失、旧仓漂移与实采可见时间回归。"""

import unittest
from dataclasses import replace
from decimal import Decimal as D

from pendle.backtest import _Replay, NS_MS, run_backtest
from pendle.bt_config import BacktestSettings
from tests.test_backtest import bn, ondo


class DBacktestTests(unittest.TestCase):
    def setUp(self):
        self.settings = BacktestSettings(quote_notional=100, quote_offset_bps=100,
            max_inventory_usd=1000, data_latency_ms=0, place_latency_ms=0,
            cancel_latency_ms=0, max_ref_age_s=10000, guard_move_bps=10000,
            requote_bps=10000, reward_two_sided=False, pickoff_enabled=False)

    def replay(self, trades=(), prices=(), end=20001, **changes):
        return run_backtest([ondo(0), ondo(0, side='sell', ident='seed')] + list(trades),
            [bn(0)] + list(prices), replace(self.settings, **changes),
            symbol='INTC', start_ms=0, end_ms=end)

    def test_pickoff_pnl_uses_fill_center_and_preserves_hold_to_end(self):
        for price in ('102', '98'):
            with self.subTest(price=price):
                result = self.replay(prices=[bn(1000, price), bn(2000, '105')],
                                     pickoff_enabled=True, end=3000)
                fills = [fill for fill in result['fills'] if fill['fill_type'] == 'pickoff']
                self.assertTrue(fills)
                immediate = sum(((fill['center'] - fill['price']) * fill['quantity'] *
                    (1 if fill['side'] == 'buy' else -1) - fill['fee'] for fill in fills), D(0))
                held = sum(((result['valuation']['mark']['ondo_price'] - fill['price']) *
                    fill['quantity'] * (1 if fill['side'] == 'buy' else -1) - fill['fee']
                    for fill in fills), D(0))
                self.assertLess(immediate, 0)
                self.assertNotEqual(immediate, held)
                self.assertEqual(result['totals']['pickoff_pnl'], immediate)
                self.assertEqual(result['totals']['pickoff_hold_to_end_pnl'], held)

    def test_basis_drift_accrues_on_old_paired_position_before_pickoff(self):
        replay = _Replay([], [], replace(self.settings, pickoff_enabled=True),
                         'INTC', 0, 2000, [], [])
        for side in ('buy', 'sell'):
            replay.add_sample(0, 0, side, D('.01'), replay.basis_queue,
                              replay.basis_values, 10000, 'basis_expired')
        replay.reference, replay.reference_time = D(100), 0
        replay.refresh_marks(0)
        replay.ondo_position, replay.lighter_position = D(1), D(-1)
        replay.orders[1] = dict(id=1, side='sell', price=D(111), quantity=D(1),
                                active=True, cancel_time=None)
        replay.handle(1000 * NS_MS, 'binance_visible', bn(1000, '110'))
        self.assertEqual(replay.ondo_position, 0)
        self.assertEqual(replay.totals['pickoff_count'], 1)
        self.assertEqual(replay.totals['basis_pnl'], D('.1'))

    def test_binance_received_time_delays_reference_and_pickoff(self):
        result = self.replay(prices=[{**bn(1000, '102'), 'received_ms': 11000}],
            data_latency_ms=100, pickoff_enabled=True)
        fills = [fill for fill in result['fills'] if fill['fill_type'] == 'pickoff']
        self.assertEqual([fill['time_ms'] for fill in fills], [11000])
        self.assertTrue(all(row['price'] == D(100) for row in result['center_prices']
                            if row['price'] is not None and row['time_ms'] < 11000))

    def test_ondo_received_time_delays_basis_but_not_actual_trade_matching(self):
        result = self.replay(trades=[{**ondo(1000, '98', '.5', 'sell'),
                                      'received_ms': 11000}], data_latency_ms=100)
        self.assertEqual(result['fills'][0]['time_ms'], 1000)
        self.assertTrue(all(row['price'] == D(100) for row in result['center_prices']
                            if row['price'] is not None and row['time_ms'] < 11000))
        self.assertEqual(next(row['price'] for row in result['center_prices']
                              if row['time_ms'] == 11000), D('99.5'))

    def test_nullable_or_early_received_time_keeps_configured_latency(self):
        for received in (None, 500):
            with self.subTest(received=received):
                result = self.replay(prices=[{**bn(1000, '102'), 'received_ms': received}],
                    data_latency_ms=100, pickoff_enabled=True)
                self.assertEqual(result['fills'][0]['time_ms'], 1100)

    def test_late_older_binance_trade_cannot_rewind_reference(self):
        result = self.replay(prices=[{**bn(1000, '102'), 'received_ms': 11000},
                                     bn(2000, '101')])
        self.assertEqual([row['price'] for row in result['center_prices']
                          if row['time_ms'] <= 11000][-1], D(101))

    def test_late_ondo_sample_expires_by_exchange_time(self):
        replay = _Replay([], [], self.settings, 'INTC', 0, 10000, [], [])
        for now, stamp, value in ((2000, 2000, '.02'), (2100, 1000, '0')):
            replay.add_sample(now * NS_MS, stamp * NS_MS, 'buy', D(value),
                replay.basis_queue, replay.basis_values, D('1.5'), 'basis_expired')
        self.assertEqual(replay.side_median(3001 * NS_MS, 'buy', replay.basis_queue,
            replay.basis_values, D('1.5')), D('.02'))

    def test_late_ondo_sample_cannot_evict_a_newer_sample_at_count_limit(self):
        replay = _Replay([], [], replace(self.settings, basis_samples=1),
                         'INTC', 0, 10000, [], [])
        for now, stamp, value in ((2000, 2000, '.02'), (2100, 1000, '0')):
            replay.add_sample(now * NS_MS, stamp * NS_MS, 'buy', D(value),
                replay.basis_queue, replay.basis_values, D('1.5'), 'basis_expired')
        self.assertEqual(replay.side_median(2100 * NS_MS, 'buy', replay.basis_queue,
            replay.basis_values, D('1.5')), D('.02'))

    def test_late_ondo_trade_does_not_rewind_post_only_side_prices(self):
        for side, attribute in (('buy', 'best_ask'), ('sell', 'best_bid')):
            with self.subTest(side=side):
                replay = _Replay([], [], self.settings, 'INTC', 0, 10000, [], [])
                replay.handle(2000 * NS_MS, 'ondo_visible', ondo(2000, '101', side=side))
                replay.handle(3000 * NS_MS, 'ondo_visible', ondo(1000, '99', side=side))
                self.assertEqual(getattr(replay, attribute), D(101))


if __name__ == '__main__':
    unittest.main()
