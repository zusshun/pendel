"""M29 regressions for low-qualification log throttling."""

import asyncio
import logging
import unittest

from pendle.alerts import Alerter, TelegramCredentials
from pendle.ondo_api import HttpResponse


class LowQualificationLogRateLimitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = 0.0
        self.logger = logging.Logger('m29-low-qualification')
        self.alerter = Alerter(monotonic=lambda: self.now, logger=self.logger)

    async def observe(self, now):
        self.now = now
        await self.alerter.observe(healthy=False, waiting=True, qualified=False,
                                   last_quote=None)
        await asyncio.gather(*self.alerter._background_tasks)

    async def test_low_qualification_info_log_is_limited_to_once_per_six_hundred_seconds(self):
        await self.observe(0)
        with self.assertLogs(self.logger, logging.INFO) as records:
            for now in (1800, 1801, 1802, 2399.999):
                await self.observe(now)
        self.assertEqual(len(records.records), 1)
        self.assertEqual(records.records[0].getMessage(),
                         '最近 30 分钟有资格挂单时长低于 50%')
        self.assertEqual(records.records[0].levelno, logging.INFO)
        with self.assertLogs(self.logger, logging.INFO) as records:
            await self.observe(2400)
        self.assertEqual(len(records.records), 1)
        with self.assertNoLogs(self.logger, logging.INFO):
            await self.observe(2401)
        self.assertEqual(self.alerter._summary_counts['low-qualification'], 6)
        self.assertEqual(self.alerter._pending, {})

    async def test_low_qualification_summary_counts_suppressed_observations(self):
        sent = []

        async def http(method, url, *, json=None, timeout=10):
            sent.append(json['text'])
            return HttpResponse(200, {'ok': True})

        self.alerter = Alerter(TelegramCredentials('private-token', 'private-chat'),
                               http=http, monotonic=lambda: self.now,
                               logger=self.logger, summary_interval_s=2401)
        await self.observe(0)
        with self.assertLogs(self.logger, logging.INFO) as records:
            for now in (1800, 1801, 2400, 2401):
                await self.observe(now)
        self.assertEqual(len(records.records), 2)
        self.assertEqual(len(sent), 1)
        self.assertIn('低资格观测：4次', sent[0])
        with self.assertNoLogs(self.logger, logging.INFO):
            await self.observe(2402)
        self.assertEqual(self.alerter._summary_counts['low-qualification'], 1)
