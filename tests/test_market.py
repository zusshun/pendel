import json
import io
import unittest
from decimal import Decimal as D
from unittest.mock import patch
from urllib.error import HTTPError
from pathlib import Path

from pendle.market import (Book, HttpClient, MarketError, RateLimited,
                           parse_lighter, parse_ondo, resolve_lighter, validate_ondo_contract)

FIXTURES = Path(__file__).parent / 'fixtures'


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


class MarketTests(unittest.TestCase):
    def test_real_ondo_fixture_round_trip(self):
        book = parse_ondo(fixture('ondo_depth.json'), 'INTC-USD.P', 2000000000000)
        self.assertLess(book.bids[0][0], book.asks[0][0])
        self.assertIsInstance(book.bids[0][0], D)
        self.assertIsNotNone(book.source_ms)
        self.assertEqual(Book.from_dict(book.to_dict()), book)

    def test_real_lighter_fixture_aggregates_non_cumulative_orders(self):
        raw = fixture('lighter_orders.json')
        book = parse_lighter(raw, 'INTC', 1000)
        price = D(raw['asks'][0]['price'])
        expected = sum((D(row['remaining_base_amount']) for row in raw['asks']
                        if D(row['price']) == price), D(0))
        self.assertEqual(dict(book.asks)[price], expected)
        self.assertIsNone(book.source_ms)

    def test_invalid_books_and_api_failures_are_rejected(self):
        bads = [({'success': False, 'error': 'failed'}, 'ondo'),
                ({'code': 400}, 'lighter'),
                ({'success': True, 'result': {'market': 'BTC-USD.P', 'bids': [], 'asks': []}}, 'ondo')]
        for raw, venue in bads:
            with self.subTest(venue=venue), self.assertRaises(MarketError):
                (parse_ondo(raw, 'INTC-USD.P', 1000) if venue == 'ondo'
                 else parse_lighter(raw, 'INTC', 1000))
        for bids, asks in (([], [('101', '1')]), ([('102', '1')], [('101', '1')]),
                           ([('99', 'NaN')], [('101', '1')]),
                           ([('99', '-1')], [('101', '1')])):
            with self.subTest(bids=bids), self.assertRaises(MarketError):
                Book.make('ondo', 'INTC', 1000, None, bids, asks)

    def test_market_mapping_and_linear_contract_validation(self):
        meta = resolve_lighter(fixture('lighter_markets.json'), 'INTC')
        self.assertIsInstance(meta['market_id'], int)
        self.assertEqual(D(meta['multiplier']), D(1))
        raw = fixture('lighter_markets.json')
        raw['order_books'] *= 2
        with self.assertRaises(MarketError):
            resolve_lighter(raw, 'INTC')
        raw['order_books'] = raw['order_books'][:1]
        raw['order_books'][0]['multiplier'] = '2'
        with self.assertRaises(MarketError):
            resolve_lighter(raw, 'INTC')
        self.assertEqual(validate_ondo_contract(fixture('ondo_contracts.json'), 'INTC-USD.P')['baseCurrency'], 'INTC')
        raw = fixture('ondo_contracts.json')
        raw['result'][0]['contractType'] = 'inverse'
        with self.assertRaises(MarketError):
            validate_ondo_contract(raw, 'INTC-USD.P')


class HttpTests(unittest.TestCase):
    def test_get_headers_json_and_rate_limit_without_retries(self):
        seen = []

        def urlopen(request, *, timeout):
            seen.append((request.get_method(), request.get_header('User-agent')))
            self.assertEqual(request.get_header('Accept'), 'application/json')
            self.assertEqual(timeout, 1)
            if request.full_url.endswith('/limited'):
                raise HTTPError(request.full_url, 429, '限流',
                                {'Retry-After': '120'}, io.BytesIO())
            return io.BytesIO(b'{"code":200}')

        with patch('pendle.market.urllib.request.urlopen', side_effect=urlopen) as transport:
            client = HttpClient(timeout_s=1)
            base = 'https://market.example.invalid'
            self.assertEqual(client.get_json(base + '/ok'), {'code': 200})
            with self.assertRaises(RateLimited) as caught:
                client.get_json(base + '/limited')
            self.assertEqual(caught.exception.retry_after_s, 60)
            self.assertEqual(len(seen), 2)
            self.assertEqual(seen[0], ('GET', 'pendle-research/0.1'))
            self.assertEqual(transport.call_count, 2)
