"""Offline dispatch and configuration behavior of the persistent collector CLI."""

import contextlib
import io
import json
import sys
import tempfile
import types
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from pendle.bt_config import CollectSettings
from pendle.cli import main


ROOT = Path(__file__).parents[1]


class CollectConfigTests(unittest.TestCase):
    def test_focus_markets_alias_copies_symbols_and_preserves_precision(self):
        markets = ['INTC', 'WEN']
        settings = CollectSettings.from_dict({'focus_markets': markets,
            'focus_snapshot_s': '0.25',
            'symbol_overrides': {'WEN': {'binance_symbol': 'WENUSDC'}}})
        markets.append('IBM')
        self.assertEqual(settings.focus_symbols, ('INTC', 'WEN'))
        self.assertEqual(settings.focus_markets, settings.focus_symbols)
        self.assertEqual(settings.focus_snapshot_s, Decimal('0.25'))
        self.assertEqual(settings.for_symbol('WEN').binance_symbol, 'WENUSDC')
        self.assertEqual(CollectSettings.from_dict(settings.to_dict()), settings)

    def test_conflicting_aliases_and_invalid_markets_are_rejected(self):
        for data in ({'focus_markets': ['INTC'], 'focus_symbols': ['WEN']},
                     {'focus_markets': ['intc']}, {'focus_markets': []},
                     {'focus_markets': ['INTC', 'INTC']}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                CollectSettings.from_dict(data)
        settings = CollectSettings.from_dict({'focus_markets': ['INTC'],
                                              'focus_symbols': ['INTC']})
        self.assertEqual(settings.focus_symbols, ('INTC',))

    def test_example_has_eight_focus_markets_and_runtime_defaults(self):
        settings = CollectSettings.from_file(ROOT / 'collect.example.toml')
        self.assertEqual(settings.focus_symbols,
            ('INTC', 'DRAM', 'CRCL', 'WEN', 'NKE', 'RDDT', 'IBM', 'DKNG'))
        self.assertEqual(settings.data_dir, 'data/collect')
        self.assertEqual(settings.ondo_ping_s, Decimal('30'))
        self.assertEqual(CollectSettings().ondo_ping_s, Decimal('30'))


class CollectCliTests(unittest.TestCase):
    def invoke(self, arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(arguments)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_collect_loads_config_and_preserves_disk_limit_exit_code(self):
        received = []
        module = types.ModuleType('pendle.collector')

        def run_collect(settings):
            received.append(settings)
            return 75

        module.run_collect = run_collect
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'collect.toml'
            config.write_text('focus_markets = ["WEN"]\nmax_disk_gb = "2.5"\n',
                              encoding='utf-8')
            with patch.dict(sys.modules, {'pendle.collector': module}):
                code, stdout, stderr = self.invoke(['collect', '--config', str(config)])
        self.assertEqual(code, 75)
        self.assertEqual(stderr, '')
        self.assertEqual(stdout, '')
        self.assertEqual(received[0].focus_symbols, ('WEN',))
        self.assertEqual(received[0].max_disk_gb, Decimal('2.5'))

    def test_collect_bad_config_returns_chinese_error(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'collect.toml'
            config.write_text('max_disk_gb = "NaN"\n', encoding='utf-8')
            code, stdout, stderr = self.invoke(['collect', '--config', str(config)])
        self.assertEqual(code, 2)
        self.assertEqual(stdout, '')
        self.assertIn('有限数字', stderr)
        self.assertNotIn('Traceback', stderr)

    def test_import_collected_opens_store_and_prints_result(self):
        received = []
        module = types.ModuleType('pendle.collected_import')

        def import_collected(src, store):
            received.append((src, store.connection.execute('SELECT 1').fetchone()[0]))
            return {'files': 2, 'ondo_trades': 3, 'truncated': 1}

        module.import_collected = import_collected
        with tempfile.TemporaryDirectory() as directory:
            source, db = Path(directory) / 'collect', Path(directory) / 'history.sqlite'
            source.mkdir()
            with patch.dict(sys.modules, {'pendle.collected_import': module}):
                code, stdout, stderr = self.invoke(['import-collected', '--src', str(source),
                                                     '--db', str(db)])
            self.assertTrue(db.is_file())
        self.assertEqual(code, 0)
        self.assertEqual(stderr, '')
        self.assertEqual(json.loads(stdout), {'files': 2, 'ondo_trades': 3, 'truncated': 1})
        self.assertEqual(received, [(source, 1)])

    def test_help_lists_live_commands_without_loading_collector(self):
        with patch.dict(sys.modules, {'pendle.collector': None}), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            with self.assertRaises(SystemExit) as result:
                main(['--help'])
        self.assertEqual(result.exception.code, 0)
        self.assertIn('collect', stdout.getvalue())
        self.assertIn('import-collected', stdout.getvalue())
