"""Batch D regressions for receive times in collected data, legacy database migration, and precision sources."""

import json
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from pendle.bt_config import BacktestSettings
from pendle.collected_import import import_collected
from pendle.history import HistoryStore
from pendle.raw_import import import_raw_history, load_symbol_settings


def ondo_trade():
    return {'id': 'ondo-d', 'market': 'WEN-USD.P',
            'time': '1970-01-01T00:00:01Z', 'price': '7', 'size': '2',
            'cost': '14', 'aggressor_side': 'sell'}


def binance_trade():
    return {'symbol': 'WENUSDT', 'agg_id': 1, 'price': '7', 'quantity': '2',
            'first_trade_id': 1, 'last_trade_id': 1, 'transact_time': 1000,
            'is_buyer_maker': False}


def markets_payload():
    return {'success': True, 'result': {'perps': {'tradingPairs': [
        {'market': 'WEN-USD.P', 'quoteIncrement': '0.0001', 'baseIncrement': '0.01'}]}}}


class ReceivedTimeImportTests(unittest.TestCase):
    def write_collected(self, path, rows):
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')

    def record(self, kind, payload, received_ms):
        return {'source': 'ondo' if kind != 'binance_trade' else 'binance',
                'kind': kind, 'market': None, 'received_ms': received_ms, 'payload': payload}

    def legacy_database(self, path):
        with sqlite3.connect(path) as connection:
            connection.executescript('''
                CREATE TABLE ondo_trades (
                    id TEXT PRIMARY KEY, market TEXT NOT NULL, time_ns INTEGER NOT NULL,
                    price TEXT NOT NULL, size TEXT NOT NULL, cost TEXT NOT NULL,
                    aggressor_side TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE binance_trades (
                    symbol TEXT NOT NULL, agg_id INTEGER NOT NULL, price TEXT NOT NULL,
                    quantity TEXT NOT NULL, first_trade_id INTEGER NOT NULL,
                    last_trade_id INTEGER NOT NULL, transact_time INTEGER NOT NULL,
                    is_buyer_maker INTEGER NOT NULL, PRIMARY KEY(symbol,agg_id));
            ''')
            connection.execute('INSERT INTO ondo_trades VALUES (?,?,?,?,?,?,?,?)',
                ('ondo-d', 'WEN-USD.P', 1000000000, '7', '2', '14', 'sell',
                 json.dumps(ondo_trade())))
            connection.execute('INSERT INTO binance_trades VALUES (?,?,?,?,?,?,?,?)',
                ('WENUSDT', 1, '7', '2', 1, 1, 1000, 0))

    def test_collected_outer_received_time_is_saved_for_both_trade_sources(self):
        with tempfile.TemporaryDirectory() as directory, HistoryStore(':memory:') as store:
            path = Path(directory) / 'received.jsonl'
            self.write_collected(path, [
                self.record('ondo_trade', {**ondo_trade(), 'received_ms': 2}, 11000),
                self.record('binance_trade', {**binance_trade(), 'received_ms': 3}, 12000)])
            import_collected(path, store)
            self.assertEqual(next(store.iter_ondo_trades('WEN-USD.P')).get('received_ms'), 11000)
            self.assertEqual(next(store.iter_binance_trades('WENUSDT')).get('received_ms'), 12000)

    def test_duplicate_trades_keep_earliest_known_receipt_and_fill_historical_null(self):
        with HistoryStore(':memory:') as store:
            for writer, reader, trade in (
                (store.upsert_ondo_trades, lambda: next(store.iter_ondo_trades('WEN-USD.P')), ondo_trade()),
                (store.upsert_binance_trades, lambda: next(store.iter_binance_trades('WENUSDT')), binance_trade())):
                with self.subTest(symbol=trade.get('market', trade.get('symbol'))):
                    self.assertEqual(writer([trade]), 1)
                    self.assertEqual(writer([{**trade, 'received_ms': 12000}]), 1)
                    self.assertEqual(writer([{**trade, 'received_ms': 11000}]), 1)
                    self.assertEqual(writer([{**trade, 'received_ms': 13000}, trade]), 0)
                    self.assertEqual(reader().get('received_ms'), 11000)

    def test_read_only_legacy_database_exposes_nullable_receipt_without_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'old.sqlite'
            self.legacy_database(path)
            with HistoryStore(path, create=False) as store:
                for row in (next(store.iter_ondo_trades('WEN-USD.P')),
                            next(store.iter_binance_trades('WENUSDT'))):
                    self.assertIn('received_ms', row)
                    self.assertIsNone(row['received_ms'])
            with sqlite3.connect(path) as connection:
                columns = [row[1] for row in connection.execute('PRAGMA table_info(ondo_trades)')]
            self.assertNotIn('received_ms', columns)

    def test_writable_legacy_database_migrates_and_retains_trades(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'old.sqlite'
            self.legacy_database(path)
            with HistoryStore(path) as store:
                for table in ('ondo_trades', 'binance_trades'):
                    columns = {row['name']: row for row in store.connection.execute(f'PRAGMA table_info({table})')}
                    self.assertIn('received_ms', columns)
                    self.assertEqual(columns['received_ms']['notnull'], 0)
                store.upsert_ondo_trades([{**ondo_trade(), 'received_ms': 11000}])
                store.upsert_binance_trades([{**binance_trade(), 'received_ms': 12000}])
            with HistoryStore(path, create=False) as store:
                self.assertEqual(next(store.iter_ondo_trades('WEN-USD.P'))['received_ms'], 11000)
                self.assertEqual(next(store.iter_binance_trades('WENUSDT'))['received_ms'], 12000)

    def test_invalid_receipt_rolls_back_entire_trade_batch(self):
        for writer_name, trade in (('upsert_ondo_trades', ondo_trade()),
                                   ('upsert_binance_trades', binance_trade())):
            for invalid in (-1, True, '11000'):
                with self.subTest(writer=writer_name, invalid=invalid), HistoryStore(':memory:') as store:
                    with self.assertRaisesRegex(ValueError, 'received_ms'):
                        getattr(store, writer_name)([trade, {**trade, 'received_ms': invalid}])
                    self.assertEqual(list(store.iter_ondo_trades('WEN-USD.P')), [])
                    self.assertEqual(list(store.iter_binance_trades('WENUSDT')), [])


class SharedPrecisionImportTests(unittest.TestCase):
    def test_collected_and_raw_markets_use_same_storage_and_loader(self):
        payload = markets_payload()
        with tempfile.TemporaryDirectory() as directory, HistoryStore(':memory:') as store:
            root = Path(directory)
            source = root / 'markets.jsonl'
            source.write_text(json.dumps({'source': 'ondo', 'kind': 'markets', 'market': None,
                'received_ms': 2000, 'payload': payload}) + '\n', encoding='utf-8')
            counts = import_collected(source, store)
            again = import_collected(source, store)
            settings = load_symbol_settings(store, BacktestSettings(), 'WEN')
            self.assertEqual((settings.price_tick, settings.quantity_step),
                             (Decimal('.0001'), Decimal('.01')))
            self.assertEqual(counts['contracts'], 1)
            self.assertEqual(again['contracts'], 0)
            self.assertEqual(next(store.iter_contracts('WEN-USD.P'))['source'], 'ondo_markets')
            (root / 'ondo_markets_20261104.json').write_text(json.dumps(payload), encoding='utf-8')
            import_raw_history(root, [], 0, 1, store, BacktestSettings())
            self.assertEqual({row['source'] for row in store.iter_contracts()}, {'ondo_markets'})

    def test_missing_precision_rejects_symbol_in_chinese(self):
        with HistoryStore(':memory:') as store:
            with self.assertRaisesRegex(ValueError, 'WEN.*精度'):
                load_symbol_settings(store, BacktestSettings(), 'WEN')

    def test_both_explicit_precision_overrides_allow_symbol_without_markets(self):
        settings = BacktestSettings(symbol_overrides={'WEN': {
            'price_tick': '.0005', 'quantity_step': '.02'}})
        with HistoryStore(':memory:') as store:
            result = load_symbol_settings(store, settings, 'WEN')
        self.assertEqual((result.price_tick, result.quantity_step), (Decimal('.0005'), Decimal('.02')))

    def test_partial_override_does_not_supply_other_missing_precision(self):
        for override in ({'price_tick': '.0005'}, {'quantity_step': '.02'},
                         {'binance_symbol': 'WENALTUSDT'}):
            with self.subTest(override=override), HistoryStore(':memory:') as store:
                with self.assertRaisesRegex(ValueError, 'WEN.*精度'):
                    load_symbol_settings(store, BacktestSettings(symbol_overrides={'WEN': override}), 'WEN')

    def test_invalid_collected_markets_is_atomic(self):
        payload = markets_payload()
        payload['result']['perps']['tradingPairs'].append(
            {'market': 'IBM-USD.P', 'quoteIncrement': '.01', 'baseIncrement': '0'})
        with tempfile.TemporaryDirectory() as directory, HistoryStore(':memory:') as store:
            path = Path(directory) / 'invalid.jsonl'
            path.write_text(json.dumps({'source': 'ondo', 'kind': 'markets', 'received_ms': 2000,
                                        'payload': payload}) + '\n', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, '精度'):
                import_collected(path, store)
            self.assertEqual(list(store.iter_contracts()), [])
