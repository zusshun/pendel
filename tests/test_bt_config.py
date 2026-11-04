import tempfile
import unittest
from dataclasses import FrozenInstanceError, fields
from decimal import Decimal
from pathlib import Path

from pendle.bt_config import BacktestSettings, CollectSettings


class BacktestConfigTests(unittest.TestCase):
    def test_b3_defaults_and_example(self):
        settings = BacktestSettings()
        expected = {'max_depth_age_s': Decimal('120'),
                    'markout_max_delay_s': Decimal('1800'),
                    'pickoff_enabled': True, 'pickoff_edge_bps': Decimal('2.5'),
                    'reward_mid_error_scenarios': tuple(map(Decimal, ('0', '10', '20')))}
        example = BacktestSettings.from_file(Path(__file__).parent.parent / 'backtest.example.toml')
        for name, value in expected.items():
            with self.subTest(name=name):
                self.assertEqual(getattr(settings, name, None), value)
                self.assertEqual(getattr(example, name, None), value)

    def test_b3_values_are_decimal_immutable_and_round_trip(self):
        scenarios = ['0', '12.3456789123456789', '20']
        settings = BacktestSettings.from_dict({
            'max_depth_age_s': '30.125', 'markout_max_delay_s': 0,
            'pickoff_enabled': False, 'pickoff_edge_bps': '3.125',
            'reward_mid_error_scenarios': scenarios})
        scenarios.append('30')
        self.assertEqual(settings.max_depth_age_s, Decimal('30.125'))
        self.assertEqual(settings.markout_max_delay_s, Decimal('0'))
        self.assertFalse(settings.pickoff_enabled)
        self.assertEqual(settings.pickoff_edge_bps, Decimal('3.125'))
        self.assertEqual(settings.reward_mid_error_scenarios,
                         tuple(map(Decimal, ('0', '12.3456789123456789', '20'))))
        self.assertEqual(BacktestSettings.from_dict(settings.to_dict()), settings)

    def test_b3_rejects_invalid_pickoff_and_reward_scenarios(self):
        for value in ({'pickoff_enabled': 1}, {'pickoff_enabled': 'true'},
                      {'pickoff_edge_bps': 10000}, {'reward_mid_error_bps': 10000},
                      {'reward_mid_error_scenarios': []},
                      {'reward_mid_error_scenarios': '0,10,20'},
                      {'reward_mid_error_scenarios': ['-1']},
                      {'reward_mid_error_scenarios': ['NaN']},
                      {'reward_mid_error_scenarios': ['Infinity']},
                      {'reward_mid_error_scenarios': [True]},
                      {'reward_mid_error_scenarios': ['10000']}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                BacktestSettings.from_dict(value)

    def test_b2_basis_and_competition_defaults(self):
        settings = BacktestSettings()
        self.assertEqual(getattr(settings, 'basis_pair_max_age_s', None), Decimal('30'))
        self.assertEqual(getattr(settings, 'basis_samples', None), 20)
        self.assertEqual(getattr(settings, 'max_basis_age_s', None), Decimal('21600'))
        self.assertEqual(settings.max_ref_age_s, Decimal('120'))
        self.assertEqual(settings.comp_scenarios, tuple(map(Decimal,
            ('1000', '3000', '10000', '30000', '100000', '300000', '1000000', '4000000'))))
        example = BacktestSettings.from_file(Path(__file__).parent.parent / 'backtest.example.toml')
        for name in ('basis_pair_max_age_s', 'basis_samples', 'max_basis_age_s',
                     'max_ref_age_s', 'comp_scenarios'):
            self.assertEqual(getattr(example, name), getattr(settings, name))

    def test_b2_basis_limits_must_be_positive(self):
        for name in ('basis_pair_max_age_s', 'basis_samples', 'max_basis_age_s'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                BacktestSettings.from_dict({name: 0})

    def test_mapping_deviation_default_and_override(self):
        self.assertEqual(getattr(BacktestSettings(), 'max_mapping_dev_bps', None), Decimal('500'))
        self.assertEqual(BacktestSettings.from_dict({'max_mapping_dev_bps': '300'}).max_mapping_dev_bps,
                         Decimal('300'))

    def test_defaults_and_round_trip(self):
        settings = BacktestSettings()
        self.assertEqual(settings.symbols, ('INTC', 'DRAM', 'CRCL'))
        for name, expected in {'quote_notional': '800', 'max_inventory_usd': '1600',
                               'band_bps': '25', 'pool_usdc_per_week': '110000',
                               'ondo_maker_fee_bps': '1',
                               'ondo_taker_fee_bps': '2.5'}.items():
            self.assertEqual(getattr(settings, name), Decimal(expected))
        self.assertEqual(settings.at_price_fill_ratios,
                         (Decimal('0'), Decimal('0.5'), Decimal('1')))
        self.assertEqual(BacktestSettings.from_dict(settings.to_dict()), settings)

    def test_settings_are_frozen(self):
        for settings in (BacktestSettings(), CollectSettings()):
            with self.subTest(settings=type(settings).__name__):
                with self.assertRaises(FrozenInstanceError):
                    settings.band_bps = Decimal('20')

    def test_toml_keeps_decimal_precision(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'backtest.toml'
            path.write_text('quote_notional = 800.123456789123456789\n'
                            'at_price_fill_ratios = [0, 0.25, 1]\n', encoding='utf-8')
            settings = BacktestSettings.from_file(path)
        self.assertEqual(settings.quote_notional, Decimal('800.123456789123456789'))
        self.assertEqual(settings.at_price_fill_ratios,
                         (Decimal(0), Decimal('0.25'), Decimal(1)))

    def test_decimal_fields_reject_nonfinite_negative_and_boolean(self):
        for model in (BacktestSettings, CollectSettings):
            for setting in fields(model):
                if isinstance(setting.default, Decimal):
                    for value in ('NaN', 'Infinity', '-1', True):
                        with self.subTest(model=model.__name__, name=setting.name, value=value):
                            with self.assertRaises(ValueError):
                                model.from_dict({setting.name: value})

    def test_integer_fields_reject_noninteger_negative_nonfinite_and_boolean(self):
        for model in (BacktestSettings, CollectSettings):
            for setting in fields(model):
                if type(setting.default) is int:
                    for value in (-1, 1.5, float('nan'), float('inf'), True):
                        with self.subTest(model=model.__name__, name=setting.name, value=value):
                            with self.assertRaises(ValueError):
                                model.from_dict({setting.name: value})

    def test_unknown_keys_and_nonmapping_are_rejected(self):
        for model in (BacktestSettings, CollectSettings):
            for value in ({'unknown': 1}, {1: 1}, {'unknown': 1, 2: 2}, [], None):
                with self.subTest(model=model.__name__, value=value), self.assertRaises(ValueError):
                    model.from_dict(value)

    def test_symbols_are_validated_and_copied(self):
        symbols = ['INTC', 'CRCL']
        settings = BacktestSettings(symbols=symbols)
        symbols.append('DRAM')
        self.assertEqual(settings.symbols, ('INTC', 'CRCL'))
        for symbols in ([], ['INTC', 'INTC'], ['intc'], 'INTC', [True]):
            with self.subTest(symbols=symbols), self.assertRaises(ValueError):
                BacktestSettings(symbols=symbols)

    def test_symbol_overrides_use_decimal_and_default_binance_mapping(self):
        settings = BacktestSettings.from_dict({'symbol_overrides': {
            'INTC': {'price_tick': '0.001', 'quantity_step': '0.01',
                     'binance_symbol': 'INTCUSDC'}, 'CRCL': {'price_tick': '0.1'}}})
        intc = settings.for_symbol('INTC')
        self.assertEqual((intc.symbol, intc.ondo_market, intc.binance_symbol),
                         ('INTC', 'INTC-USD.P', 'INTCUSDC'))
        self.assertEqual((intc.price_tick, intc.quantity_step),
                         (Decimal('0.001'), Decimal('0.01')))
        self.assertEqual(settings.for_symbol('DRAM').binance_symbol, 'DRAMUSDT')
        self.assertEqual(settings.for_symbol('CRCL').quantity_step, settings.quantity_step)
        self.assertEqual(BacktestSettings.from_dict(settings.to_dict()), settings)
        with self.assertRaises(FrozenInstanceError):
            intc.price_tick = Decimal('1')

    def test_symbol_overrides_are_deeply_immutable(self):
        overrides = {'INTC': {'price_tick': '0.01'}}
        settings = BacktestSettings(symbol_overrides=overrides)
        overrides['INTC']['price_tick'] = '999'
        self.assertEqual(settings.for_symbol('INTC').price_tick, Decimal('0.01'))
        with self.assertRaises(TypeError):
            settings.symbol_overrides['INTC']['price_tick'] = '9'
        with self.assertRaises(TypeError):
            settings.symbol_overrides['DRAM'] = {}

    def test_invalid_symbol_overrides_are_rejected(self):
        for value in ({'intc': {}}, {'INTC': {'unknown': '1'}},
                      {'INTC': {'unknown': 1, 2: 2}},
                      {'INTC': {'price_tick': 0}}, {'INTC': {'quantity_step': '-1'}},
                      {'INTC': {'price_tick': 'NaN'}}, {'INTC': {'binance_symbol': 'intcusdt'}},
                      {'INTC': []}, []):
            with self.subTest(value=value), self.assertRaises(ValueError):
                BacktestSettings(symbol_overrides=value)
        with self.assertRaises(ValueError):
            BacktestSettings().for_symbol('bad')

    def test_invalid_ratios_scenarios_modes_and_boolean_flags_are_rejected(self):
        for value in ({'at_price_fill_ratio': '1.01'}, {'at_price_fill_ratios': []},
                      {'at_price_fill_ratios': ['-1']}, {'at_price_fill_ratios': ['NaN']},
                      {'comp_scenarios': ['-1']}, {'comp_scenarios': []},
                      {'reward_two_sided': 1}, {'reward_markets': 0},
                      {'quote_center': 'index'}, {'requote_mode': 'place_first'},
                      {'hedge_mode': 'real'}, {'reward_weight': 'other'},
                      {'comp_source': 'other'}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                BacktestSettings.from_dict(value)

    def test_required_positive_fields_and_bps_ranges_are_validated(self):
        for value in ({'quote_notional': 0}, {'price_tick': 0}, {'quantity_step': 0},
                      {'band_bps': 0}, {'band_bps': 10000}, {'quote_offset_bps': 10000},
                      {'max_inventory_usd': 0}, {'basis_window_s': 0}, {'max_ref_age_s': 0},
                      {'http_timeout_s': 0}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                BacktestSettings.from_dict(value)

    def test_named_periods_default_and_validation(self):
        settings = BacktestSettings()
        self.assertEqual(dict(settings.named_periods['周末']),
                         {'start_weekday': 5, 'start_hour': 9,
                          'end_weekday': 0, 'end_hour': 5})
        for value in ({'周末': {'start_weekday': 7}}, {'周末': {'unknown': 1}},
                      {'': {'start_weekday': 5}}, [], {'周末': {'start_hour': True}}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                BacktestSettings(named_periods=value)

    def test_example_file_is_valid_and_matches_promised_defaults(self):
        example = BacktestSettings.from_file(Path(__file__).parent.parent / 'backtest.example.toml')
        self.assertEqual(example.quote_notional, Decimal('800'))
        self.assertEqual(example.max_inventory_usd, Decimal('1600'))
        self.assertEqual(example.band_bps, Decimal('25'))
        self.assertEqual(example.pool_usdc_per_week, Decimal('110000'))
        self.assertEqual(example.ondo_maker_fee_bps, Decimal('1'))
        self.assertEqual(example.ondo_taker_fee_bps, Decimal('2.5'))


class CollectConfigTests(unittest.TestCase):
    def test_defaults_and_round_trip(self):
        settings = CollectSettings()
        self.assertEqual(settings.focus_symbols, ('INTC', 'DRAM', 'CRCL'))
        self.assertEqual(settings.focus_snapshot_s, Decimal('1'))
        self.assertEqual(settings.contracts_interval_s, Decimal('300'))
        self.assertEqual(settings.binance_reconnect_s, Decimal('82800'))
        self.assertEqual(settings.market_stale_s, Decimal('600'))
        self.assertEqual(settings.source_silence_warn_s, Decimal('180'))
        self.assertEqual(settings.binance_ws_url, 'wss://fstream.binance.com/market/stream?streams=')
        self.assertEqual(settings.ondo_ws_url, 'wss://api.ondoperps.xyz/ws')
        self.assertEqual(settings.for_symbol('DRAM').binance_symbol, 'DRAMUSDT')
        self.assertEqual(CollectSettings.from_dict(settings.to_dict()), settings)

    def test_collect_toml_and_symbol_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'collect.toml'
            path.write_text('focus_symbols = ["CRCL"]\nfocus_snapshot_s = 0.25\n'
                            '[symbol_overrides.CRCL]\nprice_tick = "0.001"\n', encoding='utf-8')
            settings = CollectSettings.from_file(path)
        self.assertEqual(settings.focus_symbols, ('CRCL',))
        self.assertEqual(settings.focus_snapshot_s, Decimal('0.25'))
        self.assertEqual(settings.for_symbol('CRCL').price_tick, Decimal('0.001'))

    def test_collect_requires_valid_paths_positive_limits_and_focus_symbols(self):
        for value in ({'data_dir': ''}, {'log_dir': 5}, {'max_disk_gb': 0},
                      {'market_stale_s': 0}, {'source_silence_warn_s': 0},
                      {'binance_ws_url': 'https://example.test'}, {'ondo_ws_url': ''},
                      {'focus_snapshot_s': 0}, {'max_http_workers': 0},
                      {'write_queue_size': 0}, {'depth': 101}, {'max_tick_depth': 11},
                      {'focus_symbols': ['intc']}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                CollectSettings.from_dict(value)
