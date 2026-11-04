import gzip
import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from decimal import Decimal
from importlib import import_module
from pathlib import Path

from pendle.bt_config import BacktestSettings
from pendle.history import HistoryStore, parse_time_ns


START = parse_time_ns('2026-10-31T00:30:00Z') // 1000000
END = parse_time_ns('2026-11-01T21:10:00Z') // 1000000


def ondo_trade(identity, timestamp, market='WEN-USD.P'):
    return {'id': identity, 'market': market, 'time': timestamp, 'price': '7.12',
            'size': '0.1', 'cost': '0.712', 'aggressor_side': 'buy'}


class RawImportTests(unittest.TestCase):
    def api(self):
        try:
            return import_module('pendle.raw_import')
        except ModuleNotFoundError:
            self.fail('缺少离线原始行情导入模块')

    def write_raw(self, root, symbol='WEN', *, bad_checksum=False):
        root = Path(root)
        (root / 'ondo_trades').mkdir(exist_ok=True)
        (root / 'binance').mkdir(exist_ok=True)
        trades = [ondo_trade('new', '2026-11-02T00:00:00.123456789Z'),
                  ondo_trade('inside', '2026-10-31T00:30:01.123456789Z'),
                  ondo_trade('old', '2026-10-30T23:00:00.123456789Z')]
        for row in trades:
            row['market'] = symbol + '-USD.P'
        filename = root / 'ondo_trades' / f'{symbol}_until_20261030.jsonl.gz'
        with gzip.open(filename, 'wt') as stream:
            for rows, cursor in ((trades[:2], None), (trades[1:], 'cursor-1')):
                json.dump({'received_ms': END + 1000, 'cursor': cursor,
                           'payload': {'success': True, 'result': rows,
                                       'pageInfo': {'nextCursor': 'unused'}}}, stream)
                stream.write('\n')
        markets = {'success': True, 'result': {'perps': {'tradingPairs': [
            {'market': symbol + '-USD.P', 'quoteIncrement': '0.0001',
             'baseIncrement': '0.1'},
            {'market': 'IBM-USD.P', 'quoteIncrement': '0.01', 'baseIncrement': '0.01'}]}}}
        (root / 'ondo_markets_20261104.json').write_text(json.dumps(markets))
        body = io.BytesIO()
        with zipfile.ZipFile(body, 'w') as archive:
            archive.writestr('trades.csv', 'agg_trade_id,price,quantity,first_trade_id,'
                             'last_trade_id,transact_time,is_buyer_maker\n'
                             f'1,7.12,1,1,1,{START - 1},false\n'
                             f'2,7.13,2,2,2,{START + 1},true\n')
        path = root / 'binance' / f'{symbol}USDT-aggTrades-2026-10-31.zip'
        path.write_bytes(body.getvalue())
        digest = '0' * 64 if bad_checksum else hashlib.sha256(body.getvalue()).hexdigest()
        Path(str(path) + '.CHECKSUM').write_text(digest + '  ' + path.name + '\n')
        return filename, path

    def test_offline_import_is_idempotent_and_retains_warmup_and_nanoseconds(self):
        with tempfile.TemporaryDirectory() as directory, HistoryStore(':memory:') as store:
            self.write_raw(directory)
            importer = self.api().import_raw_history
            first = importer(directory, ['WEN'], START, END, store, BacktestSettings())
            second = importer(directory, ['WEN'], START, END, store, BacktestSettings())
            rows = list(store.iter_ondo_trades('WEN-USD.P'))
            binance = list(store.iter_binance_trades('WENUSDT'))
            records = list(store.iter_fetches('ondo'))
        self.assertEqual(first['ondo_trades'], 3)
        self.assertEqual(first['binance_trades'], 2)
        self.assertEqual(first['contracts'], 2)
        self.assertEqual(first['errors'], [])
        self.assertEqual(second['ondo_trades'], 0)
        self.assertEqual(second['binance_trades'], 0)
        self.assertEqual(second['contracts'], 0)
        self.assertEqual([row['id'] for row in rows], ['old', 'inside', 'new'])
        self.assertEqual(rows[1]['time_ns'] % 1000000000, 123456789)
        self.assertLess(binance[0]['transact_time'], START)
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0]['complete'])
        self.assertEqual(records[0]['params']['raw_file'], 'WEN_until_20261030.jsonl.gz')

    def test_checksum_mismatch_rejects_day_but_imports_other_symbol(self):
        with tempfile.TemporaryDirectory() as directory, HistoryStore(':memory:') as store:
            self.write_raw(directory, 'WEN', bad_checksum=True)
            self.write_raw(directory, 'INTC')
            result = self.api().import_raw_history(directory, ['WEN', 'INTC'], START, END,
                                                   store, BacktestSettings())
            self.assertEqual(list(store.iter_binance_trades('WENUSDT')), [])
            self.assertEqual(len(list(store.iter_binance_trades('INTCUSDT'))), 2)
            failures = [row for row in store.iter_fetches('binance') if row['status'] == 'error']
        self.assertEqual(len(failures), 1)
        self.assertIn('SHA256', failures[0]['error'])
        self.assertTrue(any('WEN' in error for error in result['errors']))

    def test_missing_checksum_rejects_day_with_auditable_error(self):
        with tempfile.TemporaryDirectory() as directory, HistoryStore(':memory:') as store:
            _, path = self.write_raw(directory)
            Path(str(path) + '.CHECKSUM').unlink()
            result = self.api().import_raw_history(directory, ['WEN'], START, END,
                                                   store, BacktestSettings())
            self.assertEqual(list(store.iter_binance_trades('WENUSDT')), [])
            failures = [row for row in store.iter_fetches('binance') if row['status'] == 'error']
        self.assertEqual(len(failures), 1)
        self.assertIn('CHECKSUM', result['errors'][0])

    def test_market_snapshot_precision_and_explicit_overrides(self):
        with tempfile.TemporaryDirectory() as directory, HistoryStore(':memory:') as store:
            self.write_raw(directory)
            api = self.api()
            settings = BacktestSettings(symbol_overrides={'WEN': {'quantity_step': '.2',
                                                                'binance_symbol': 'WENALTUSDT'}})
            api.import_raw_history(directory, [], START, END, store, settings)
            wen = api.load_symbol_settings(store, settings, 'WEN')
            ibm = api.load_symbol_settings(store, settings, 'IBM')
            with self.assertRaisesRegex(ValueError, 'INTC.*精度'):
                api.load_symbol_settings(store, settings, 'INTC')
            snapshots = list(store.iter_contracts('WEN-USD.P'))
        self.assertEqual(wen.price_tick, Decimal('.0001'))
        self.assertEqual(wen.quantity_step, Decimal('.2'))
        self.assertEqual(wen.binance_symbol, 'WENALTUSDT')
        self.assertEqual(ibm.quantity_step, Decimal('.01'))
        self.assertEqual(snapshots[0]['source'], 'ondo_markets')

    def test_incomplete_raw_coverage_is_explicit(self):
        with tempfile.TemporaryDirectory() as directory, HistoryStore(':memory:') as store:
            self.write_raw(directory)
            start = parse_time_ns('2026-10-29T00:00:00Z') // 1000000
            self.api().import_raw_history(directory, ['WEN'], start, END,
                                         store, BacktestSettings())
            record = next(store.iter_fetches('ondo'))
        self.assertEqual(record['status'], 'partial')
        self.assertFalse(record['complete'])
        self.assertIn('覆盖', record['error'])

    def test_missing_symbol_is_reported_without_aborting_available_data(self):
        with tempfile.TemporaryDirectory() as directory, HistoryStore(':memory:') as store:
            self.write_raw(directory)
            result = self.api().import_raw_history(directory, ['MISSING', 'WEN'], START, END,
                                                   store, BacktestSettings())
            missing = [row for row in store.iter_fetches() if
                       row['params'].get('symbol') == 'MISSING']
        self.assertEqual(result['ondo_trades'], 3)
        self.assertTrue(any('MISSING' in error for error in result['errors']))
        self.assertEqual({row['source'] for row in missing}, {'ondo', 'binance'})

    def test_corrected_checksum_gets_new_success_record_instead_of_stale_failure(self):
        with tempfile.TemporaryDirectory() as directory, HistoryStore(':memory:') as store:
            _, path = self.write_raw(directory, bad_checksum=True)
            api = self.api()
            api.import_raw_history(directory, ['WEN'], START, END, store, BacktestSettings())
            Path(str(path) + '.CHECKSUM').write_text(
                hashlib.sha256(path.read_bytes()).hexdigest() + '  ' + path.name + '\n')
            result = api.import_raw_history(directory, ['WEN'], START, END,
                                            store, BacktestSettings())
            records = list(store.iter_fetches('binance'))
        self.assertEqual(result['errors'], [])
        self.assertEqual([row['status'] for row in records], ['error', 'ok'])

    def test_malformed_later_page_preserves_earlier_pages_and_records_error(self):
        with tempfile.TemporaryDirectory() as directory, HistoryStore(':memory:') as store:
            path, _ = self.write_raw(directory)
            with gzip.open(path, 'at') as stream:
                stream.write('{"received_ms":')
            result = self.api().import_raw_history(directory, ['WEN'], START, END,
                                                   store, BacktestSettings())
            record = next(store.iter_fetches('ondo'))
            rows = list(store.iter_ondo_trades('WEN-USD.P'))
        self.assertEqual(len(rows), 3)
        self.assertEqual(record['status'], 'error')
        self.assertFalse(record['complete'])
        self.assertIn(path.name, result['errors'][0])

    def test_invalid_market_precision_is_rejected_before_any_snapshot_write(self):
        with tempfile.TemporaryDirectory() as directory, HistoryStore(':memory:') as store:
            self.write_raw(directory)
            path = Path(directory) / 'ondo_markets_20261104.json'
            payload = json.loads(path.read_text())
            payload['result']['perps']['tradingPairs'][1]['baseIncrement'] = '0'
            path.write_text(json.dumps(payload))
            result = self.api().import_raw_history(directory, [], START, END,
                                                   store, BacktestSettings())
            self.assertEqual(list(store.iter_contracts()), [])
        self.assertTrue(any('精度' in error for error in result['errors']))
