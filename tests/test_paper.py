import copy
import unittest
from unittest.mock import patch
from decimal import Decimal as D

from pendle import __version__
from pendle.config import Settings
from pendle.paper import DepthBudget, Simulator, replay
from pendle.storage import Sample
from test_recorder import book, metadata


def run(settings=None):
    return {'id': 'test-run', 'schema_version': 1, 'engine_version': __version__,
            'settings': (settings or Settings()).to_dict(), 'metadata': metadata()}


def sample(at, bid='99.9', ask='100.1', size='10', lighter_at=None,
           lighter_bid=None, lighter_ask=None, source_ms=None):
    o = book('ondo', at, bid, ask, size)
    if source_ms is not None:
        o = type(o).make(o.venue, o.symbol, at, source_ms, o.bids, o.asks)
    l = book('lighter', at if lighter_at is None else lighter_at,
             lighter_bid or bid, lighter_ask or ask, size)
    return Sample(max(at, l.received_ms), o, l)


def params(**changes):
    return Settings.from_dict({'quote_notional': '100', 'quote_offset_bps': '10',
                              'quantity_step': '0.001', 'max_basis_bps': '1000',
                              'max_jump_bps': '1000', **changes})


def events(report, kind):
    return [e for e in report['events'] if e['type'] == kind]


class PaperTests(unittest.TestCase):
    def test_no_fill_on_creation_or_touch_and_deterministic_replay(self):
        settings = params()
        data = [sample(1000), sample(2000)]
        first = replay(run(settings), data)
        self.assertEqual(events(first, 'maker_fill'), [])
        self.assertEqual(first, replay(run(settings), data))
        self.assertIsNone(first['reward_scenario_usdc'])
        self.assertIsNone(first['scenario_pnl_usdc'])

    def test_strict_crossing_executes_at_old_price_with_partial_size(self):
        settings = params(cancel_latency_ms=3000, hedge_latency_ms=5000)
        data = [sample(1000), sample(2000, '99.7', '99.8', '0.3')]
        report = replay(run(settings), data)
        fill = events(report, 'maker_fill')[0]
        self.assertEqual(D(fill['price']), D('99.9'))
        self.assertEqual(D(fill['quantity']), D('0.3'))
        self.assertEqual(D(report['positions']['ondo']), D('0.3'))
        self.assertGreater(D(report['pending_hedge_quantity']), 0)

    def test_repeated_unchanged_book_does_not_replenish_liquidity(self):
        budget = DepthBudget()
        original = book('lighter', 1000, size='1')
        budget.update(original)
        self.assertEqual(budget.take('bids', D('99'), D('0.8')), D('0.8'))
        budget.update(book('lighter', 2000, size='1'))
        self.assertEqual(budget.take('bids', D('99'), D('1')), D('0.2'))
        budget.update(book('lighter', 3000, size='1.5'))
        self.assertEqual(budget.take('bids', D('99'), D('1')), D('0.5'))

    def test_unchanged_crossed_book_does_not_refill_partial_order(self):
        settings = params(cancel_latency_ms=5000, hedge_latency_ms=5000)
        data = [sample(1000), sample(2000, '99.7', '99.8', '.3'),
                sample(3000, '99.7', '99.8', '.3')]
        report = replay(run(settings), data)
        self.assertEqual(sum((D(e['quantity']) for e in events(report, 'maker_fill')), D(0)), D('.3'))

    def test_hedge_honors_market_minimum_notional(self):
        settings = params(hedge_latency_ms=0)
        report = replay(run(settings), [sample(1000), sample(2000, '99.7', '99.8', '.05')])
        self.assertEqual(events(report, 'hedge_fill'), [])
        self.assertIn('hedge_minimum', report['risk_reasons'])
        self.assertEqual(D(report['pending_hedge_quantity']), D('.05'))

    def test_known_offsetting_fills_cancel_old_hedge_before_due(self):
        settings = params(hedge_latency_ms=5000, cancel_latency_ms=5000,
                          max_loss_usd='0.01', quantity_step='1', quote_notional='101')
        data = [sample(1000), sample(2000, '99.7', '99.8', '1'),
                sample(3000, '100.2', '100.3', '1'),
                sample(7000), sample(8000)]
        report = replay(run(settings), data)
        self.assertEqual(D(report['positions']['ondo']), D(0))
        self.assertEqual(events(report, 'hedge_fill'), [])
        self.assertEqual(D(report['net_quantity']), D(0))

    def test_existing_unchanged_quotes_recheck_potential_inventory(self):
        settings = params(quote_notional='101', quote_offset_bps='1',
                          quantity_step='1', price_tick='0.01', max_inventory_usd='100.005')
        data = [sample(1000, '100.00', '100.01'), sample(2000, '100.002', '100.010')]
        report = replay(run(settings), data)
        self.assertIn('inventory', report['risk_reasons'])
        self.assertTrue(report['halted'])

    def test_replay_processes_iterator_incrementally(self):
        count = []
        original = Simulator.step
        def observe(simulator, row):
            original(simulator, row)
            count.append(row.timestamp_ms)
        def rows():
            yield sample(1000)
            self.assertEqual(count, [1000], '不得在处理之前预先载入全部历史')
            yield sample(2000)
        with patch.object(Simulator, 'step', observe):
            self.assertEqual(replay(run(params()), rows())['sample_count'], 2)

    def test_cancel_pending_can_fill_before_effective_time(self):
        settings = params(cancel_latency_ms=1000, hedge_latency_ms=5000)
        data = [sample(1000), sample(2000, '100', '100.2'),
                sample(2500, '99.7', '99.8')]
        report = replay(run(settings), data)
        self.assertTrue(events(report, 'cancel_request'))
        self.assertEqual(events(report, 'maker_fill')[0]['at_ms'], 2500)

    def test_hedge_uses_only_source_received_after_due(self):
        settings = params(hedge_latency_ms=500, cancel_latency_ms=0)
        data = [sample(1000), sample(2000, '99.7', '99.8'),
                sample(2600, '99.7', '99.8', lighter_at=2400),
                sample(3000, '99.7', '99.8', lighter_at=2900)]
        report = replay(run(settings), data)
        hedges = events(report, 'hedge_fill')
        self.assertTrue(hedges)
        self.assertGreaterEqual(hedges[0]['at_ms'], 2500)
        self.assertEqual(hedges[0]['at_ms'], 2900)
        self.assertGreaterEqual(report['observed_hedge_wait_ms'][0], 500)

    def test_depth_shortfall_preserves_exposure_and_halts(self):
        settings = params(hedge_latency_ms=0, cancel_latency_ms=0)
        second = sample(2000, '99.7', '99.8')
        second = Sample(2000, second.ondo, book('lighter', 2000, '99.7', '99.8', '0.1'))
        report = replay(run(settings), [sample(1000), second])
        self.assertTrue(report['halted'])
        self.assertGreater(D(report['net_quantity']), 0)
        self.assertIn('hedge_depth', report['risk_reasons'])

    def test_invalid_session_identity_versions_and_order_are_rejected(self):
        for mutate in (lambda r: r.update(schema_version=99),
                       lambda r: r.update(engine_version='future'),
                       lambda r: r['metadata'].update(symbol='BTC'),
                       lambda r: r['metadata']['lighter_market'].update(multiplier='2')):
            value = copy.deepcopy(run())
            mutate(value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                replay(value, [sample(1000)])
        with self.assertRaises(ValueError):
            replay(run(), [sample(2000), sample(1000)])


class RiskAndAccountingTests(unittest.TestCase):
    def test_freshness_checks_total_source_age_at_decision_time(self):
        settings = params(max_stale_ms=6000, max_skew_ms=2000)
        report = replay(run(settings), [sample(5000, lighter_at=7000, source_ms=0)])
        self.assertIn('stale', report['risk_reasons'])
        self.assertEqual(events(report, 'quote'), [])

    def test_quote_observation_time_stops_at_cancel_effective_time(self):
        settings = params(cancel_latency_ms=500, max_stale_ms=6000)
        report = replay(run(settings), [sample(1000), sample(2000, '100', '100.2'),
                                        sample(5000, '100', '100.2')])
        self.assertEqual(report['valid_quote_observation_ms'], 1500)

    def test_quote_time_handles_cancellation_created_inside_sample(self):
        settings = params(cancel_latency_ms=500, hedge_latency_ms=500, max_net_usd='100.11')
        report = replay(run(settings), [sample(1000),
                                        sample(2000, '100.2', '100.3', lighter_at=3000)])
        self.assertIn('net_exposure', report['risk_reasons'])
        self.assertEqual(report['valid_quote_observation_ms'], 1500)

    def test_gap_with_live_quotes_marks_incomplete_history(self):
        settings = params(max_stale_ms=3000)
        report = replay(run(settings), [sample(1000), sample(10000)])
        self.assertTrue(report['execution_history_incomplete'])
        self.assertTrue(report['halted'])
        self.assertEqual(report['max_sample_gap_ms'], 9000)
        self.assertEqual(events(report, 'maker_fill'), [])

    def test_missing_or_stale_data_halts_and_does_not_forge_close(self):
        settings = params(hedge_latency_ms=5000)
        data = [sample(1000), sample(2000, '99.7', '99.8'),
                Sample(20000, None, None, {'ondo': '失败'})]
        report = replay(run(settings), data)
        self.assertTrue(report['halted'])
        self.assertIsNone(report['pnl_before_rewards_usdc'])
        self.assertGreater(D(report['positions']['ondo']), 0)

    def test_basis_skew_source_staleness_and_jump_stop_quotes(self):
        scenarios = [([sample(1000, lighter_bid='109.9', lighter_ask='110.1')], 'basis'),
                     ([sample(4000, lighter_at=1000)], 'skew'),
                     ([sample(10000, source_ms=1000)], 'stale'),
                     ([sample(1000), sample(2000, '109.9', '110.1')], 'jump')]
        settings = params(max_basis_bps='50', max_jump_bps='50')
        for rows, reason in scenarios:
            with self.subTest(reason=reason):
                report = replay(run(settings), rows)
                self.assertIn(reason, report['risk_reasons'])
                self.assertTrue(report['halted'])

    def test_potential_same_side_inventory_blocks_quote(self):
        settings = params(max_inventory_usd='50')
        report = replay(run(settings), [sample(1000)])
        self.assertEqual(events(report, 'quote'), [])
        self.assertIn('inventory', report['risk_reasons'])

    def test_cash_fees_and_bbo_valuation_by_hand(self):
        settings = params(hedge_latency_ms=0, cancel_latency_ms=0,
                          extra_slippage_bps='0', ondo_maker_fee_bps='1',
                          lighter_taker_fee_bps='2')
        report = replay(run(settings), [sample(1000), sample(2000, '99.7', '99.8', '1')])
        # Buy 1@99.9 and hedge by selling 1@99.7; value final longs at 99.7 and shorts at 99.8.
        expected_fees = D('99.9') * D('.0001') + D('99.7') * D('.0002')
        expected = D('-.3') - expected_fees
        self.assertEqual(D(report['fees_usdc']), expected_fees)
        self.assertEqual(D(report['pnl_before_rewards_usdc']), expected)
        self.assertEqual(report['valuation_method'], 'bbo_unrealized_excludes_exit_costs')

    def test_funding_uses_previous_position_and_long_pays_short_receives(self):
        settings = params(hedge_latency_ms=0, cancel_latency_ms=0, max_stale_ms=4000000,
                          max_loss_usd='100', extra_slippage_bps='0',
                          ondo_funding_bps_per_hour='10', lighter_funding_bps_per_hour='5')
        data = [sample(1000), sample(2000, '99.7', '99.8', '1'),
                sample(3602000, '99.7', '99.8', '1')]
        report = replay(run(settings), data)
        expected = -D('99.75') * D('.001') + D('99.75') * D('.0005')
        self.assertEqual(D(report['funding_pnl_usdc']), expected)

    def test_reward_is_explicit_scenario_not_official_payment(self):
        settings = params(reward_usdc='5')
        report = replay(run(settings), [sample(1000)])
        self.assertEqual(D(report['scenario_pnl_usdc']), D(report['pnl_before_rewards_usdc']) + 5)
        self.assertTrue(report['limitations'])
        self.assertNotIn('apr', report)
