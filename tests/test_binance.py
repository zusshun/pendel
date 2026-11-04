import hashlib
import io
import tempfile
import unittest
import urllib.error
import zipfile
from datetime import date
from decimal import Decimal as D
from pathlib import Path

from pendle.binance import (MAX_FILE_BYTES, BinaryHttpClient,
                            fetch_binance_day, parse_agg_trades_zip)
from pendle.history import HistoryStore
from pendle.market import MarketError, RateLimited


HEADER = ('agg_trade_id,price,quantity,first_trade_id,last_trade_id,'
          'transact_time,is_buyer_maker\n')
CSV = ('101,24.1200,2.50,201,202,1793404800123,true\n'
       '102,24.1300,1.25,203,203,1793404800456,false\n')
FILENAME = 'INTCUSDT-aggTrades-2026-10-31.zip'
BASE = 'https://data.binance.vision/data/futures/um/daily/aggTrades/INTCUSDT/'


def archive(text, name='INTCUSDT-aggTrades-2026-10-31.csv'):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as output:
        output.writestr(name, text)
    return stream.getvalue()


class MemoryStore:
    def __init__(self):
        self.trades = {}
        self.fetches = []

    def upsert_binance_trades(self, rows):
        before = len(self.trades)
        for row in rows:
            self.trades.setdefault((row['symbol'], row['agg_id']), dict(row))
        return len(self.trades) - before

    def record_fetch(self, source, params, **values):
        result = {'source': source, 'params': params, **values}
        self.fetches.append(result)
        return result


class MemoryClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def get_bytes(self, url, max_bytes=MAX_FILE_BYTES):
        self.calls.append((url, max_bytes))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


class BinaryHttpTests(unittest.TestCase):
    def test_binary_get_is_bounded_and_has_public_headers(self):
        seen = []

        class Response(io.BytesIO):
            def read(self, size=-1):
                seen.append(size)
                return super().read(size)

        def opener(request, timeout):
            seen.append((request.get_method(), request.get_header('Accept'), timeout))
            return Response(b'PK\x00\xff')

        client = BinaryHttpClient(timeout_s=2, opener=opener)
        self.assertEqual(client.get_bytes('https://example.invalid/day.zip', max_bytes=8),
                         b'PK\x00\xff')
        self.assertEqual(seen, [('GET', 'application/octet-stream', 2), 9])

    def test_binary_get_rejects_oversized_body(self):
        client = BinaryHttpClient(opener=lambda request, timeout: io.BytesIO(b'12345'))
        with self.assertRaisesRegex(MarketError, '上限'):
            client.get_bytes('https://example.invalid/day.zip', max_bytes=4)

    def test_binary_get_never_reads_more_than_200_mib_plus_one(self):
        reads = []

        class Response(io.BytesIO):
            def read(self, size=-1):
                reads.append(size)
                return b'ok'

        client = BinaryHttpClient(opener=lambda request, timeout: Response())
        client.get_bytes('https://example.invalid/day.zip', max_bytes=MAX_FILE_BYTES * 2)
        self.assertEqual(reads, [200 * 1024 * 1024 + 1])

    def test_binary_get_exposes_rate_limit_with_injected_clock(self):
        def opener(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 429, 'limited',
                                         {'Retry-After': '2'}, io.BytesIO())

        with self.assertRaises(RateLimited) as caught:
            BinaryHttpClient(opener=opener, clock=lambda: 0).get_bytes(
                'https://example.invalid/day.zip')
        self.assertEqual(caught.exception.retry_after_s, 2)


class BinanceParseTests(unittest.TestCase):
    def test_real_column_format_with_and_without_header(self):
        for prefix in ('', HEADER):
            with self.subTest(header=bool(prefix)):
                rows = parse_agg_trades_zip(archive(prefix + CSV), 'INTCUSDT')
                self.assertEqual([row['agg_id'] for row in rows], [101, 102])
                self.assertEqual(rows[0], {
                    'symbol': 'INTCUSDT', 'agg_id': 101, 'price': D('24.1200'),
                    'quantity': D('2.50'), 'first_trade_id': 201,
                    'last_trade_id': 202, 'transact_time': 1793404800123,
                    'is_buyer_maker': True})
                self.assertFalse(rows[1]['is_buyer_maker'])

    def test_invalid_rows_are_rejected_without_partial_result(self):
        for bad in ('103,NaN,2,204,204,1793404800789,true\n',
                    '103,24,-2,204,204,1793404800789,true\n',
                    '103,24,2,204,204,1793404800789,maybe\n',
                    '103,24,2,205,204,1793404800789,true\n',
                    '103,24,2,204,204,1793404800789\n'):
            with self.subTest(row=bad), self.assertRaises(MarketError):
                parse_agg_trades_zip(archive(CSV + bad), 'INTCUSDT')

    def test_invalid_zip_or_missing_csv_is_rejected(self):
        for body in (b'not a zip', archive('说明', name='readme.txt')):
            with self.subTest(body=body), self.assertRaises(MarketError):
                parse_agg_trades_zip(body, 'INTCUSDT')


class BinanceFetchTests(unittest.TestCase):
    def make_client(self, body=None, checksum=None):
        body = archive(HEADER + CSV) if body is None else body
        checksum = (hashlib.sha256(body).hexdigest() + '  ' + FILENAME + '\n').encode() \
            if checksum is None else checksum
        return MemoryClient([body, checksum])

    def test_day_download_checks_sha_before_idempotent_insert_and_records_coverage(self):
        store = MemoryStore()
        for day in ('2026-10-31', date(2026, 10, 31)):
            client = self.make_client()
            times = iter((1, 2))
            waits = []
            result = fetch_binance_day(client, 'INTCUSDT', day, store,
                                       clock=lambda: next(times), sleep=waits.append,
                                       request_interval_ms=200)
            self.assertEqual([call[0] for call in client.calls],
                             [BASE + FILENAME, BASE + FILENAME + '.CHECKSUM'])
            self.assertEqual(waits, [.2])
            self.assertEqual(result['status'], 'ok')
            self.assertEqual(result['started_ms'], 1000)
            self.assertEqual(result['finished_ms'], 2000)
            self.assertEqual(result['coverage_start_ms'], 1793404800123)
            self.assertEqual(result['coverage_end_ms'], 1793404800456)
            self.assertTrue(result['complete'])
        self.assertEqual(len(store.trades), 2)
        self.assertEqual(len(store.fetches), 2)

    def test_bad_checksum_is_recorded_and_nothing_is_inserted(self):
        for checksum in (('0' * 64 + '  ' + FILENAME).encode(), b'invalid',
                         ('0' * 64 + '  another.zip').encode()):
            store = MemoryStore()
            with self.subTest(checksum=checksum), self.assertRaises(MarketError):
                fetch_binance_day(self.make_client(checksum=checksum), 'INTCUSDT',
                                  '2026-10-31', store, clock=lambda: 1)
            self.assertEqual(store.trades, {})
            self.assertEqual(store.fetches[0]['status'], 'error')
            self.assertFalse(store.fetches[0]['complete'])
            self.assertTrue(store.fetches[0]['error'])

    def test_invalid_csv_is_recorded_without_partial_insert(self):
        store = MemoryStore()
        body = archive(CSV + 'bad,row\n')
        with self.assertRaises(MarketError):
            fetch_binance_day(self.make_client(body=body), 'INTCUSDT',
                              '2026-10-31', store, clock=lambda: 1)
        self.assertFalse(store.trades)
        self.assertEqual(store.fetches[0]['row_count'], 0)
        self.assertEqual(store.fetches[0]['status'], 'error')

    def test_empty_valid_archive_has_no_invented_coverage(self):
        store = MemoryStore()
        result = fetch_binance_day(self.make_client(body=archive(HEADER)), 'INTCUSDT',
                                   '2026-10-31', store, clock=lambda: 1)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['row_count'], 0)
        self.assertIsNone(result['coverage_start_ms'])
        self.assertIsNone(result['coverage_end_ms'])

    def test_rate_limit_wait_is_injected_and_retry_is_bounded(self):
        good = self.make_client()
        client = MemoryClient([RateLimited(.5), *list(good.responses)])
        waits = []
        result = fetch_binance_day(client, 'INTCUSDT', '2026-10-31', MemoryStore(),
                                   clock=lambda: 1, sleep=waits.append)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(waits, [.5])
        self.assertEqual(len(client.calls), 3)
        store = MemoryStore()
        client = MemoryClient([RateLimited(.5), RateLimited(.5)])
        with self.assertRaises(RateLimited):
            fetch_binance_day(client, 'INTCUSDT', '2026-10-31', store,
                              clock=lambda: 1, sleep=lambda delay: None, max_retries=1)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(store.fetches[0]['status'], 'error')

    def test_download_failure_is_recorded(self):
        store = MemoryStore()
        with self.assertRaisesRegex(MarketError, '失败'):
            fetch_binance_day(MemoryClient([MarketError('下载失败')]), 'INTCUSDT',
                              '2026-10-31', store, clock=lambda: 1)
        self.assertFalse(store.trades)
        self.assertEqual(store.fetches[0]['error'], '下载失败')

    def test_invalid_symbol_or_day_does_not_request_http(self):
        for symbol, day in (('../INTC', '2026-10-31'), ('INTCUSDT', '2026-02-30')):
            client = MemoryClient([])
            with self.subTest(symbol=symbol, day=day), self.assertRaises(ValueError):
                fetch_binance_day(client, symbol, day, MemoryStore(), clock=lambda: 1)
            self.assertFalse(client.calls)

    def test_real_store_keeps_successful_import_idempotent(self):
        with HistoryStore(':memory:') as store:
            for _ in range(2):
                fetch_binance_day(self.make_client(), 'INTCUSDT', '2026-10-31',
                                  store, clock=lambda: 1)
            rows = list(store.iter_binance_trades('INTCUSDT'))
            self.assertEqual([row['agg_id'] for row in rows], [101, 102])
            self.assertEqual(rows[0]['price'], D('24.1200'))
            self.assertTrue(rows[0]['is_buyer_maker'])
            self.assertEqual(len(list(store.iter_fetches('binance'))), 2)

    def test_real_store_persists_checksum_error_without_trades(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'history.sqlite'
            with HistoryStore(path) as store, self.assertRaises(MarketError):
                fetch_binance_day(self.make_client(checksum=b'bad checksum'),
                                  'INTCUSDT', '2026-10-31', store, clock=lambda: 1)
            with HistoryStore(path, create=False) as store:
                self.assertEqual(list(store.iter_binance_trades('INTCUSDT')), [])
                errors = list(store.iter_fetches('binance'))
                self.assertEqual(len(errors), 1)
                self.assertEqual(errors[0]['status'], 'error')
                self.assertIn('CHECKSUM', errors[0]['error'])

    def test_corrupt_deflate_with_matching_checksum_records_read_error(self):
        body = bytearray(archive(HEADER + CSV))
        name_size = int.from_bytes(body[26:28], 'little')
        extra_size = int.from_bytes(body[28:30], 'little')
        body[30 + name_size + extra_size] = 0xff
        with HistoryStore(':memory:') as store:
            with self.assertRaisesRegex(MarketError, '币安归档读取失败'):
                fetch_binance_day(self.make_client(body=bytes(body)), 'INTCUSDT',
                                  '2026-10-31', store, clock=lambda: 1)
            self.assertEqual(list(store.iter_binance_trades('INTCUSDT')), [])
            errors = list(store.iter_fetches('binance'))
            self.assertEqual(len(errors), 1)
            self.assertEqual(errors[0]['status'], 'error')
            self.assertFalse(errors[0]['complete'])
            self.assertEqual(errors[0]['row_count'], 0)
            self.assertIn('币安归档读取失败', errors[0]['error'])
