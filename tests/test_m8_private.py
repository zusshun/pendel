"""M8 private update fault tolerance; field contracts await verification against sandbox samples."""

import asyncio
import unittest
from dataclasses import replace
from decimal import Decimal as D

from pendle.maker import Maker, MarketRules
from pendle.maker_config import MakerSettings
from test_maker import MARKET
from test_maker_e2e import live_deadman_ack, Alerts, Clock, Exchange, Files


class M8PrivateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock, self.files, self.alerts = Clock(), Files(), Alerts()
        self.api = Exchange(self.clock, self.files)
        self.bot = Maker(replace(MakerSettings(), mode='sandbox'), api=self.api,
            alerter=self.alerts, rules=MarketRules(D('.01'), D('.01')), files=self.files,
            monotonic=self.clock, clock=self.clock.wall, sleep=self.clock.sleep)
        self.reset_ready()

    def reset_ready(self):
        self.bot.state.modes[MARKET] = 'RUNNING'
        self.bot.state.deadman_acked = True
        self.bot.reconcile_event.clear()
        self.alerts.messages.clear()

    async def assert_invalid(self, message):
        self.reset_ready()
        try:
            await self.bot.on_private(message)
        except Exception as exc:
            self.fail(f'坏私有推送不应使消费任务退出：{type(exc).__name__}: {exc}')
        self.assertEqual(self.bot.state.market_mode(MARKET), 'SYNCING')
        self.assertTrue(self.bot.reconcile_event.is_set())
        self.assertTrue(any('待沙盒样本' in text for text in self.alerts.messages),
                        self.alerts.messages)

    def order(self):
        return {'market': MARKET, 'clientOrderId': 'm8-order', 'orderId': 'remote-order',
                'side': 'buy', 'size': '1', 'price': '100', 'filledSize': '0',
                'time_ms': int(self.clock.wall()*1000), 'status': 'CANCELED'}

    def fill(self):
        return {'id': 'm8-fill', 'market': MARKET, 'orderId': 'remote-fill',
                'clientOrderId': 'm8-fill-order', 'price': '100', 'size': '.1',
                'side': 'buy', 'fee': '.01', 'pnl': '0',
                'time': int(self.clock.wall()*1000), 'isMaker': True}

    async def test_bad_json_and_nonobject_private_messages_sync_and_alert(self):
        for message in ('{', 'null', '[]', [], None, 1, {}):
            with self.subTest(message=message):
                await self.assert_invalid(message)

    async def test_missing_null_scalar_or_bad_private_data_syncs_and_alerts(self):
        for channel in ('ordersPerps', 'fillsPerps', 'positionsPerps', 'balancePerps'):
            for data in (None, 'bad', 7, [None], [7], [False], [[]]):
                with self.subTest(channel=channel, data=data):
                    await self.assert_invalid({'channel': channel, 'data': data})
            with self.subTest(channel=channel, data='missing'):
                await self.assert_invalid({'channel': channel})

    async def test_missing_private_order_fields_sync_and_alert(self):
        for key in self.order():
            if key == 'clientOrderId':
                continue  # M24: optional; match locally by orderId, or cancel orphaned orders.
            row = self.order()
            del row[key]
            with self.subTest(key=key):
                await self.assert_invalid({'channel': 'ordersPerps', 'data': [row]})

    async def test_missing_private_fill_fields_sync_and_alert(self):
        # clientOrderId is optional: fills of orders placed outside the bot (web app) do not carry it.
        for key in (key for key in self.fill() if key != 'clientOrderId'):
            row = self.fill()
            del row[key]
            with self.subTest(key=key):
                await self.assert_invalid({'channel': 'fillsPerps', 'data': [row]})

    async def test_invalid_private_numeric_and_identity_values_sync_and_alert(self):
        cases = [('ordersPerps', self.order(), 'market', []),
                 ('ordersPerps', self.order(), 'size', 'NaN'),
                 ('ordersPerps', self.order(), 'time_ms', True),
                 ('fillsPerps', self.fill(), 'side', 'up'),
                 ('fillsPerps', self.fill(), 'size', '-1'),
                 ('fillsPerps', self.fill(), 'time', 10**20),
                 ('fillsPerps', self.fill(), 'time', 10**30),
                 ('fillsPerps', self.fill(), 'market', None)]
        for channel, row, key, value in cases:
            row[key] = value
            with self.subTest(channel=channel, key=key, value=value):
                await self.assert_invalid({'channel': channel, 'data': [row]})
        self.assertFalse(self.bot.state.fills)

    async def test_missing_or_invalid_position_fields_sync_and_alert(self):
        for row in ({}, {'market': MARKET}, {'quantity': '0'},
                    {'market': MARKET, 'quantity': 'NaN'},
                    {'market': MARKET, 'direction': 'long'},
                    {'market': MARKET, 'direction': 'up', 'netQuantity': '1'},
                    {'market': MARKET, 'quantity': '0', 'markPrice': 'bad'}):
            with self.subTest(row=row):
                await self.assert_invalid({'channel': 'positionsPerps', 'data': [row]})

    async def test_missing_or_invalid_balance_fields_sync_and_alert(self):
        for row in ({}, {'equity': '100'}, {'available': '100'},
                    {'equity': 'NaN', 'available': '100'},
                    {'marginBalance': '100', 'availableMargin': '100'},
                    {'equity': '100', 'available': '100', 'margin_ratio': 'bad'},
                    {'equity': '100', 'available': '100', 'under_liquidation': 'no'},
                    {'equity': '100', 'available': '100', 'time_ms': None}):
            with self.subTest(row=row):
                await self.assert_invalid({'channel': 'balancePerps', 'data': row})

    async def test_subscribed_control_is_processed_before_data(self):
        await self.bot.on_private({'type': 'subscribed', 'channel': 'fillsPerps', 'data': None})
        self.assertEqual(self.bot.state.market_mode(MARKET), 'RUNNING')
        self.assertFalse(self.bot.reconcile_event.is_set())

    async def test_subscribed_control_without_channel_syncs_and_alerts(self):
        await self.assert_invalid({'type': 'subscribed'})

    async def test_real_deadman_subscribed_control_matches_default_ack(self):
        self.bot.state.deadman_acked = False
        self.bot.private_logged_in = self.bot.deadman_requested = True
        await self.bot.on_private(live_deadman_ack(self.bot.settings.deadman_timeout_s))
        self.assertTrue(self.bot.state.deadman_acked)
        self.assertTrue(self.bot.reconcile_event.is_set())

    async def test_real_deadman_control_does_not_bypass_custom_ack(self):
        self.bot.settings = replace(self.bot.settings,
            deadman_ack_match={'channel': 'cancelAllOrdersAfterPerps', 'ok': True})
        self.bot.state.deadman_acked = False
        self.bot.private_logged_in = self.bot.deadman_requested = True
        await self.bot.on_private(live_deadman_ack(self.bot.settings.deadman_timeout_s))
        self.assertFalse(self.bot.state.deadman_acked)

    async def test_private_subscription_error_blocks_and_alerts(self):
        await self.bot.on_private({'type': 'error', 'channel': 'ordersPerps',
                                   'msg': 'channel unsupported'})
        self.assertFalse(self.bot.state.deadman_acked)
        self.assertEqual(self.bot.state.market_mode(MARKET), 'SYNCING')
        self.assertTrue(self.bot.reconcile_event.is_set())
        self.assertTrue(any('订阅失败' in text and '待沙盒样本' in text
                            for text in self.alerts.messages))

    async def test_single_object_balance_compatibility_is_preserved(self):
        await self.bot.on_private({'channel': 'balancePerps',
                                   'data': {'equity': '100', 'available': '90'}})
        self.assertEqual(self.bot.state.balances['equity'], '100')

    async def test_private_cancellation_still_propagates(self):
        async def cancelled(*args, **kwargs):
            raise asyncio.CancelledError
        self.bot.manager.handle_order_event = cancelled
        with self.assertRaises(asyncio.CancelledError):
            await self.bot.on_private({'channel': 'ordersPerps', 'data': [self.order()]})

    async def test_private_invalid_alert_transport_failure_still_propagates(self):
        async def failed(*args, **kwargs):
            raise OSError('测试告警故障')
        self.bot.alerter.alert = failed
        with self.assertRaisesRegex(OSError, '测试告警故障'):
            await self.bot.on_private({'channel': 'fillsPerps', 'data': [{}]})

    async def test_private_order_transport_failure_still_propagates(self):
        async def failed(*args, **kwargs):
            raise OSError('测试订单故障')
        self.bot.manager.handle_order_event = failed
        with self.assertRaisesRegex(OSError, '测试订单故障'):
            await self.bot.on_private({'channel': 'ordersPerps', 'data': [self.order()]})
