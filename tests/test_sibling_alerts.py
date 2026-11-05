"""Market identification for every Telegram message in sibling mode."""

import asyncio
import logging
import unittest

from pendle.alerts import Alerter, TelegramCredentials
from pendle.maker import Maker
from pendle.maker_config import MakerSettings
from pendle.maker_transport import build_maker
from pendle.ondo_api import HttpResponse, SendGate
from test_maker_e2e import Files


class SiblingAlertTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = 0.0
        self.sent = []

        async def http(method, url, *, json=None, timeout=10):
            self.sent.append((method, json))
            return HttpResponse(200, {'ok': True})

        self.alerter = Alerter(TelegramCredentials('dummy-token', 'dummy-chat'),
            http=http, monotonic=lambda: self.now, summary_interval_s=10)
        self.alerter.market = 'IBM-USD.P'

    async def drain(self):
        await asyncio.gather(*self.alerter._background_tasks)

    def messages(self):
        return [payload['text'] for method, payload in self.sent if method == 'POST']

    async def test_market_can_be_configured_at_construction(self):
        alerter = Alerter(TelegramCredentials('dummy-token', 'dummy-chat'),
                          http=self.alerter._http, market='RDDT-USD.P')
        self.assertTrue(await alerter.self_test())
        self.assertEqual(self.messages(), ['[RDDT-USD.P] 实盘做市告警通道自检'])

    async def test_self_test_ordinary_and_urgent_messages_include_market(self):
        self.assertTrue(await self.alerter.self_test())
        self.assertTrue(await self.alerter.alert('普通守卫'))
        await self.drain()
        self.assertTrue(await self.alerter.alert('对端心跳过期', urgent=True))
        self.assertEqual(self.messages(), [
            '[IBM-USD.P] 实盘做市告警通道自检',
            '[IBM-USD.P] 做市告警：\n普通守卫',
            '[IBM-USD.P] 紧急告警：对端心跳过期'])

    async def test_periodic_summary_includes_market(self):
        await self.alerter.observe(healthy=True, waiting=True, qualified=True,
                                   last_quote=None)
        self.now = 10
        await self.alerter.observe(healthy=True, waiting=True, qualified=True,
                                   last_quote=None)
        await self.drain()
        self.assertEqual(len(self.messages()), 1)
        self.assertTrue(self.messages()[0].startswith('[IBM-USD.P] 做市摘要：'))

    async def test_market_prefix_fits_telegram_limit_and_preserves_redaction(self):
        await self.alerter.alert('dummy-token ' + '长' * 5000, urgent=True)
        message = self.messages()[0]
        self.assertTrue(message.startswith('[IBM-USD.P] 紧急告警：*** '))
        self.assertEqual(len(message), 4096)
        self.assertNotIn('dummy-token', message)

    async def test_empty_market_keeps_original_messages_byte_for_byte(self):
        self.alerter.market = ''
        self.assertTrue(await self.alerter.self_test())
        self.assertTrue(await self.alerter.alert('普通守卫'))
        await self.drain()
        self.assertTrue(await self.alerter.alert('紧急熔断', urgent=True))
        self.assertEqual(self.messages(), [
            '实盘做市告警通道自检', '做市告警：\n普通守卫', '紧急告警：紧急熔断'])

    async def test_build_maker_adds_own_market_prefix_to_telegram(self):
        for market in ('IBM-USD.P', 'RDDT-USD.P'):
            with self.subTest(market=market):
                bot = build_maker(MakerSettings(market=market),
                    env={'TELEGRAM_BOT_TOKEN': 'dummy-token',
                         'TELEGRAM_CHAT_ID': 'dummy-chat'},
                    http=self.alerter._http, logger=logging.Logger('sibling-prefix'))
                self.assertTrue(await bot.alerter.self_test())
                self.assertEqual(self.messages()[-1], f'[{market}] 实盘做市告警通道自检')

    async def test_slow_urgent_telegram_does_not_delay_peer_cancel_and_verification(self):
        urgent_started, release_urgent, verified = (
            asyncio.Event(), asyncio.Event(), asyncio.Event())
        calls = []

        async def http(method, url, *, json=None, timeout=10):
            self.assertTrue(json['text'].startswith('[IBM-USD.P] 紧急告警：'))
            urgent_started.set()
            await release_urgent.wait()
            return HttpResponse(200, {'ok': True})

        class API:
            gate = SendGate()

            async def cancel_all(self, market, *, stop=False):
                calls.append(('cancel', market, stop))

            async def get_open_orders(self, market, *, stop=False):
                calls.append(('verify', market, stop))
                verified.set()
                return []

        alerter = Alerter(TelegramCredentials('dummy-token', 'dummy-chat'), http=http)
        settings = MakerSettings(mode='sandbox', sibling_markets=['RDDT-USD.P'],
            peer_heartbeat_paths={'RDDT-USD.P': 'missing-peer-heartbeat.json'})
        bot = Maker(settings, api=API(), alerter=alerter, files=Files())
        check = asyncio.create_task(bot.check_peer_once('RDDT-USD.P'))
        try:
            await asyncio.wait_for(urgent_started.wait(), .1)
            try:
                await asyncio.wait_for(verified.wait(), .1)
            except TimeoutError:
                self.fail('紧急 Telegram HTTP 阻塞了对端代撤核零')
            self.assertEqual(calls, [
                ('cancel', 'RDDT-USD.P', True), ('verify', 'RDDT-USD.P', True)])
        finally:
            release_urgent.set()
            await check
