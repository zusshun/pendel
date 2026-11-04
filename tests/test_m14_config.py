"""M14 source hysteresis and ultra-slow anchor configuration must load correctly and preserve safety boundaries."""

import tempfile
import tomllib
import unittest
from decimal import Decimal
from pathlib import Path

from pendle.maker_config import MakerSettings


class M14SettingsTests(unittest.TestCase):
    def test_source_dwell_and_slow_anchor_defaults(self):
        settings = MakerSettings()
        self.assertEqual(getattr(settings, 'source_min_dwell_s', None), 30.0)
        self.assertEqual(getattr(settings, 'slow_anchor_tau_s', None), 3600.0)
        self.assertEqual(getattr(settings, 'max_slow_drift_bps', None), Decimal('150'))
        self.assertEqual(getattr(settings, 'max_mark_phase_drift_bps', None), Decimal('300'))

    def test_new_settings_load_and_convert_from_mapping(self):
        for name in ('source_min_dwell_s', 'slow_anchor_tau_s', 'max_slow_drift_bps',
                     'max_mark_phase_drift_bps'):
            self.assertTrue(hasattr(MakerSettings, name), name)
        settings = MakerSettings.from_dict({'source_min_dwell_s': '45.5',
                                            'slow_anchor_tau_s': '7200.5',
                                            'max_slow_drift_bps': '35.125',
                                            'max_mark_phase_drift_bps': '275.125'})
        self.assertEqual(settings.source_min_dwell_s, 45.5)
        self.assertEqual(settings.slow_anchor_tau_s, 7200.5)
        self.assertEqual(settings.max_slow_drift_bps, Decimal('35.125'))
        self.assertEqual(settings.max_mark_phase_drift_bps, Decimal('275.125'))

    def test_new_settings_load_from_toml_without_losing_decimal_precision(self):
        for name in ('source_min_dwell_s', 'slow_anchor_tau_s', 'max_slow_drift_bps',
                     'max_mark_phase_drift_bps'):
            self.assertTrue(hasattr(MakerSettings, name), name)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'maker.toml'
            path.write_text('[maker]\nsource_min_dwell_s = 35.5\n'
                            'slow_anchor_tau_s = 4000.5\n'
                            'max_slow_drift_bps = 39.00000000000000001\n'
                            'max_mark_phase_drift_bps = 299.00000000000000001\n')
            settings = MakerSettings.from_file(path)
        self.assertEqual(settings.source_min_dwell_s, 35.5)
        self.assertEqual(settings.slow_anchor_tau_s, 4000.5)
        self.assertEqual(settings.max_slow_drift_bps, Decimal('39.00000000000000001'))
        self.assertEqual(settings.max_mark_phase_drift_bps, Decimal('299.00000000000000001'))

    def test_new_settings_reject_nonpositive_nonfinite_and_boolean_values(self):
        for name in ('source_min_dwell_s', 'slow_anchor_tau_s', 'max_slow_drift_bps',
                     'max_mark_phase_drift_bps'):
            for value in (0, -1, 'NaN', 'Infinity', '-Infinity', True, False):
                with self.subTest(name=name, value=value):
                    with self.assertRaisesRegex(ValueError, name + ' 必须是大于零的有限数字'):
                        MakerSettings.from_dict({name: value})

    def test_narrow_spread_leaves_positive_hysteresis_exit_threshold(self):
        for value in ('5', '4.9'):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, 'narrow_spread_bps.*5'):
                    MakerSettings(narrow_spread_bps=value)
        self.assertEqual(MakerSettings(narrow_spread_bps='5.1').narrow_spread_bps,
                         Decimal('5.1'))

    def test_example_explicitly_sets_source_dwell_and_slow_anchor_limits(self):
        path = Path(__file__).parents[1] / 'maker.example.toml'
        with path.open('rb') as stream:
            values = tomllib.load(stream)['maker']
        self.assertEqual(values.get('source_min_dwell_s'), 30.0)
        self.assertEqual(values.get('slow_anchor_tau_s'), 3600.0)
        self.assertEqual(values.get('max_slow_drift_bps'), '150')
        self.assertEqual(values.get('max_mark_phase_drift_bps'), '300')
        settings = MakerSettings.from_file(path)
        self.assertEqual(settings.max_slow_drift_bps, Decimal('150'))
        self.assertEqual(settings.max_mark_phase_drift_bps, Decimal('300'))


if __name__ == '__main__':
    unittest.main()
