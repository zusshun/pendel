"""精度缺失仅拒绝对应标的，其他标的继续产出报告。"""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from pendle.cli import main
from pendle.history import HistoryStore


class DBacktestCliTests(unittest.TestCase):
    def test_one_symbol_missing_precision_does_not_abort_other_symbols(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / 'history.sqlite'
            with HistoryStore(db) as store:
                store.upsert_contracts({'success': True, 'result': [dict(market='INTC-USD.P',
                    quoteIncrement='.01', baseIncrement='.1')]}, 0, source='ondo_markets')
                store.upsert_ondo_trades([dict(id=side, market='INTC-USD.P',
                    time='1970-01-01T00:00:00Z', price='100', size='1', cost='100',
                    aggressor_side=side) for side in ('buy', 'sell')])
                store.upsert_binance_trades([dict(symbol='INTCUSDT', agg_id=0,
                    price='100', quantity='1', first_trade_id=0, last_trade_id=0,
                    transact_time=0, is_buyer_maker=False)])
            before = db.read_bytes()
            output, error = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
                code = main(['backtest', '--db', str(db), '--symbols', 'INTC,WEN',
                    '--start', '1970-01-01T00:00:00Z', '--end', '1970-01-01T00:00:02Z'])
            self.assertEqual(code, 0, error.getvalue())
            reports = json.loads(output.getvalue())['reports']
            self.assertEqual(len(reports), 8)
            self.assertTrue(all(row['status'] == 'ok' for row in reports if row['symbol'] == 'INTC'))
            for row in reports:
                if row['symbol'] == 'WEN':
                    self.assertEqual(row['status'], 'rejected')
                    self.assertIn('精度', row['reason'])
            self.assertEqual(db.read_bytes(), before)
