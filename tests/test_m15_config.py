"""M15 标记价格静默、REST 确认和价格发现时效配置。"""

import unittest
from pathlib import Path

from pendle.maker_config import MakerSettings


MARK_TIMING_DEFAULTS = {
    'mark_quiet_max_s': 60.0,
    'mark_rest_confirm_s': 15.0,
    'mark_rest_poll_after_s': 10.0,
    'mark_rest_poll_interval_s': 10.0,
    'mark_source_max_age_s': 3600.0,
    'discovery_quiet_max_s': 300.0,
}


class M15MarkTimingConfigTests(unittest.TestCase):
    def test_mark_timing_defaults_separate_quiet_confirmation_source_and_discovery(self):
        settings = MakerSettings()
        for name, expected in MARK_TIMING_DEFAULTS.items():
            with self.subTest(name=name):
                self.assertEqual(getattr(settings, name, None), expected)

    def test_mark_timing_options_parse_positive_finite_numbers(self):
        values = {name: str(default + .5) for name, default in MARK_TIMING_DEFAULTS.items()}
        try:
            settings = MakerSettings.from_dict(values)
        except ValueError as exc:
            self.fail(f'M15 时效配置应能加载：{exc}')
        for name, value in values.items():
            with self.subTest(name=name):
                self.assertEqual(getattr(settings, name), float(value))

    def test_mark_timing_options_reject_nonpositive_nonfinite_and_boolean_values(self):
        for name in MARK_TIMING_DEFAULTS:
            for value in (0, -1, 'NaN', 'Infinity', True, False):
                with self.subTest(name=name, value=value):
                    with self.assertRaisesRegex(ValueError, f'{name}.*大于零.*有限数字'):
                        MakerSettings.from_dict({name: value})

    def test_source_freshness_option_is_removed_instead_of_silently_accepted(self):
        self.assertFalse(hasattr(MakerSettings(), 'max_mark_age_s'))
        with self.assertRaisesRegex(ValueError, '未知.*max_mark_age_s'):
            MakerSettings.from_dict({'max_mark_age_s': 10})

    def test_example_uses_explicit_m15_mark_timing_defaults(self):
        settings = MakerSettings.from_file(Path(__file__).parents[1] / 'maker.example.toml')
        for name, expected in MARK_TIMING_DEFAULTS.items():
            with self.subTest(name=name):
                self.assertEqual(getattr(settings, name, None), expected)


if __name__ == '__main__':
    unittest.main()
