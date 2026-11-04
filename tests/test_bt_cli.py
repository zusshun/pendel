"""Test offline end-to-end replay through the historical CLI using real local archives."""

import contextlib
import gzip
import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

from pendle.cli import main
from pendle.bt_cli import _backtests
from pendle.bt_config import BacktestSettings
from pendle.history import HistoryStore, parse_time_ns


class BacktestCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.db = self.root / 'history.sqlite'
        self.raw = self.root / 'raw'
        (self.raw / 'ondo_trades').mkdir(parents=True)
        (self.raw / 'binance').mkdir()
        self.start = '2026-10-31T00:30:00Z'
        self.end = '2026-10-31T00:33:01Z'
        self.start_ms = parse_time_ns(self.start) // 1000000
        self.config = self.root / 'backtest.toml'
        self.config.write_text('data_latency_ms = 0\nplace_latency_ms = 0\n'
                               'max_ref_age_s = "600"\nguard_move_bps = "1000"\n',
                               encoding='utf-8')

    def command(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                result = main(list(map(str, args)))
            except SystemExit as exc:
                result = exc.code
        return result, stdout.getvalue(), stderr.getvalue()

    def make_raw(self, symbol='INTC', price='100'):
        rows = []
        for index, (time, side, value) in enumerate((
                (self.start, 'buy', price), ('2026-10-31T00:30:01Z', 'sell', price),
                ('2026-10-31T00:31:01Z', 'sell', str(float(price) * .998)),
                ('2026-10-31T00:29:59Z', 'sell', price),
                ('2026-10-31T00:33:02Z', 'buy', price))):
            rows.append({'id': symbol + str(index), 'market': symbol + '-USD.P',
                         'time': time, 'price': value, 'size': '2', 'cost': '200',
                         'aggressor_side': side})
        with gzip.open(self.raw / 'ondo_trades' / f'{symbol}_until_20261030.jsonl.gz',
                       'wt', encoding='utf-8') as stream:
            stream.write(json.dumps({'received_ms': self.start_ms + 999999,
                                     'cursor': None, 'payload': {
                                         'success': True, 'result': rows, 'pageInfo': {}}}) + '\n')
        filename = f'{symbol}USDT-aggTrades-2026-10-31.zip'
        body = io.BytesIO()
        with zipfile.ZipFile(body, 'w') as archive:
            archive.writestr('trades.csv', '\n'.join(
                f'{index},100,2,{index},{index},{self.start_ms + index * 1000},false'
                for index in range(182)) + '\n')
        path = self.raw / 'binance' / filename
        path.write_bytes(body.getvalue())
        Path(str(path) + '.CHECKSUM').write_text(
            hashlib.sha256(body.getvalue()).hexdigest() + '  ' + filename + '\n')
        return path

    def test_offline_end_to_end_and_continue_after_mapping_rejection(self):
        self.make_raw()
        self.make_raw('BB', '10')
        markets = {'success': True, 'result': {'perps': {'tradingPairs': [
            {'market': 'INTC-USD.P', 'quoteIncrement': '.01', 'baseIncrement': '.1'},
            {'market': 'BB-USD.P', 'quoteIncrement': '.01', 'baseIncrement': '.1'}]}}}
        (self.raw / 'ondo_markets_20261104.json').write_text(json.dumps(markets))
        with patch('urllib.request.urlopen', side_effect=AssertionError('离线命令禁止联网')):
            code, _, error = self.command('fetch-history', '--raw-dir', self.raw,
                '--symbols', 'INTC,BB', '--start', self.start, '--end', self.end, '--db', self.db)
            self.assertEqual(code, 0, error)
            scan = self.root / 'scan.jsonl'
            scan.write_bytes(Path('tests/fixtures/depth_scan_small.jsonl').read_bytes())
            code, _, error = self.command('import-scan', '--src', scan, '--db', self.db)
            self.assertEqual(code, 0, error)
            ranking, ranking_md = self.root / 'competition.json', self.root / 'competition.md'
            code, _, error = self.command('competition', '--db', self.db,
                '--report', ranking, '--summary', ranking_md)
            self.assertEqual(code, 0, error)
            self.assertTrue(json.loads(ranking.read_text()))
            self.assertIn('竞争', ranking_md.read_text())
            report, summary = self.root / 'bt.json', self.root / 'bt.md'
            code, _, error = self.command('backtest', '--db', self.db, '--config', self.config,
                '--symbols', 'INTC,BB', '--start', self.start, '--end', self.end,
                '--report', report, '--summary', summary)
            self.assertEqual(code, 0, error)
        reports = json.loads(report.read_text())['reports']
        self.assertEqual(len(reports), 8)
        by_mode = {(row['symbol'], row['ref_fallback'], row['hedge_mode']): row for row in reports}
        for fallback, mode in ((fallback, mode) for fallback in ('ondo_mid', 'none')
                               for mode in ('none', 'lighter_proxy')):
            accepted, rejected = by_mode['INTC', fallback, mode], by_mode['BB', fallback, mode]
            self.assertEqual(accepted['status'], 'ok')
            self.assertEqual(accepted['settings']['hedge_mode'], mode)
            self.assertTrue(accepted['coverage'].get('complete'))
            self.assertEqual(rejected['status'], 'rejected')
            self.assertIn('映射', rejected['reason'])
            self.assertEqual([row['at_price_fill_ratio'] for row in accepted['sensitivity']],
                             ['0', '0.5', '1'])
            expected = ['ok'] * 3 if fallback == 'ondo_mid' else ['not_run', 'ok', 'not_run']
            self.assertEqual([row['status'] for row in accepted['sensitivity']], expected)
            self.assertNotIn('events', accepted)
        self.assertIn('INTC', summary.read_text())
        with HistoryStore(self.db, create=False) as store:
            self.assertEqual(len(list(store.iter_ondo_trades('INTC-USD.P'))), 5)

    def test_backtest_event_details_are_opt_in(self):
        self.make_raw()
        markets = {'success': True, 'result': {'perps': {'tradingPairs': [
            {'market': 'INTC-USD.P', 'quoteIncrement': '.01', 'baseIncrement': '.1'}]}}}
        (self.raw / 'ondo_markets_20261104.json').write_text(json.dumps(markets))
        code, _, error = self.command('fetch-history', '--raw-dir', self.raw,
            '--symbols', 'INTC', '--start', self.start, '--end', self.end, '--db', self.db)
        self.assertEqual(code, 0, error)
        for include_events in (False, True):
            args = ['backtest', '--db', self.db, '--config', self.config,
                    '--symbols', 'INTC', '--start', self.start, '--end', self.end]
            if include_events:
                args.append('--events')
            code, output, error = self.command(*args)
            self.assertEqual(code, 0, error)
            for report in json.loads(output)['reports']:
                for field in ('events', 'ondo_trades', 'binance_prices', 'center_prices'):
                    self.assertEqual(field in report, include_events, field)
                    if include_events:
                        self.assertTrue(report[field], field)
                self.assertIn('fills', report)
                self.assertIn('minutes', report)
                self.assertTrue(report['minutes'])
                for minute in report['minutes']:
                    for field in ('before_events', 'online_breakdown_ms'):
                        self.assertEqual(field in minute, include_events, field)
                    if not include_events:
                        self.assertEqual(set(minute), {'time_ms', 'my_weight', 'two_sided',
                            'online', 'guard_paused', 'center', 'reward_mid'})

    def test_events_option_is_only_available_for_backtest(self):
        code, _, error = self.command('competition', '--db', self.db, '--events')
        self.assertEqual(code, 2)
        self.assertIn('--events', error)

    def test_warmup_keeps_old_valid_basis_sample_and_its_pairing_reference(self):
        settings = BacktestSettings(symbols=('INTC',), max_basis_age_s='3600',
                                    basis_pair_max_age_s='30', max_ref_age_s='120',
                                    at_price_fill_ratios=('0.5',), data_latency_ms=100,
                                    symbol_overrides={'INTC': {'price_tick': '.01', 'quantity_step': '.1'}})
        sample_ms = self.start_ms - 3590 * 1000
        paired_ms = sample_ms - 20 * 1000
        old_time = datetime.fromtimestamp(sample_ms / 1000, timezone.utc).isoformat()
        with HistoryStore(self.db) as store:
            store.upsert_ondo_trades([{'id': 'warmup', 'market': 'INTC-USD.P',
                'time': old_time, 'price': '100', 'size': '1', 'cost': '100',
                'aggressor_side': 'buy'}])
            store.upsert_binance_trades([{'symbol': 'INTCUSDT', 'agg_id': 1,
                'price': '100', 'quantity': '1', 'first_trade_id': 1, 'last_trade_id': 1,
                'transact_time': paired_ms, 'is_buyer_maker': False}])
            seen = []

            def replay(ondo, binance, *args, **kwargs):
                seen.append((list(ondo), list(binance)))
                return {'coverage': {}}

            with patch('pendle.backtest.run_backtest', side_effect=replay), \
                    patch('pendle.bt_report.build_report', return_value={}):
                _backtests(store, settings, self.start_ms, self.start_ms + 1000)
        self.assertTrue(seen)
        for ondo, binance in seen:
            self.assertEqual([row['id'] for row in ondo], ['warmup'])
            self.assertEqual([row['transact_time'] for row in binance], [paired_ms])

    def test_backtests_compare_fallbacks_without_repeating_none_sensitivity(self):
        settings = BacktestSettings(symbols=('INTC',),
            symbol_overrides={'INTC': {'price_tick': '.01', 'quantity_step': '.1'}})
        seen = []

        def replay(ondo, binance, mode_settings, **kwargs):
            seen.append((getattr(mode_settings, 'ref_fallback', 'missing'), mode_settings.hedge_mode,
                         mode_settings.at_price_fill_ratio, kwargs['gaps']))
            return {'coverage': {}}

        with HistoryStore(self.db) as store:
            store.upsert_collector_gaps([dict(source='binance', market='*', channel='*',
                start_ms=self.start_ms, end_ms=self.start_ms + 500, reason='连接静默')])
            with patch('pendle.backtest.run_backtest', side_effect=replay), \
                    patch('pendle.bt_report.build_report', side_effect=lambda *args, **kwargs: {}):
                value = _backtests(store, settings, self.start_ms, self.start_ms + 1000)
        self.assertEqual(len(value['reports']), 4)
        self.assertEqual({(row['ref_fallback'], row['hedge_mode']) for row in value['reports']},
                         {(fallback, mode) for fallback in ('ondo_mid', 'none')
                          for mode in ('none', 'lighter_proxy')})
        self.assertEqual(sum(row[0] == 'ondo_mid' for row in seen), 6)
        self.assertEqual(sum(row[0] == 'none' for row in seen), 2)
        self.assertTrue(all(len(row[3]) == 1 and row[3][0]['market'] == '*' for row in seen))

    def test_recent_basis_median_expires_each_old_sample_even_when_latest_is_fresh(self):
        settings = BacktestSettings(symbols=('INTC',), at_price_fill_ratios=('0.5',),
                                    data_latency_ms=0, place_latency_ms=0,
                                    symbol_overrides={'INTC': {'price_tick': '.01', 'quantity_step': '.1'}})
        times = [self.start_ms - 7 * 3600 * 1000 + index * 1000 for index in range(19)]
        times += [self.start_ms - 1000, self.start_ms + 5000]
        with HistoryStore(self.db) as store:
            store.upsert_ondo_trades([{'id': str(index), 'market': 'INTC-USD.P',
                'time': datetime.fromtimestamp(time / 1000, timezone.utc).isoformat(),
                'price': '101' if index < 19 else '100', 'size': '1', 'cost': '100',
                # The old two-sided samples have expired; use one fresh buy and one fresh sell for per-side medians.
                'aggressor_side': 'buy' if index % 2 == 0 else 'sell'}
                for index, time in enumerate(times)])
            store.upsert_binance_trades([{'symbol': 'INTCUSDT', 'agg_id': index,
                'price': '100', 'quantity': '1', 'first_trade_id': index,
                'last_trade_id': index, 'transact_time': time, 'is_buyer_maker': False}
                for index, time in enumerate(times)])
            result = _backtests(store, settings, self.start_ms, self.start_ms + 10000,
                                include_events=True)
        self.assertEqual(len(result['reports']), 4)
        for report in result['reports']:
            self.assertEqual(report['status'], 'ok')
            centers = [row['price'] for row in report['center_prices'] if row['price'] is not None]
            self.assertTrue(centers)
            self.assertEqual(centers[0], Decimal('100'))

    def test_invalid_window_and_symbols_return_chinese_error(self):
        for symbols, start, end in (('INTC', self.end, self.start),
                                     ('bad', self.start, self.end)):
            code, _, error = self.command('fetch-history', '--raw-dir', self.raw,
                '--symbols', symbols, '--start', start, '--end', end, '--db', self.db)
            self.assertEqual(code, 2)
            self.assertIn('错误', error)

    def test_fetch_continues_after_failed_archive_without_real_network(self):
        seen = []

        def archive(client, symbol, day, store, **kwargs):
            seen.append((symbol, day.isoformat()))
            row = store.record_fetch('binance', {'symbol': symbol}, started_ms=0,
                finished_ms=0, status='error' if len(seen) == 1 else 'ok',
                error='本地模拟下载失败' if len(seen) == 1 else None)
            if len(seen) == 1:
                raise ValueError('本地模拟下载失败')
            return row

        with patch('pendle.binance.BinaryHttpClient'), \
                patch('pendle.binance.fetch_binance_day', side_effect=archive), \
                patch('pendle.history.fetch_contracts', return_value={'status': 'ok'}), \
                patch('pendle.history.fetch_ondo_trades', return_value={'status': 'ok'}):
            code, output, error = self.command('fetch-history', '--symbols', 'INTC,CRCL',
                '--start', self.start, '--end', '2026-11-01T21:10:00Z', '--db', self.db)
        self.assertEqual(code, 1, error)
        self.assertEqual(len(seen), 4)
        self.assertEqual(json.loads(output)['errors'], 1)

    def test_history_outputs_cannot_overwrite_database_or_each_other(self):
        with HistoryStore(self.db):
            pass
        original = self.db.read_bytes()
        code, _, error = self.command('competition', '--db', self.db, '--summary', self.db)
        self.assertEqual(code, 2)
        self.assertIn('覆盖', error)
        self.assertEqual(self.db.read_bytes(), original)
        output = self.root / 'same.json'
        code, _, error = self.command('competition', '--db', self.db,
                                      '--report', output, '--summary', output)
        self.assertEqual(code, 2)
        self.assertIn('覆盖', error)


if __name__ == '__main__':
    unittest.main()
