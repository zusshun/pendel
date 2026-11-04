"""M5 eligibility weights must match backtests and retain observations for both metrics."""

import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path

from pendle.maker import Decision, MarketRules, QuoteEngine
from pendle.maker_config import MakerSettings
from pendle.maker_orders import AccountState
from pendle.market import Book


MARKET = 'IBM-USD.P'


def depth(quantity='20'):
    return Book.make('ondo', 'IBM', 100000, 100000,
                     [('99.9', quantity), ('99', '10')],
                     [('100.1', quantity), ('101', '10')])


class GateWeightTests(unittest.TestCase):
    def evaluate(self, settings, *, book=None, tick='.01', mark=None):
        engine = QuoteEngine(settings)
        engine.mark = mark if mark is not None else (D(100), 100)
        engine.in_price_discovery = False
        engine.price_discovery_seen = mark[1] if mark else 100
        return engine.evaluate(book or depth(), AccountState([MARKET]),
                               MarketRules(D(tick), D('.001')), now=100)

    def test_default_flat_rejects_depth_that_linear_would_accept(self):
        settings = MakerSettings()
        result = self.evaluate(settings)
        self.assertFalse(result.eligible, result.reason)
        self.assertEqual(result.reason, '竞争量超过判据')
        self.assertEqual(getattr(settings, 'gate_weight', None), 'flat')
        self.assertEqual(result.competition, D(4000))

    def test_same_depth_has_different_gate_results_for_flat_and_linear(self):
        results = {weight: self.evaluate(MakerSettings.from_dict({'gate_weight': weight}))
                   for weight in ('flat', 'linear')}
        self.assertFalse(results['flat'].eligible)
        self.assertTrue(results['linear'].eligible, results['linear'].reason)
        for weight, result in results.items():
            with self.subTest(weight=weight):
                self.assertEqual(result.competition_flat, D(4000))
                self.assertEqual(result.competition_linear, D(2400))
                self.assertEqual(result.competition, getattr(result, 'competition_'+weight))

    def test_weight_loads_from_toml_and_example_defaults_to_flat(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'maker.toml'
            path.write_text('[maker]\ngate_weight = "linear"\n')
            self.assertEqual(MakerSettings.from_file(path).gate_weight, 'linear')
        example = Path(__file__).parents[1] / 'maker.example.toml'
        self.assertEqual(MakerSettings.from_file(example).gate_weight, 'flat')

    def test_invalid_weight_is_rejected(self):
        for value in ('quadratic', '', None, True, 1):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'gate_weight.*flat.*linear'):
                MakerSettings.from_dict({'gate_weight': value})

    def test_quote_and_mark_guards_keep_both_calculated_competitions(self):
        for kwargs in ({'tick': '1'}, {'mark': (D('100.05'), 100)}):
            with self.subTest(kwargs=kwargs):
                result = self.evaluate(MakerSettings(), book=depth('10'), **kwargs)
                self.assertFalse(result.eligible)
                self.assertEqual(getattr(result, 'competition_flat', None), D(2000))
                self.assertEqual(getattr(result, 'competition_linear', None), D(1200))
                self.assertEqual(result.competition, D(2000))

    def test_incomplete_depth_does_not_publish_partial_competitions(self):
        incomplete = Book.make('ondo', 'IBM', 100000, 100000,
            [('99.9', '10'), ('99', '10')], [('100.1', '10')])
        result = self.evaluate(MakerSettings(), book=incomplete)
        self.assertFalse(result.eligible)
        self.assertEqual(result.reason, '第三方盘口覆盖不完整')
        self.assertIsNone(getattr(result, 'competition_flat', 'missing'))
        self.assertIsNone(getattr(result, 'competition_linear', 'missing'))

    def test_decision_existing_positional_arguments_remain_compatible(self):
        result = Decision(True, '判据通过', D(100), (), D(1200))
        self.assertEqual(result.competition, D(1200))
        self.assertIsNone(getattr(result, 'competition_flat', 'missing'))
        self.assertIsNone(getattr(result, 'competition_linear', 'missing'))


if __name__ == '__main__':
    unittest.main()
