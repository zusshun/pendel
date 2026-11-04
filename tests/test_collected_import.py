"""Recover complete lines from collected archives and import them idempotently using natural keys."""

import gzip
import importlib.util
import json
import tempfile
import unittest
import zlib
from pathlib import Path

from pendle.history import HistoryStore
from pendle.market import parse_ondo
from pendle.scan_import import depth_observation


def record(kind, payload, source='ondo', market='INTC-USD.P', at=1000):
    return dict(source=source, kind=kind, market=market, received_ms=at,
                payload=payload, run_id='run', connection_id='connection')


def trade(identity='trade'):
    return dict(id=identity, market='INTC-USD.P', time='2026-11-04T00:00:00.123456789Z',
                price='100', size='2', cost='200', aggressor_side='sell')


def gap(end=None, source='ondo', market='INTC-USD.P', channel='tradesPerps'):
    return dict(source=source, market=market, channel=channel,
                start_ms=1000, end_ms=end, reason='断线')


class CollectedImportTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('pendle.collected_import'),
                             '必须实现采集文件导入模块')
        from pendle.collected_import import import_collected
        self.import_collected = import_collected
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def write(self, rows, name='ondo/20261104T00Z-run.jsonl.gz'):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        body = ''.join(json.dumps(row, default=str, ensure_ascii=False) + '\n'
                       for row in rows).encode()
        path.write_bytes(gzip.compress(body) if name.endswith('.gz') else body)
        return path

    def test_recursively_imports_all_supported_kinds_idempotently(self):
        book = parse_ondo({'success': True, 'result': {
            'market': 'INTC-USD.P', 'time': '2026-11-04T00:00:00Z',
            'bids': [['99.9', '2'], ['99', '3']],
            'asks': [['100.1', '2'], ['101', '3']]}}, 'INTC-USD.P', 1000)
        observation = depth_observation(book, source='ws')
        self.write([record('ondo_trade', trade()),
                    record('depth_observation', observation),
                    record('contracts', {'success': True, 'result': [{'market': 'INTC-USD.P'}]}),
                    record('gap', gap()), record('gap', gap(2000)),
                    record('depth', {'bids': []}), record('error', {'error': '请求失败'})])
        self.write([record('binance_trade', dict(symbol='INTCUSDT', agg_id=1,
            price='100', quantity='2', first_trade_id=1, last_trade_id=2,
            transact_time=1000, is_buyer_maker=False), 'binance', 'INTCUSDT')],
            'binance/20261104T00Z-run.jsonl.gz')
        with HistoryStore(':memory:') as store:
            first = self.import_collected(self.root, store)
            second = self.import_collected(self.root, store)
            self.assertEqual(first['ondo_trades'], 1)
            self.assertEqual(first['binance_trades'], 1)
            self.assertEqual(first['depth_observations'], 1)
            self.assertEqual(first['contracts'], 1)
            self.assertEqual(first['gaps'], 2)
            self.assertEqual(first['ignored'], 2)
            for key in ('ondo_trades', 'binance_trades', 'depth_observations', 'contracts', 'gaps'):
                self.assertEqual(second[key], 0, key)
            self.assertEqual(list(store.iter_ondo_trades('INTC-USD.P'))[0]['time_ns'],
                             1793750400123456789)
            self.assertEqual(list(store.iter_depth_observations())[0]['source'], 'ws')
            self.assertEqual(list(store.iter_collector_gaps())[0]['end_ms'], 2000)

    def test_truncated_gzip_trailer_preserves_every_complete_line(self):
        path = self.write([record('ondo_trade', trade('a')), record('ondo_trade', trade('b'))])
        path.write_bytes(path.read_bytes()[:-6])
        with HistoryStore(':memory:') as store:
            counts = self.import_collected(path, store)
            self.assertEqual(counts['ondo_trades'], 2)
            self.assertEqual(counts['truncated_files'], 1)
            self.assertEqual(len(list(store.iter_ondo_trades('INTC-USD.P'))), 2)

    def test_truncated_compressed_body_keeps_lines_before_partial_record(self):
        path = self.write([])
        compressor = zlib.compressobj(wbits=31)
        first = (json.dumps(record('ondo_trade', trade('first'))) + '\n').encode()
        body = compressor.compress(first) + compressor.flush(zlib.Z_SYNC_FLUSH)
        partial = compressor.compress(b'{"kind":"ondo_trade","payload":')
        partial += compressor.flush(zlib.Z_SYNC_FLUSH)
        path.write_bytes(body + partial[:-2])
        with HistoryStore(':memory:') as store:
            counts = self.import_collected(path, store)
            self.assertEqual(counts['ondo_trades'], 1)
            self.assertEqual(counts['truncated_files'], 1)
            self.assertEqual(counts['truncated_lines'], 1)

    def test_truncated_line_is_skipped_but_complete_final_json_is_retained(self):
        path = self.write([record('ondo_trade', trade())], 'ondo/tail.jsonl')
        path.write_bytes(path.read_bytes() + b'{"kind":"ondo_trade"')
        with HistoryStore(':memory:') as store:
            counts = self.import_collected(path, store)
            self.assertEqual(counts['ondo_trades'], 1)
            self.assertEqual(counts['truncated_lines'], 1)
        path.write_text(json.dumps(record('ondo_trade', trade())), encoding='utf-8')
        with HistoryStore(':memory:') as store:
            self.assertEqual(self.import_collected(path, store)['ondo_trades'], 1)

    def test_malformed_interior_line_is_rejected_with_chinese_location(self):
        path = self.write([], 'ondo/bad.jsonl')
        path.write_bytes(b'{"kind":\n{}\n')
        with HistoryStore(':memory:') as store, self.assertRaisesRegex(ValueError, '第 1 行'):
            self.import_collected(path, store)

    def test_unknown_kind_is_rejected_instead_of_silently_ignored(self):
        path = self.write([record('unexpected', {})])
        with HistoryStore(':memory:') as store, self.assertRaisesRegex(ValueError, 'kind'):
            self.import_collected(path, store)

    def test_gap_query_uses_source_market_channel_and_overlap(self):
        with HistoryStore(':memory:') as store:
            store.upsert_collector_gaps([gap(), gap(3000, 'binance', 'INTCUSDT', 'aggTrade')])
            store.upsert_collector_gaps([gap(2000)])
            store.upsert_collector_gaps([gap()])
            rows = list(store.iter_collector_gaps(source='ondo', market='INTC-USD.P',
                channel='tradesPerps', start_ms=1500, end_ms=2500))
            self.assertEqual(rows, [gap(2000)])
            self.assertEqual(list(store.iter_collector_gaps(start_ms=3000)), [])

    def test_invalid_gap_batch_is_atomic(self):
        with HistoryStore(':memory:') as store:
            with self.assertRaisesRegex(ValueError, '断档'):
                store.upsert_collector_gaps([gap(), {**gap(), 'end_ms': 999}])
            self.assertEqual(list(store.iter_collector_gaps()), [])
