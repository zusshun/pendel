"""M13 真实样本回放；删去近档的派生样本仅用于模拟宽价差与单侧市场。"""

import copy
import json
import logging
import unittest
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import patch

from pendle.maker_config import MakerSettings
from pendle.maker_transport import build_maker
from pendle.market import timestamp_ms
from pendle.ondo_api import HttpResponse
from test_maker_e2e import Alerts, Clock, Files


SAMPLES = Path(__file__).parent / 'fixtures' / 'live_samples'


def sample(name):
    return json.loads((SAMPLES / (name+'.json')).read_text(encoding='utf-8'))


class CenterLiveReplayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock, self.alerts, self.files = Clock(), Alerts(), Files()
        self.clock.epoch = timestamp_ms(sample('ws_depthBooksPerps')['timestamp'])/1000
        self.requests = []
        self.settings = replace(MakerSettings(), window_start_day=2,
            window_start_time='00:00', window_end_day=3, window_end_time='23:59')

        async def http(method, url, **kwargs):
            self.requests.append((method, url))
            self.assertEqual(method, 'GET', '回放只允许内存中的只读元数据请求')
            self.assertTrue(url.endswith('/v1/markets'))
            return HttpResponse(200, sample('v1_markets_ibm'))

        self.bot = build_maker(self.settings, env={}, http=http, alerter=self.alerts,
            files=self.files, logger=logging.getLogger(self.id()),
            monotonic=self.clock, clock=self.clock.wall, sleep=self.clock.sleep)
        socket_patch = patch('socket.socket.connect', side_effect=AssertionError('回放禁止联网'))
        socket_patch.start()
        self.addCleanup(socket_patch.stop)

    async def asyncSetUp(self):
        await self.bot.load_rules()
        for message in sample('ws_control'):
            if message['type'] == 'subscribed':
                await self.bot.on_public(json.dumps(message))

    async def asyncTearDown(self):
        await self.bot.shutdown()
        self.assertEqual(self.requests, [('GET', self.settings.rest_url+'/v1/markets')])
        self.assertFalse(self.bot.state.orders)

    @staticmethod
    def derived_depth(*, empty_side=None):
        """派生样本：只删除真实样本两侧最靠近 mark 的两档，价格与量不作人工替换。"""
        message = copy.deepcopy(sample('ws_depthBooksPerps'))
        row = message['data'][0]
        row['bids'] = row['bids'][2:]
        row['asks'] = row['asks'][2:]
        if empty_side is not None:
            row[empty_side] = []
        return message

    async def replay(self, depth, *, discovery=False):
        try:
            await self.bot.on_public(json.dumps(depth))
        except ValueError as exc:
            self.fail('报价回放必须接受单侧有效的公开深度：'+str(exc))
        message = sample('ws_markPricesPerps')
        message['data'][0]['inPriceDiscovery'] = discovery
        await self.bot.on_public(json.dumps(message))
        await self.bot.tick()
        self.assertIsNotNone(self.bot.last_decision)
        return self.bot.last_decision

    def assert_mark_quotes(self, decision):
        self.assertTrue(decision.eligible, decision.reason)
        self.assertEqual(decision.center, D('247.81'))
        self.assertEqual(getattr(decision, 'center_source', None), 'mark')
        self.assertEqual(decision.competition, 0)
        self.assertEqual({quote.side for quote in decision.quotes}, {'buy', 'sell'})
        for quote in decision.quotes:
            distance = abs(quote.price/D('247.81')-1)*10000
            self.assertGreaterEqual(distance, self.settings.min_offset_bps)
            self.assertLessEqual(distance, self.settings.band_bps-self.settings.band_margin_bps)

    async def test_original_live_depth_uses_third_party_mid_and_keeps_competition_gate(self):
        decision = await self.replay(sample('ws_depthBooksPerps'))
        self.assertEqual(decision.center, D('247.82'))
        self.assertEqual(getattr(decision, 'center_source', None), 'third_party_mid')
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.reason, '竞争量超过判据')
        self.assertGreater(decision.competition, self.settings.gate_comp_max_usd)
        self.assertEqual(self.bot.rules.tick, D('.01'))
        self.assertEqual(self.bot.rules.increment, D('.01'))

    async def test_derived_live_wide_spread_low_competition_can_quote_around_mark(self):
        depth = self.derived_depth()
        self.assertEqual(depth['data'][0]['bids'][0][0], '246.76')
        self.assertEqual(depth['data'][0]['asks'][0][0], '249')
        decision = await self.replay(depth)
        self.assert_mark_quotes(decision)
        self.assertEqual(self.bot.engine.anchor, D('247.81'))
        self.assertTrue(self.bot.qualified)

    async def test_derived_live_empty_bids_and_real_far_asks_use_mark(self):
        decision = await self.replay(self.derived_depth(empty_side='bids'))
        self.assertEqual(self.bot.book.bids, ())
        self.assertTrue(self.bot.book.asks)
        self.assert_mark_quotes(decision)

    async def test_derived_live_empty_asks_and_real_far_bids_use_mark(self):
        decision = await self.replay(self.derived_depth(empty_side='asks'))
        self.assertEqual(self.bot.book.asks, ())
        self.assertTrue(self.bot.book.bids)
        self.assert_mark_quotes(decision)

    async def test_live_mark_price_discovery_stops_both_sides_for_wide_spread(self):
        decision = await self.replay(self.derived_depth(), discovery=True)
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.quotes, ())
        self.assertIn('价格发现', decision.reason)
        self.assertFalse(self.bot.qualified)
        self.assertEqual(self.bot.engine.oracle[0], D('248.1146357291905471'))


if __name__ == '__main__':
    unittest.main()
