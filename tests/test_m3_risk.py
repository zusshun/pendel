"""M3：发送时对两侧最终持仓端点执行硬上限复核。"""

import unittest
from decimal import Decimal as D

from pendle.maker_risk import (ACTIVE_STATES, MarketRisk, RiskLimits, RiskManager,
                               RiskOrder, RiskRejected)


class HardCapEndpointTests(unittest.TestCase):
    def setUp(self):
        self.risk = RiskManager(RiskLimits(quote_notional=D('800')))

    def check(self, position, side, quantity, *, orders=(), price='100', reduce_only=False):
        return self.risk.check_order('IBM', side, D(quantity), D(price),
            [MarketRisk('IBM', D(position), D('100'))], orders, D('1000'), D('1000'),
            reduce_only=reduce_only)

    def test_eight_hundred_dollar_opposite_quote_cannot_flip_beyond_hard_cap(self):
        for position, side in (('1', 'sell'), ('-1', 'buy')):
            with self.subTest(position=position, side=side), self.assertRaises(RiskRejected):
                self.check(position, side, '7.991')

    def test_every_nonterminal_same_side_order_reserves_final_endpoint(self):
        for position, side in (('1', 'sell'), ('-1', 'buy')):
            for state in ACTIVE_STATES:
                orders = [RiskOrder('IBM', side, D('2'), D('100'), state)]
                with self.subTest(position=position, state=state):
                    self.assertEqual(self.check(position, side, '5', orders=orders)
                                     .exposures['IBM'], D('600'))
                    with self.assertRaises(RiskRejected):
                        self.check(position, side, '5.001', orders=orders)

    def test_final_endpoint_uses_higher_proposed_price_conservatively(self):
        for position, side in (('1', 'sell'), ('-1', 'buy')):
            with self.subTest(position=position):
                self.assertEqual(self.check(position, side, '3.5', price='200')
                                 .exposures['IBM'], D('600'))
                with self.assertRaises(RiskRejected):
                    self.check(position, side, '3.501', price='200')

    def test_true_reduce_only_remains_available_above_hard_cap(self):
        for position, side in (('8', 'sell'), ('-8', 'buy')):
            with self.subTest(position=position):
                self.check(position, side, '2', reduce_only=True)
                with self.assertRaises(RiskRejected):
                    self.check(position, side, '2')

