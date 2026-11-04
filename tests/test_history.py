import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from pendle.history import (HistoryStore, fetch_contracts, fetch_ondo_trades,
                            parse_time_ns)
from pendle.market import MarketError, RateLimited

FIXTURES = Path(__file__).parent / 'fixtures'
BASE_MS = 1793408400000


def trade(identity='b', second=1, fraction='123456789'):
    return {'market': 'INTC-USD.P', 'price': '62.71', 'size': '1.2',
            'cost': '75.252', 'aggressor_side': 'buy',
            'time': f'2026-10-31T01:00:{second:02d}.{fraction}Z', 'id': identity}


def page(rows, cursor=None):
    return {'success': True, 'result': rows, 'pageInfo': {'nextCursor': cursor}}


class FakeClock:
    def __init__(self):
        self.value = 1000
        self.sleeps = []

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += seconds


class FakeHttp:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.urls = []

    def get_json(self, url):
        self.urls.append(url)
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return result


class HistoryTests(unittest.TestCase):
    def test_nanoseconds_are_exact_and_timezone_aware(self):
        self.assertEqual(parse_time_ns('2026-10-31T01:00:01.123456789Z'),
                         BASE_MS * 1000000 + 1123456789)
        self.assertEqual(parse_time_ns('2026-10-31T09:00:01.123456789+08:00'),
                         BASE_MS * 1000000 + 1123456789)
        self.assertEqual(parse_time_ns('1970-01-01T00:00:00.1Z'), 100000000)
        for value in ('2026-10-31T01:00:00', 'bad',
                      '2026-10-31T01:00:00.1234567890Z'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_time_ns(value)

    def test_stable_order_idempotency_and_late_arrival(self):
        with HistoryStore(':memory:') as store:
            self.assertEqual(store.upsert_ondo_trades([trade('z'), trade('b')]), 2)
            self.assertEqual(store.upsert_ondo_trades([trade('b'), trade('a')]), 1)
            store.upsert_ondo_trades([trade('earlier', 0)])
            rows = list(store.iter_ondo_trades('INTC-USD.P'))
        self.assertEqual([r['id'] for r in rows], ['earlier', 'a', 'b', 'z'])
        self.assertEqual(rows[1]['price'], Decimal('62.71'))
        self.assertEqual(rows[1]['time_ns'], BASE_MS * 1000000 + 1123456789)

    def test_batch_is_atomic_on_invalid_trade(self):
        with HistoryStore(':memory:') as store:
            bad = {**trade('bad'), 'price': '-1'}
            with self.assertRaises(ValueError):
                store.upsert_ondo_trades([trade(), bad])
            self.assertEqual(list(store.iter_ondo_trades('INTC-USD.P')), [])

    def test_ondo_time_window_is_half_open(self):
        with HistoryStore(':memory:') as store:
            store.upsert_ondo_trades([trade('a', 0, '000000000'),
                                      trade('b', 1, '000000000')])
            rows = list(store.iter_ondo_trades('INTC-USD.P', BASE_MS, BASE_MS + 1000))
        self.assertEqual([r['id'] for r in rows], ['a'])

    def test_pagination_crosses_start_and_records_real_coverage(self):
        clock = FakeClock()
        client = FakeHttp([page([trade('new', 5), trade('b', 2)], 'cursor+1'),
                           page([trade('a', 2), trade('b', 2), trade('old', 0)], 'unused')])
        with HistoryStore(':memory:') as store:
            result = fetch_ondo_trades(client, 'INTC-USD.P', BASE_MS + 1000,
                                      BASE_MS + 4000, store, clock=clock.now,
                                      sleep=clock.sleep, request_interval_ms=250)
            self.assertEqual([r['id'] for r in store.iter_ondo_trades('INTC-USD.P')], ['a', 'b'])
            saved = list(store.iter_fetches())
        self.assertTrue(result['complete'])
        self.assertEqual(result['row_count'], 2)
        self.assertEqual(result['coverage_start_ms'], BASE_MS + 123)
        self.assertEqual(result['coverage_end_ms'], BASE_MS + 5123)
        self.assertEqual(len(client.urls), 2)
        self.assertEqual(parse_qs(urlparse(client.urls[1]).query)['cursor'], ['cursor+1'])
        self.assertEqual(clock.sleeps, [.25])
        self.assertEqual(saved[0]['params']['market'], 'INTC-USD.P')

    def test_equal_start_timestamp_does_not_stop_pagination(self):
        client = FakeHttp([page([trade('b', 1, '000000000')], 'next'),
                           page([trade('a', 1, '000000000'), trade('old', 0)], None)])
        with HistoryStore(':memory:') as store:
            fetch_ondo_trades(client, 'INTC-USD.P', BASE_MS + 1000, BASE_MS + 2000,
                              store, clock=lambda: 0, sleep=lambda _: None)
            self.assertEqual([r['id'] for r in store.iter_ondo_trades('INTC-USD.P')], ['a', 'b'])
        self.assertEqual(len(client.urls), 2)

    def test_exhausted_cursor_is_incomplete_without_fabricating_coverage(self):
        with HistoryStore(':memory:') as store:
            result = fetch_ondo_trades(FakeHttp([page([trade('a', 2)])]),
                                      'INTC-USD.P', BASE_MS, BASE_MS + 3000, store,
                                      clock=lambda: 0, sleep=lambda _: None)
        self.assertFalse(result['complete'])
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['coverage_start_ms'], BASE_MS + 2123)
        self.assertIn('覆盖', result['error'])

    def test_rate_limit_respects_retry_after_and_request_interval(self):
        clock = FakeClock()
        with HistoryStore(':memory:') as store:
            fetch_ondo_trades(FakeHttp([RateLimited(7), page([trade('old', 0)])]),
                              'INTC-USD.P', BASE_MS + 1000, BASE_MS + 3000,
                              store, clock=clock.now, sleep=clock.sleep,
                              request_interval_ms=250)
        self.assertEqual(clock.sleeps, [7])

    def test_error_preserves_previous_pages_and_coverage(self):
        with HistoryStore(':memory:') as store:
            result = fetch_ondo_trades(FakeHttp([page([trade('a', 2)], 'next'),
                                                MarketError('接口失败')]),
                                      'INTC-USD.P', BASE_MS, BASE_MS + 3000, store,
                                      clock=lambda: 0, sleep=lambda _: None)
            self.assertEqual(len(list(store.iter_ondo_trades('INTC-USD.P'))), 1)
            self.assertEqual(len(list(store.iter_fetches())), 1)
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['coverage_start_ms'], BASE_MS + 2123)
        self.assertEqual(result['error'], '接口失败')

    def test_repeated_cursor_stops_with_error(self):
        client = FakeHttp([page([trade()], 'same'), page([trade()], 'same')])
        with HistoryStore(':memory:') as store:
            result = fetch_ondo_trades(client, 'INTC-USD.P', BASE_MS, BASE_MS + 3000,
                                      store, clock=lambda: 0, sleep=lambda _: None)
        self.assertEqual(result['status'], 'error')
        self.assertIn('游标', result['error'])

    def test_contract_snapshot_is_preserved_and_idempotent(self):
        payload = json.loads((FIXTURES / 'ondo_contracts.json').read_text())
        with HistoryStore(':memory:') as store:
            fetch_contracts(FakeHttp([payload]), store, clock=lambda: 1000)
            store.upsert_contracts(payload, 1000000)
            rows = list(store.iter_contracts())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['payload']['fundingRate'], '0.0000063')

    def test_binance_storage_deduplicates_and_orders(self):
        row = {'symbol': 'INTCUSDT', 'agg_id': 2, 'price': '62.7', 'quantity': '1',
               'first_trade_id': 3, 'last_trade_id': 3, 'transact_time': 1000,
               'is_buyer_maker': True}
        with HistoryStore(':memory:') as store:
            self.assertEqual(store.upsert_binance_trades([row, row]), 1)
            store.upsert_binance_trades([{**row, 'agg_id': 1}])
            rows = list(store.iter_binance_trades('INTCUSDT', 1000, 1001))
        self.assertEqual([r['agg_id'] for r in rows], [1, 2])
        self.assertIs(rows[0]['is_buyer_maker'], True)
        self.assertEqual(rows[0]['quantity'], Decimal(1))

    def test_invalid_response_shape_is_recorded_as_fetch_error(self):
        payloads = [None, [], {'success': True, 'result': [], 'pageInfo': None},
                    {'success': True, 'result': [None]}]
        for payload in payloads:
            with self.subTest(payload=payload), HistoryStore(':memory:') as store:
                result = fetch_ondo_trades(FakeHttp([payload]), 'INTC-USD.P',
                                          BASE_MS, BASE_MS + 1000, store,
                                          clock=lambda: 0, sleep=lambda _: None)
                self.assertEqual(result['status'], 'error')
                self.assertEqual(len(list(store.iter_fetches())), 1)
                self.assertEqual(list(store.iter_ondo_trades('INTC-USD.P')), [])

    def test_invalid_contract_response_is_recorded_and_batch_rolled_back(self):
        payloads = [None, {'success': True, 'result': [{'market': 'INTC-USD.P'}, None]}]
        for payload in payloads:
            with self.subTest(payload=payload), HistoryStore(':memory:') as store:
                result = fetch_contracts(FakeHttp([payload]), store,
                                         clock=lambda: 0, sleep=lambda _: None)
                self.assertEqual(result['status'], 'error')
                self.assertEqual(len(list(store.iter_fetches())), 1)
                self.assertEqual(list(store.iter_contracts()), [])
