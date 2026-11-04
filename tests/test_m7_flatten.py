"""M7 硬上限减仓每轮使用新鲜中心价，复用真实编排器和内存交易所。"""

import unittest
from decimal import Decimal as D

import test_maker_dispatch as dispatch
from test_maker import MARKET


class M7FlattenTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = dispatch.DispatchTests.asyncSetUp
    asyncTearDown = dispatch.DispatchTests.asyncTearDown
    publish = dispatch.DispatchTests.publish

    def iocs(self):
        return [body for kind, *args in self.exchange.calls
                if kind == 'place' and (body := args[0])['timeInForce'] == 'IOC']

    def set_position(self, quantity):
        self.bot.state.positions[MARKET] = self.exchange.position = D(quantity)

    async def assert_retry_uses_new_center(self, position, new_center, expected_price):
        self.set_position(position)

        def update_after_first_ioc():
            self.publish(new_center if self.iocs() else '100')

        self.exchange.on_quote = update_after_first_ioc
        self.assertFalse(await self.bot._flatten(D(100)))
        rows = self.iocs()
        self.assertEqual(len(rows), 3)
        self.assertEqual(D(rows[0]['price']), D('99.40') if D(position) > 0 else D('100.60'))
        self.assertEqual([D(row['price']) for row in rows[1:]], [D(expected_price)] * 2,
                         '重试必须按新中心重新计算 IOC 滑点限价')
        self.assertTrue(all(row['reduceOnly'] and not row['postOnly'] for row in rows))

    async def test_ioc_sell_retry_refreshes_slippage_price(self):
        await self.assert_retry_uses_new_center('7', '90', '89.46')

    async def test_ioc_buy_retry_refreshes_slippage_price(self):
        await self.assert_retry_uses_new_center('-7', '110', '110.66')

    async def test_missing_first_reduction_center_halts_without_stale_price_intent(self):
        self.set_position('7')
        self.exchange.on_quote = None
        self.bot.book_seen = None

        self.assertFalse(await self.bot._flatten(D(100)))

        self.assertEqual(self.iocs(), [], '没有新鲜中心时禁止发送 IOC')
        self.assertTrue(self.bot.gate.trading_halted)
        self.assertIn(str(self.bot.settings.halt_path), self.files.rows)
        self.assertEqual(list(self.bot.state.orders.values()), [],
                         '首轮缺中心应立即熔断，不能先生成旧价意图再交给发送守卫拒绝')

    async def test_missing_retry_reduction_center_halts_without_another_intent(self):
        self.set_position('7')

        def lose_book_after_first_ioc():
            self.publish()
            if self.iocs():
                self.bot.book_seen = None

        self.exchange.on_quote = lose_book_after_first_ioc
        self.assertFalse(await self.bot._flatten(D(100)))

        self.assertEqual(len(self.iocs()), 1, '重试前缺中心时不得再次发单')
        self.assertTrue(self.bot.gate.trading_halted)
        self.assertIn(str(self.bot.settings.halt_path), self.files.rows)
        self.assertEqual(len(self.bot.state.orders), 1,
                         '重试前缺中心应立即停止，不能继续生成旧价意图')
