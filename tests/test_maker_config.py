"""Live configuration must default to read-only mode and reject unsafe or misspelled parameters explicitly."""

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from pendle.maker_config import MakerSettings


class MakerSettingsTests(unittest.TestCase):
    def test_default_is_read_only_single_market_with_code_caps(self):
        settings = MakerSettings()
        self.assertEqual(settings.mode, 'dry-run')
        self.assertEqual(settings.market, 'IBM-USD.P')
        self.assertEqual(settings.quote_notional, Decimal('400'))
        self.assertEqual(settings.soft_cap_usd, Decimal('300'))
        self.assertEqual(settings.hard_cap_usd, Decimal('600'))
        self.assertEqual(settings.offset_bps, Decimal('12'))
        self.assertEqual(settings.skew_bps, Decimal('4'))
        self.assertEqual(settings.min_offset_bps, Decimal('8'))
        self.assertEqual(settings.band_bps - settings.band_margin_bps, Decimal('22'))
        self.assertEqual(settings.risk_limits.quote_notional, Decimal('400'))
        self.assertFalse(settings.guards_calibrated)
        self.assertFalse(settings.sandbox_verified)

    def test_example_file_loads_without_enabling_live(self):
        settings = MakerSettings.from_file(Path(__file__).parents[1] / 'maker.example.toml')
        self.assertEqual(settings.mode, 'dry-run')
        self.assertEqual(settings.gate_comp_max_usd, Decimal('3500'))
        self.assertEqual(settings.window_start_day, 5)
        self.assertEqual(settings.window_start_time, '09:00')
        self.assertEqual(settings.window_end_day, 0)
        self.assertEqual(settings.window_end_time, '05:00')
        self.assertEqual(settings.timezone, 'Asia/Shanghai')

    def test_quote_center_defaults_support_normal_wide_spreads(self):
        settings = MakerSettings()
        self.assertEqual(getattr(settings, 'narrow_spread_bps', None), Decimal('20'))
        self.assertEqual(getattr(settings, 'max_mid_mark_bps', None), Decimal('8'))
        self.assertEqual(settings.max_spread_bps, Decimal('300'))
        self.assertEqual(settings.mark_quiet_max_s, 60.0)

    def test_quote_center_thresholds_parse_decimal_and_reject_invalid_values(self):
        self.assertTrue(hasattr(MakerSettings, 'narrow_spread_bps'))
        self.assertTrue(hasattr(MakerSettings, 'max_mid_mark_bps'))
        settings = MakerSettings.from_dict({'narrow_spread_bps': '18.5',
                                            'max_mid_mark_bps': '7.5'})
        self.assertEqual(settings.narrow_spread_bps, Decimal('18.5'))
        self.assertEqual(settings.max_mid_mark_bps, Decimal('7.5'))
        for name in ('narrow_spread_bps', 'max_mid_mark_bps', 'max_spread_bps',
                     'mark_quiet_max_s'):
            for value in (0, -1, 'NaN', 'Infinity', True):
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    MakerSettings.from_dict({name: value})

    def test_narrow_spread_threshold_cannot_exceed_extreme_spread_limit(self):
        with self.assertRaisesRegex(ValueError, 'narrow_spread_bps.*max_spread_bps'):
            MakerSettings.from_dict({'narrow_spread_bps': '301', 'max_spread_bps': '300'})
        settings = MakerSettings.from_dict({'narrow_spread_bps': '300',
                                            'max_spread_bps': '300'})
        self.assertEqual(settings.narrow_spread_bps, settings.max_spread_bps)

    def test_example_uses_independent_binance_guard_and_revised_center_limits(self):
        settings = MakerSettings.from_file(Path(__file__).parents[1] / 'maker.example.toml')
        self.assertEqual(settings.binance_symbol, 'IBMUSDT')
        self.assertEqual(settings.narrow_spread_bps, Decimal('20'))
        self.assertEqual(settings.max_mid_mark_bps, Decimal('8'))
        self.assertEqual(settings.max_spread_bps, Decimal('300'))
        self.assertEqual(settings.mark_quiet_max_s, 60.0)

    def test_toml_requires_maker_table_and_rejects_unknown_options(self):
        for content in ('quote_notional = 400', '[maker]\nquote_notionl = 400',
                        '[maker]\n[other]\nmode="live"'):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'maker.toml'
                path.write_text(content)
                with self.assertRaises(ValueError):
                    MakerSettings.from_file(path)

    def test_code_caps_cannot_be_overridden(self):
        for kwargs in ({'quote_notional': '800.01'}, {'max_position_usd': '1200.01'},
                       {'max_account_exposure_usd': '1500.01'}, {'max_margin_ratio': '1.01'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                MakerSettings(**kwargs)

    def test_finite_positive_values_and_integer_types_are_validated(self):
        for kwargs in ({'quote_notional': 'NaN'}, {'soft_cap_usd': 0},
                       {'max_book_age_s': -1}, {'max_book_age_s': float('inf')},
                       {'rate_per_second': True}, {'quiet_s': False},
                       {'window_start_day': True}, {'deadman_timeout_s': 0},
                       {'flatten_attempts': 4}, {'unknown_query_attempts': 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                MakerSettings(**kwargs)

    def test_relationships_cannot_disable_safety(self):
        for kwargs in ({'soft_cap_usd': 600}, {'hard_cap_usd': 1201},
                       {'offset_bps': 24}, {'min_offset_bps': 13},
                       {'anchor_ema_alpha': '1.1'}, {'max_flatten_slippage_bps': 10000},
                       {'min_requote_interval_s': 0}, {'unknown_timeout_s': 61},
                       {'deadman_timeout_s': 31}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                MakerSettings(**kwargs)

    def test_live_requires_explicit_calibration_and_sandbox_acceptance(self):
        for kwargs in ({}, {'guards_calibrated': True}, {'sandbox_verified': True}):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, '标定|沙盒'):
                MakerSettings(mode='live', **kwargs)
        live = MakerSettings(mode='live', guards_calibrated=True, sandbox_verified=True,
                             healthcheck_url='https://health.example/check')
        self.assertEqual(live.mode, 'live')

    def test_live_requires_external_heartbeat(self):
        with self.assertRaisesRegex(ValueError, '心跳'):
            MakerSettings(mode='live', guards_calibrated=True, sandbox_verified=True)

    def test_sandbox_addresses_do_not_fall_back_to_production(self):
        settings = MakerSettings(mode='sandbox')
        self.assertIn('ondoperps-sandbox.xyz', settings.rest_url)
        self.assertIn('ondoperps-sandbox.xyz', settings.ws_url)
        self.assertEqual(MakerSettings().rest_url, 'https://api.ondoperps.xyz')
        with self.assertRaisesRegex(ValueError, '沙盒'):
            MakerSettings(mode='sandbox', sandbox_rest_url='https://api.ondoperps.xyz')

    def test_modes_urls_and_account_paths_are_checked(self):
        for kwargs in ({'mode': 'paper'}, {'account': '../victim'}, {'market': 'IBM,ETH'},
                       {'prod_rest_url': 'http://example.com'}, {'sandbox_ws_url': 'ws://example'},
                       {'healthcheck_url': 'file:///tmp/check'}, {'timezone': 'No/SuchZone'},
                       {'window_start_time': '9:00'}, {'window_end_day': 7},
                       {'window_end_time': '25:01'}, {'window_start_day': 0,
                        'window_start_time': '05:00'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                MakerSettings(**kwargs)
        settings = MakerSettings(account='main-1', data_dir='/tmp/xyz')
        self.assertEqual(settings.instance_lock_path, Path('/tmp/xyz/main-1.lock'))
        self.assertEqual(settings.halt_path, Path('/tmp/xyz/main-1.halt'))

    def test_deadman_match_rule_is_nonempty_mapping_of_scalar_expectations(self):
        for value in ({}, [], {'channel': None}, {'args': {'nested': True}}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                MakerSettings(deadman_ack_match=value)
        settings = MakerSettings(deadman_ack_match={'op': 'subscribed', 'ok': True})
        self.assertEqual(settings.deadman_ack_match['ok'], True)
        with self.assertRaises(TypeError):
            settings.deadman_ack_match['ok'] = False

    def test_toml_fractional_amount_cannot_round_down_past_code_cap(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'maker.toml'
            path.write_text('[maker]\nquote_notional = 800.00000000000000001\n')
            with self.assertRaisesRegex(ValueError, '硬上限'):
                MakerSettings.from_file(path)

    def test_sandbox_cannot_use_production_hostname_with_trailing_dot(self):
        with self.assertRaisesRegex(ValueError, '沙盒'):
            MakerSettings(mode='sandbox', sandbox_rest_url='https://api.ondoperps.xyz.')

    def test_data_directory_cannot_be_missing(self):
        with self.assertRaises(ValueError):
            MakerSettings(data_dir=None)

    def test_emergency_load_keeps_live_environment_without_startup_requirements(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'maker.toml'
            path.write_text('[maker]\nmode="live"\n')
            with self.assertRaisesRegex(ValueError, '标定|沙盒'):
                MakerSettings.from_file(path)
            settings = MakerSettings.from_file(path, for_emergency=True)
            self.assertEqual(settings.mode, 'live')
            self.assertEqual(settings.rest_url, 'https://api.ondoperps.xyz')
            self.assertFalse(settings.guards_calibrated)
            path.write_text('[maker]\nmode="live"\nquote_notional=801\n')
            with self.assertRaisesRegex(ValueError, '硬上限'):
                MakerSettings.from_file(path, for_emergency=True)

    def test_config_cannot_set_internal_emergency_loading_switch(self):
        with self.assertRaisesRegex(ValueError, '未知'):
            MakerSettings.from_dict({'_for_emergency': True, 'mode': 'live'})

    def test_boolean_flags_cannot_be_strings(self):
        with self.assertRaises(ValueError):
            MakerSettings(guards_calibrated='true')


if __name__ == '__main__':
    unittest.main()
