import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from pendle.config import Settings


class ConfigTests(unittest.TestCase):
    def test_defaults_and_round_trip(self):
        settings = Settings()
        self.assertEqual(settings.symbol, 'INTC')
        self.assertEqual(settings.quote_notional, Decimal('100'))
        self.assertEqual(Settings.from_dict(settings.to_dict()), settings)
        self.assertIsNone(settings.reward_usdc)

    def test_toml_reads_decimal_without_float_rounding(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.toml'
            path.write_text('quote_notional = "100.123456789123456789"\nreward_usdc = "5"\n')
            settings = Settings.from_file(path)
        self.assertEqual(str(settings.quote_notional), '100.123456789123456789')
        self.assertEqual(settings.reward_usdc, Decimal('5'))

    def test_toml_accepts_fractional_poll_interval(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.toml'
            path.write_text('poll_interval_s = 0.5\nhttp_timeout_s = 1.0\n')
            self.assertEqual(Settings.from_file(path).poll_interval_s, 0.5)

    def test_invalid_and_unknown_settings_are_rejected(self):
        for change in ({'oops': 1}, {'quote_notional': 'NaN'},
                       {'quote_notional': '-1'}, {'poll_interval_s': 0},
                       {'hedge_latency_ms': -1}, {'depth': 101},
                       {'symbol': 'BTC', 'ondo_market': 'INTC-USD.P'},
                       {'reward_usdc': '-1'}, {'http_timeout_s': float('inf')},
                       {'depth': True}, {'max_stale_ms': 0}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                Settings.from_dict(change)

    def test_replay_cannot_change_identity_or_recording_settings(self):
        original = Settings()
        self.assertEqual(original.for_replay(Settings(quote_offset_bps=Decimal('12'))).quote_offset_bps,
                         Decimal('12'))
        for change in ({'price_tick': '0.1'}, {'symbol': 'BTC', 'ondo_market': 'BTC-USD.P'},
                       {'poll_interval_s': 3}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                original.for_replay(Settings.from_dict(change))

    def test_partial_replay_override_preserves_recorded_assumptions(self):
        original = Settings(quote_notional='50', ondo_maker_fee_bps='4', poll_interval_s=3)
        changed = original.for_replay({'reward_usdc': '5'})
        self.assertEqual(changed.quote_notional, Decimal('50'))
        self.assertEqual(changed.ondo_maker_fee_bps, Decimal('4'))
        self.assertEqual(changed.poll_interval_s, 3)
        self.assertEqual(changed.reward_usdc, Decimal('5'))
