"""C2 offline import regressions for quality events and ignored-record categories."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from pendle.cli import main
from pendle.collected_import import import_collected
from pendle.history import HistoryStore


def record(kind, payload, market='INTC-USD.P'):
    return {'source': 'ondo', 'kind': kind, 'market': market,
            'received_ms': 2000, 'payload': payload,
            'run_id': 'c2', 'connection_id': 'ondo-c2'}


class QualityImportTests(unittest.TestCase):
    def write_rows(self, path):
        quality = {'source': 'ondo', 'market': 'INTC-USD.P',
                   'channel': 'tradesPerps', 'start_ms': 1000,
                   'end_ms': None, 'reason': '单市场长时间无更新'}
        rows = [record('depth', {'bids': []}), record('depth', {'asks': []}),
                record('error', {'error': '本地模拟错误'}),
                record('market_stale', quality),
                record('market_stale', {**quality, 'end_ms': 2000})]
        path.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n'
                                for row in rows), encoding='utf-8')

    def test_market_stale_counts_as_quality_without_becoming_backtest_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'quality.jsonl'
            self.write_rows(source)
            with HistoryStore(':memory:') as store:
                try:
                    counts = import_collected(source, store)
                except ValueError as exc:
                    self.fail(f'质量事件必须可导入：{exc}')
                self.assertEqual(counts.get('market_stale'), 2)
                self.assertEqual(counts['gaps'], 0)
                self.assertEqual(list(store.iter_collector_gaps()), [])
                self.assertEqual(counts['ignored'], 5)
                self.assertEqual(counts['ignored_by_kind'],
                                 {'depth': 2, 'error': 1, 'market_stale': 2})

    def test_cli_explains_ignored_kinds_in_its_json_output(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'quality.jsonl'
            self.write_rows(source)
            output, errors = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                code = main(['import-collected', '--src', str(source),
                             '--db', str(Path(directory) / 'history.sqlite')])
            self.assertEqual(code, 0, errors.getvalue())
            counts = json.loads(output.getvalue())
            self.assertEqual(counts['ignored_by_kind'],
                             {'depth': 2, 'error': 1, 'market_stale': 2})
            self.assertEqual(sum(counts['ignored_by_kind'].values()), counts['ignored'])

    def test_connection_gap_remains_a_gap_alongside_quality_events(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'connection.jsonl'
            payload = {'source': 'ondo', 'market': '*', 'channel': '*',
                       'start_ms': 1000, 'end_ms': 2000, 'reason': '连接静默超时'}
            source.write_text(json.dumps(record('gap', payload, market='*')) + '\n',
                              encoding='utf-8')
            with HistoryStore(':memory:') as store:
                counts = import_collected(source, store)
                self.assertEqual(counts['gaps'], 1)
                self.assertEqual(counts.get('market_stale'), 0)
                self.assertEqual(list(store.iter_collector_gaps()), [payload])


if __name__ == '__main__':
    unittest.main()
