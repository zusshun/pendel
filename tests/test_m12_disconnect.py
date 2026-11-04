"""M12: disconnect cancellation precedes potentially blocking emergency alerts, using in-memory substitutes throughout."""

import asyncio
import logging
import unittest

from pendle.maker import Maker
from pendle.maker_config import MakerSettings
from pendle.maker_reconnect import ReconnectBackoff, reconnect_failures
from test_maker_e2e import Alerts, Clock, Exchange, Files


class BlockingUrgentAlerts(Alerts):
    """Block the emergency alert so the test can check whether orders were already cancelled while it is pending."""

    def __init__(self, events):
        super().__init__()
        self.events = events
        self.entered, self.release = asyncio.Event(), asyncio.Event()

    async def alert(self, reason, **kwargs):
        if kwargs.get('urgent'):
            self.events.append('紧急告警')
            self.entered.set()
            await self.release.wait()
        return await super().alert(reason, **kwargs)


class M12DisconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_urgent_disconnect_alert_waits_until_guard_has_cancelled_orders(self):
        for mode in ('sandbox', 'live'):
            for private in (False, True):
                with self.subTest(mode=mode, private=private):
                    clock, files = Clock(), Files()
                    exchange = Exchange(clock, files)
                    exchange.rows['existing'] = {'status': 'OPEN'}
                    alerts = BlockingUrgentAlerts(files.events)
                    logger = logging.Logger('m12-disconnect')
                    logger.addHandler(logging.NullHandler())
                    bot = Maker(MakerSettings(mode=mode,
                        guards_calibrated=mode == 'live',
                        sandbox_verified=mode == 'live',
                        healthcheck_waived=mode == 'live'),
                        api=exchange, alerter=alerts, files=files,
                        monotonic=clock, clock=clock.wall, sleep=clock.sleep,
                        logger=logger)
                    block = bot.guard.block

                    def record_block():
                        files.events.append('守卫屏障')
                        block()

                    async def connect(url, **kwargs):
                        raise OSError('测试断线')

                    bot.guard.block, bot.connect = record_block, connect
                    source = '私有' if private else '公开'
                    reconnect_failures(bot).backoffs[source] = ReconnectBackoff(failures=4)
                    task = asyncio.create_task(bot.feed(private))
                    try:
                        await asyncio.wait_for(alerts.entered.wait(), 1)
                        self.assertFalse(task.done())
                        self.assertEqual(files.events, ['守卫屏障', '撤单', '紧急告警'])
                        self.assertTrue(bot.state.stopping)
                        self.assertEqual(bot.guard.state, 'COOLDOWN')
                        self.assertTrue(bot.manager._stop_task.done())
                        self.assertEqual(await exchange.get_open_orders(bot.settings.market), [])
                    finally:
                        bot.request_stop()
                        alerts.release.set()
                        await asyncio.wait_for(task, 1)


if __name__ == '__main__':
    unittest.main()
