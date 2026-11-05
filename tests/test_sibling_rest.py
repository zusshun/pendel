"""REST market isolation excludes declared peers only after explicit opt-in."""

import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from pendle import maker_transport
from pendle.maker_config import MakerSettings
from pendle.ondo_api import (Credentials, HttpResponse, OndoAPI,
                             ResponseFormatError)
from test_maker_e2e import Clock, Files


MARKET = 'IBM-USD.P'
PEER = 'RDDT-USD.P'
OTHER = 'TSLA-USD.P'
CREDENTIALS = Credentials('dummy-key', 'dummy-secret')


def fill(market, identifier='fill'):
    return {'market': market, 'id': identifier, 'orderId': 'order',
            'clientOrderId': 'client', 'price': '100', 'size': '1',
            'side': 'buy', 'fee': '0', 'pnl': '0', 'time': 1667472000000,
            'isMaker': True}


class SiblingRestTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock, self.sent, self.replies = Clock(), [], []

    def client(self, siblings=()):
        async def transport(request):
            self.sent.append(request)
            return self.replies.pop(0)

        return OndoAPI(CREDENTIALS, transport=transport, sibling_markets=siblings,
                       clock=self.clock.wall, monotonic=self.clock, sleep=self.clock.sleep)

    def reply(self, rows, **extras):
        self.replies.append(HttpResponse(200, {'success': True, 'result': rows, **extras}))

    async def test_sibling_open_orders_are_ignored_without_importing_peer_state(self):
        own = {'market': MARKET, 'orderId': 'own'}
        self.reply([{'market': PEER}, own])
        self.assertEqual(await self.client((PEER,)).get_open_orders(MARKET), [own])

    async def test_sibling_fills_are_ignored_before_own_fill_validation(self):
        self.reply([{'market': PEER}, fill(MARKET, 'own-fill')])
        rows = await self.client((PEER,)).get_fills(MARKET)
        self.assertEqual([row['fill_id'] for row in rows], ['own-fill'])
        self.assertEqual([row['market'] for row in rows], [MARKET])

    async def test_filtering_peer_rows_does_not_truncate_pagination(self):
        self.reply([{'market': PEER}], pageInfo={'nextCursor': 'next-page'})
        own = {'market': MARKET, 'orderId': 'own'}
        self.reply([own], pageInfo={'nextCursor': None})
        self.assertEqual(await self.client((PEER,)).get_open_orders(MARKET), [own])
        self.assertEqual(len(self.sent), 2)
        self.assertIn('cursor=next-page', self.sent[1].path_query)

    async def test_peer_guard_query_keeps_requested_peer_orders_for_zero_verification(self):
        peer = {'market': PEER, 'orderId': 'peer'}
        self.reply([peer])
        self.assertEqual(await self.client((PEER,)).get_open_orders(PEER, stop=True), [peer])

    async def test_non_sibling_other_market_orders_and_fills_still_reject(self):
        for operation, row in (('get_open_orders', {'market': OTHER, 'orderId': 'other'}),
                               ('get_fills', fill(OTHER))):
            with self.subTest(operation=operation):
                self.reply([row])
                with self.assertRaises(ResponseFormatError):
                    await getattr(self.client((PEER,)), operation)(MARKET)

    async def test_empty_sibling_configuration_preserves_other_market_rejection(self):
        for operation, row in (('get_open_orders', {'market': PEER, 'orderId': 'peer'}),
                               ('get_fills', fill(PEER))):
            with self.subTest(operation=operation):
                self.reply([row])
                with self.assertRaises(ResponseFormatError):
                    await getattr(self.client(), operation)(MARKET)

    async def test_sibling_filter_keeps_own_invalid_rows_rejected(self):
        for operation in ('get_open_orders', 'get_fills'):
            with self.subTest(operation=operation):
                self.reply([{'market': PEER}, {'market': MARKET}])
                with self.assertRaises(ResponseFormatError):
                    await getattr(self.client((PEER,)), operation)(MARKET)

    async def test_positions_preserve_all_non_siblings_for_other_market_detection(self):
        rows = [{'market': market, 'quantity': '1'} for market in (MARKET, PEER, OTHER)]
        self.reply(rows)
        result = await self.client((PEER,)).get_positions()
        self.assertEqual([row['market'] for row in result], [MARKET, OTHER])

    async def test_malformed_peer_positions_do_not_reject_own_position_snapshot(self):
        self.reply([{'market': PEER, 'netQuantity': 'invalid'},
                    {'market': MARKET, 'quantity': '2'}])
        rows = await self.client((PEER,)).get_positions()
        self.assertEqual([row['market'] for row in rows], [MARKET])
        self.assertEqual(rows[0]['quantity'], 2)

    async def test_api_builder_passes_sibling_filter_from_instance_configuration(self):
        settings = MakerSettings(mode='sandbox', sibling_markets=[PEER],
                                 peer_heartbeat_paths={PEER: 'peer.json'})

        async def transport(request):
            self.sent.append(request)
            return HttpResponse(200, {'success': True, 'result': [{'market': PEER}]})

        api = maker_transport._build_api(settings, {'credentials': CREDENTIALS,
            'transport': transport, 'clock': self.clock.wall,
            'monotonic': self.clock, 'sleep': self.clock.sleep})
        self.assertEqual(await api.get_open_orders(MARKET), [])
        self.assertEqual(api.sibling_markets, (PEER,))


class SiblingEmergencyRestTests(unittest.TestCase):
    def test_cancel_all_uses_each_instance_market_and_configuration_without_ws(self):
        for market, peer, directory in ((MARKET, PEER, 'data/maker'),
                                        (PEER, MARKET, 'data/maker-rddt')):
            with self.subTest(market=market):
                clock, files, requests, locks = Clock(), Files(), [], []
                files.acquire_instance = lambda path: locks.append(path) or 'lock'
                settings = MakerSettings(mode='sandbox', market=market, data_dir=directory,
                    sibling_markets=[peer], peer_heartbeat_paths={peer: 'peer.json'})

                async def transport(request):
                    requests.append(request)
                    rows = [{'market': peer}] if request.method == 'GET' else None
                    return HttpResponse(200, {'success': True, 'result': rows})

                result = maker_transport.run_cancel_all(settings, credentials=CREDENTIALS,
                    transport=transport, files=files, clock=clock.wall,
                    monotonic=clock, sleep=clock.sleep)
                self.assertEqual(result, 0)
                self.assertEqual(locks, [Path(directory)/'default.lock'])
                self.assertEqual([row.method for row in requests], ['DELETE', 'GET'])
                for request in requests:
                    query = parse_qs(urlsplit(request.path_query).query)
                    self.assertEqual(query['market'], [market])
                self.assertFalse(any('timeout_seconds' in request.path_query for request in requests))


if __name__ == '__main__':
    unittest.main()
