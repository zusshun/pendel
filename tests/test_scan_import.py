import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from pendle.history import HistoryStore
from pendle.market import parse_ondo
from pendle.scan_import import depth_observation, import_scan

FIXTURES = Path(__file__).parent / 'fixtures'


def small_depth():
    lines = (FIXTURES / 'depth_scan_small.jsonl').read_text().splitlines()
    return json.loads(lines[1])


class ScanImportTests(unittest.TestCase):
    def test_observation_keeps_only_source_reference_instead_of_raw_payload(self):
        path = FIXTURES / 'depth_scan_small.jsonl'
        with HistoryStore(':memory:') as store:
            import_scan(path, store)
            row = next(store.iter_depth_observations())
        self.assertNotIn('raw', row['payload'])
        self.assertEqual(row['payload']['source_file'], path.name)
        self.assertEqual(row['payload']['source_line'], 2)
        self.assertNotIn('bids', json.dumps(row['payload']))

    def test_real_format_fixture_is_idempotent_including_errors(self):
        with HistoryStore(':memory:') as store:
            first = import_scan(FIXTURES / 'depth_scan_small.jsonl', store,
                                price_tick=Decimal('.1'))
            second = import_scan(FIXTURES / 'depth_scan_small.jsonl', store,
                                 price_tick=Decimal('.1'))
            self.assertEqual(len(list(store.iter_contracts())), 1)
            self.assertEqual(len(list(store.iter_depth_observations())), 1)
            errors = list(store.iter_fetches('scan'))
        self.assertEqual(first, {'contracts': 1, 'depth': 1, 'errors': 1, 'truncated_lines': 0})
        self.assertEqual(second['depth'], 0)
        self.assertEqual(second['contracts'], 0)
        self.assertEqual(second['errors'], 0)
        self.assertEqual(errors[0]['error'], '接口限流')
        self.assertEqual(errors[0]['params']['market'], 'DRAM-USD.P')
        self.assertEqual(len(errors), 1)

    def test_flat_linear_and_band_completeness(self):
        row = small_depth()
        book = parse_ondo(row['payload'], row['market'], row['received_ms'])
        result = depth_observation(book, price_tick=Decimal('.1'), band_bps=25)
        self.assertEqual(result['mid'], Decimal(100))
        self.assertEqual(result['bid'], Decimal('99.9'))
        self.assertEqual(result['ask'], Decimal('100.1'))
        self.assertEqual(result['spread_bps'], Decimal(20))
        self.assertEqual(result['flat_bid_usd'], Decimal('499.2'))
        self.assertEqual(result['flat_ask_usd'], Decimal('901.4'))
        self.assertEqual(result['linear_bid_usd'], Decimal('179.76'))
        self.assertEqual(result['linear_ask_usd'], Decimal('340.44'))
        self.assertEqual(result['bid_last_distance_bps'], Decimal(30))
        self.assertTrue(result['book_band_complete'])

    def test_book_must_extend_strictly_beyond_band_on_both_sides(self):
        row = small_depth()
        row['payload']['result']['bids'][-1] = ['99.75', '9']
        result = depth_observation(parse_ondo(row['payload'], row['market'], row['received_ms']),
                                   price_tick='.1', band_bps=25)
        self.assertFalse(result['book_band_complete'])

    def test_tick_depth_uses_price_grid_and_includes_missing_levels(self):
        row = small_depth()
        result = depth_observation(parse_ondo(row['payload'], row['market'], row['received_ms']),
                                   price_tick='.1', max_tick=3)
        ticks = result['tick_depth']
        self.assertEqual(len(ticks['bids']), 4)
        self.assertEqual(ticks['bids'][0]['quantity'], '0')
        self.assertEqual(ticks['bids'][1]['price'], '99.9')
        self.assertEqual(ticks['bids'][1]['quantity'], '2')
        self.assertEqual(ticks['asks'][2]['usd'], '501.0')
        self.assertEqual(ticks['bids'][3]['tick'], 3)

    def test_ondo_depth_existing_fixture_parses_without_new_payload_format(self):
        payload = json.loads((FIXTURES / 'ondo_depth.json').read_text())
        result = depth_observation(parse_ondo(payload, 'INTC-USD.P', 1000), price_tick='.01')
        self.assertEqual(result['mid'], Decimal('62.695'))
        self.assertTrue(result['book_band_complete'])
        self.assertEqual(result['tick_depth']['bids'][0]['price'], '62.69')

    def test_truncated_tail_is_skipped_and_counted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'tail.jsonl'
            path.write_bytes((FIXTURES / 'depth_scan_small.jsonl').read_bytes() + b'{"kind":"depth"')
            with HistoryStore(':memory:') as store:
                result = import_scan(path, store)
                self.assertEqual(len(list(store.iter_depth_observations())), 1)
        self.assertEqual(result['truncated_lines'], 1)

    def test_complete_final_line_without_newline_is_imported(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'tail.jsonl'
            path.write_text(json.dumps(small_depth()))
            with HistoryStore(':memory:') as store:
                result = import_scan(path, store)
        self.assertEqual(result['depth'], 1)
        self.assertEqual(result['truncated_lines'], 0)

    def test_malformed_interior_line_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'bad.jsonl'
            path.write_text('{"kind":\n' + json.dumps(small_depth()) + '\n')
            with HistoryStore(':memory:') as store, self.assertRaisesRegex(ValueError, '行'):
                import_scan(path, store)

    def test_directory_import_and_symbol_precision_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'scan.jsonl'
            path.write_text(json.dumps(small_depth()) + '\n')
            with HistoryStore(':memory:') as store:
                result = import_scan(directory, store, symbol_overrides={'INTC': {'price_tick': '.1'}})
                rows = list(store.iter_depth_observations('INTC-USD.P', 1793726526523, 1793726526524))
        self.assertEqual(result['depth'], 1)
        self.assertEqual(rows[0]['tick_depth']['bids'][1]['price'], '99.9')
        self.assertEqual(rows[0]['flat_bid_usd'], Decimal('499.2'))

    def test_invalid_book_becomes_a_persistent_error(self):
        row = small_depth()
        row['payload']['result']['asks'] = []
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'bad.jsonl'
            path.write_text(json.dumps(row) + '\n')
            with HistoryStore(':memory:') as store:
                first = import_scan(path, store)
                second = import_scan(path, store)
                self.assertEqual(list(store.iter_depth_observations()), [])
                self.assertEqual(len(list(store.iter_fetches('scan'))), 1)
        self.assertEqual(first['errors'], 1)
        self.assertEqual(second['errors'], 0)

    def test_unknown_kind_is_rejected_instead_of_silently_dropped(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'bad.jsonl'
            path.write_text('{"kind":"unexpected","received_ms":0}\n')
            with HistoryStore(':memory:') as store, self.assertRaises(ValueError):
                import_scan(path, store)
