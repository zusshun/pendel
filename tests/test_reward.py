import unittest
from decimal import Decimal

from pendle import reward


D = Decimal


class RewardWeightTests(unittest.TestCase):
    def test_distance_is_absolute_and_uses_decimal(self):
        self.assertEqual(reward.distance_bps('99.9', '100'), D('10'))
        self.assertEqual(reward.distance_bps('100.1', '100'), D('10'))
        self.assertEqual(reward.distance_bps('100', '100'), D('0'))

    def test_mid_error_expands_distance(self):
        self.assertEqual(reward.distance_bps('99.9', '100', '3'), D('13'))

    def test_flat_includes_band_boundary_and_excludes_outside(self):
        for distance, expected in (('0', '1'), ('25', '1'), ('25.001', '0')):
            with self.subTest(distance=distance):
                self.assertEqual(reward.distance_weight(distance, '25'), D(expected))

    def test_linear_weight_declines_to_zero_at_boundary(self):
        for distance, expected in (('0', '1'), ('10', '.6'), ('25', '0'), ('26', '0')):
            with self.subTest(distance=distance):
                self.assertEqual(reward.distance_weight(distance, '25', 'linear'), D(expected))

    def test_quote_weight_applies_mid_error_once(self):
        self.assertEqual(reward.quote_weight('99.8', '100', '25', 'linear', '3'), D('.08'))
        self.assertEqual(reward.quote_weight('99.8', '100', '25', 'flat', '6'), D('0'))

    def test_invalid_weight_inputs_are_rejected(self):
        invalid = ((D('NaN'), '25', 'flat', '0'), ('1', 'Infinity', 'flat', '0'),
                   ('-1', '25', 'flat', '0'), ('1', '0', 'flat', '0'),
                   ('1', '25', 'quadratic', '0'), ('1', '25', 'flat', '-1'),
                   (True, '25', 'flat', '0'))
        for args in invalid:
            with self.subTest(args=args), self.assertRaises(ValueError):
                reward.distance_weight(*args)
        for price, mid in (('0', '100'), ('100', '-1'), ('NaN', '100')):
            with self.subTest(price=price, mid=mid), self.assertRaises(ValueError):
                reward.distance_bps(price, mid)

    def test_eligible_weight_sums_two_sides_notional(self):
        result = reward.eligible_weight([('99.9', '2')], [('100.1', '3')], '100')
        self.assertEqual(result, D('500.1'))

    def test_linear_eligible_weight_uses_each_quote_distance(self):
        result = reward.eligible_weight([('99.9', '2')], [('100.2', '3')], '100',
                                        weight_function='linear')
        self.assertEqual(result, D('180'))

    def test_required_two_sides_rejects_missing_or_outside_side(self):
        for asks in ([], [('100.3', '3')], [('100.25', '3')]):
            with self.subTest(asks=asks):
                self.assertEqual(reward.eligible_weight([('99.9', '2')], asks, '100',
                                                       weight_function='linear'), D('0'))

    def test_one_sided_rewards_are_parameterized(self):
        self.assertEqual(reward.eligible_weight([('99.9', '2')], [], '100',
                                               reward_two_sided=False), D('199.8'))

    def test_minimum_quote_applies_to_each_order_before_weighting(self):
        bids = [('99.9', '.5'), ('99.8', '.5')]
        self.assertEqual(reward.eligible_weight(bids, [('100.1', '1')], '100',
                                               min_quote_usd='60'), D('0'))
        self.assertEqual(reward.eligible_weight([('99.9', '1')], [('100.1', '1')],
                                               '100', min_quote_usd='99.9',
                                               weight_function='linear'), D('120'))

    def test_mid_error_can_remove_two_sided_eligibility(self):
        self.assertEqual(reward.eligible_weight([('99.8', '1')], [('100.1', '1')],
                                               '100', reward_mid_error_bps='6'), D('0'))

    def test_invalid_quotes_and_eligibility_options_are_rejected(self):
        for bids, options in (([('100', '-1')], {}), ([('Infinity', '1')], {}),
                              ([('100', '1')], {'min_quote_usd': '-1'}),
                              ([('100', '1')], {'reward_two_sided': 1})):
            with self.subTest(bids=bids, options=options), self.assertRaises(ValueError):
                reward.eligible_weight(bids, [('100.1', '1')], '100', **options)


class MinuteRewardTests(unittest.TestCase):
    def test_minute_share_handles_empty_and_zero_competition(self):
        self.assertEqual(reward.minute_share('0', '0'), D('0'))
        self.assertEqual(reward.minute_share('0', '100'), D('0'))
        self.assertEqual(reward.minute_share('20', '0'), D('1'))
        self.assertEqual(reward.minute_share('20', '80'), D('.2'))

    def test_reward_divides_weekly_total_by_market_count_and_week_minutes(self):
        self.assertEqual(reward.minute_reward('20', '80', '100800', 10), D('.2'))

    def test_total_reward_keeps_every_minute_including_ineligible_minutes(self):
        weights = (value for value in ('0', '20', '80'))
        self.assertEqual(reward.total_reward(weights, '80', '100800', 10), D('.7'))

    def test_zero_pool_produces_zero_reward(self):
        self.assertEqual(reward.minute_reward('20', '0', '0', 1), D('0'))

    def test_invalid_reward_parameters_are_rejected(self):
        for args in (('-1', '0', '100', 1), ('1', '-1', '100', 1),
                     ('1', '0', '-100', 1), ('1', '0', 'NaN', 1),
                     ('1', '0', '100', 0), ('1', '0', '100', True),
                     ('1', '0', '100', 1.5)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                reward.minute_reward(*args)


class BreakEvenRewardTests(unittest.TestCase):
    def test_constant_weights_have_known_break_even_competition(self):
        result = reward.break_even_comp(['100', '100'], '-1', '10080')
        self.assertEqual(result['status'], 'break_even')
        self.assertAlmostEqual(result['comp'], D('100'), places=20)

    def test_varying_weights_solve_total_rewards_not_average_weight(self):
        result = reward.break_even_comp(['100', '0', '300'], '-1.25', '10080')
        self.assertEqual(result['status'], 'break_even')
        self.assertAlmostEqual(result['comp'], D('100'), places=20)
        self.assertAlmostEqual(reward.total_reward(['100', '0', '300'], result['comp'],
                                                  '10080'), D('1.25'), places=20)

    def test_widely_different_weights_converge_to_small_competition(self):
        weights = [D('1e100'), D('1')]
        result = reward.break_even_comp(weights, '-1.5', '10080')
        self.assertEqual(result['status'], 'break_even')
        self.assertLess(abs(result['comp'] - D('1')), D('1e-20'))
        self.assertAlmostEqual(reward.total_reward(weights, result['comp'], '10080'),
                               D('1.5'), places=20)

    def test_integer_target_share_avoids_rounded_reward_plateau(self):
        result = reward.break_even_comp([D('1e100'), D('1')], '-1', '10080')
        self.assertEqual(result['status'], 'break_even')
        self.assertLess(abs(result['comp'] / D('1e50') - D('1')), D('1e-20'))

    def test_break_even_at_zero_competition_is_valid_solution(self):
        result = reward.break_even_comp(['100', '0'], '-1', '10080')
        self.assertEqual(result, {'status': 'break_even', 'comp': D('0')})

    def test_nonnegative_pnl_needs_no_rewards(self):
        for pnl in ('0', '1'):
            with self.subTest(pnl=pnl):
                self.assertEqual(reward.break_even_comp(['0'], pnl, '10080'),
                                 {'status': 'no_rewards_needed', 'comp': None})

    def test_insufficient_zero_competition_reward_has_no_solution(self):
        self.assertEqual(reward.break_even_comp(['100', '0'], '-1.01', '10080'),
                         {'status': 'unprofitable_even_at_zero_comp', 'comp': None})

    def test_entire_window_ineligible_has_no_solution(self):
        for weights in ([], ['0', '0']):
            with self.subTest(weights=weights):
                self.assertEqual(reward.break_even_comp(weights, '-1', '10080'),
                                 {'status': 'never_eligible', 'comp': None})

    def test_required_pool_is_per_market_weekly_amount(self):
        self.assertEqual(reward.required_pool_per_market(['100', '300'], '-1.25', '100'),
                         D('10080'))

    def test_required_pool_handles_no_reward_need_and_no_eligibility(self):
        self.assertEqual(reward.required_pool_per_market([], '1', '100'), D('0'))
        self.assertIsNone(reward.required_pool_per_market(['0'], '-1', '100'))

    def test_reward_solvers_reject_nonfinite_and_negative_weights(self):
        for weights, pnl in ((['-1'], '-1'), (['NaN'], '-1'), (['1'], 'NaN')):
            with self.subTest(weights=weights, pnl=pnl), self.assertRaises(ValueError):
                reward.break_even_comp(weights, pnl, '10080')
        with self.assertRaises(ValueError):
            reward.required_pool_per_market(['1'], '-1', '-1')


if __name__ == '__main__':
    unittest.main()
