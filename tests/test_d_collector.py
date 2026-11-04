"""Final batch D collector regressions with offline substitutes controlling wall-clock changes and public endpoints."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pendle.bt_config import CollectSettings
from pendle.collector import Collector, HourlyWriter
from tests.test_collector import Clock, FakeWS, depth, message, trade


FIXTURES = Path(__file__).parent / 'fixtures'


class FinalCollectorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.wall, self.mono = Clock(1000), Clock()

    def service(self, **kwargs):
        values = dict(data_dir=self.tmp.name, log_dir=self.tmp.name + '/log',
                      focus_symbols=('INTC',), request_interval_ms=0)
        values.update(kwargs.pop('settings', {}))
        service = Collector(CollectSettings(**values), clock=self.wall,
                            monotonic=self.mono, **kwargs)
        return service

    @staticmethod
    def drain(service):
        rows = []
        while not service.queue.empty():
            rows.append(service.queue.get_nowait())
            service.queue.task_done()
        return rows

    async def test_lighter_unsupported_symbols_are_cached_until_refresh_and_warn_once(self):
        urls, waits = [], []
        class HTTP:
            def get_json(self, url):
                urls.append(url)
                filename = 'lighter_markets.json' if url.endswith('/orderBooks') else 'lighter_orders.json'
                return json.loads((FIXTURES / filename).read_text())
        async def sleep(delay):
            waits.append(delay)
            self.mono.now = (2, 3599, 3600, 3602)[len(waits) - 1]
            if len(waits) == 4:
                service.request_stop()
        service = self.service(client=HTTP(), sleep=sleep,
            settings={'focus_symbols': ('INTC', 'NKE', 'RDDT', 'DKNG')})
        await service.lighter_loop()
        self.assertEqual(len([url for url in urls if url.endswith('/orderBooks')]), 2)
        self.assertEqual(len([url for url in urls if '/orderBookOrders?' in url]), 4)
        self.assertFalse(any(row['kind'] == 'error' for row in self.drain(service)))
        logs = [json.loads(line) for path in (Path(self.tmp.name) / 'log').glob('*.log')
                for line in path.read_text().splitlines()]
        warnings = [row for row in logs if row.get('级别') == 'WARNING']
        self.assertEqual(len(warnings), 3)
        for symbol in ('NKE', 'RDDT', 'DKNG'):
            self.assertTrue(any(symbol in row['警告'] and '不支持' in row['警告'] for row in warnings))
            self.assertNotIn(('lighter', symbol, 'orderBookOrders'), service.expected)

    async def test_contract_refresh_also_collects_market_precision(self):
        urls = []
        markets = {'success': True, 'result': {'perps': {'tradingPairs': [
            {'market': 'INTC-USD.P', 'quoteIncrement': '0.001', 'baseIncrement': '0.01'}]}}}
        class HTTP:
            def get_json(self, url):
                urls.append(url)
                return (markets if url.endswith('/markets') else
                        json.loads((FIXTURES / 'ondo_contracts.json').read_text()))
        service = self.service(client=HTTP())
        await service.contracts_once()
        self.assertIn('https://api.ondoperps.xyz/v1/markets', urls)
        rows = [row for row in self.drain(service) if row['kind'] == 'markets']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['payload'], markets)
        self.assertIsNone(rows[0]['market'])

    async def test_invalid_market_metadata_records_error_without_stopping_contracts(self):
        class HTTP:
            def get_json(self, url):
                return ({'success': True, 'result': []} if url.endswith('/markets') else
                        json.loads((FIXTURES / 'ondo_contracts.json').read_text()))
        service = self.service(client=HTTP())
        try:
            await service.contracts_once()
        except Exception as exc:
            self.fail(f'市场精度元数据结构错误不应终止采集：{exc}')
        rows = self.drain(service)
        self.assertEqual(len([row for row in rows if row['kind'] == 'contracts']), 1)
        errors = [row for row in rows if row['kind'] == 'error']
        self.assertEqual(errors[0]['payload']['channel'], 'markets')
        self.assertIn('无效', errors[0]['payload']['error'])

    async def test_sampling_uses_monotonic_time_while_recording_wall_time(self):
        service = self.service(settings={'other_snapshot_s': 10})
        service.tradable_markets = {'INTC-USD.P'}
        for mono, wall in ((0, 1000), (.5, 10000), (1, 900), (10, 899)):
            self.mono.now, self.wall.now = mono, wall
            await service.handle_ondo(message('depthBooksPerps', [depth()]))
        rows = self.drain(service)
        self.assertEqual([row['received_ms'] for row in rows if row['kind'] == 'depth'],
                         [1000000, 900000, 899000])
        self.assertEqual([row['received_ms'] for row in rows if row['kind'] == 'depth_observation'],
                         [1000000, 899000])

    async def test_silence_and_health_ages_use_monotonic_elapsed_time(self):
        service = self.service(settings={'silence_timeout_s': 5, 'market_stale_s': 5,
                                         'source_silence_warn_s': 5})
        await service.handle_ondo(message('tradesPerps', [trade()]))
        service.connected_since['ondo'] = service.now_ms()
        self.wall.now, self.mono.now = -1000, 6
        await service.check_silence()
        self.assertIn(('ondo', '*', '*'), service.gaps)
        stale = [row for row in self.drain(service) if row['kind'] == 'market_stale'
                 and row['market'] == 'INTC-USD.P' and row['payload']['channel'] == 'tradesPerps']
        self.assertEqual(stale[0]['payload']['stale_ms'], 6000)
        await service.health_once()
        rows = [json.loads(line) for path in (Path(self.tmp.name) / 'log').glob('*.log')
                for line in path.read_text().splitlines()]
        health = next(row for row in rows if '来源' in row)
        self.assertEqual(health['来源']['ondo']['最近消息年龄秒'], 6)
        self.assertEqual(len([row for row in rows if row.get('级别') == 'WARNING']), 1)

    async def test_http_throttling_ignores_wall_clock_jumps(self):
        waits = []
        class HTTP:
            def get_json(self, url):
                return {}
        async def sleep(delay):
            waits.append(delay)
            self.mono.now += delay
        service = self.service(client=HTTP(), sleep=sleep,
                               settings={'request_interval_ms': 250})
        await service.http('first')
        self.wall.now, self.mono.now = 10000, .1
        await service.http('second')
        self.assertEqual(waits, [.15])

    async def test_binance_rotation_uses_monotonic_time(self):
        service = self.service(settings={'binance_reconnect_s': 2})
        self.mono.now = 3
        self.wall.now = -1000
        with self.assertRaises(Exception) as caught:
            await service._receive('binance', FakeWS([]), 0)
        self.assertIsInstance(caught.exception, TimeoutError)
        self.assertIn('轮换', str(caught.exception))

    async def test_low_free_space_stops_with_75_before_connecting(self):
        service = self.service()
        service.monotonic = self.mono
        async def stopped(*args):
            service.request_stop()
        service.ws_loop = stopped
        service.contracts_loop = service.rest_loop = service.lighter_loop = service.monitor_loop = stopped
        free = SimpleNamespace(total=10 * 1024 ** 3, used=9 * 1024 ** 3, free=1024 ** 3)
        with patch('shutil.disk_usage', return_value=free):
            self.assertEqual(await service.run(), 75)


class FinalDiskTests(unittest.TestCase):
    def test_new_limits_have_defaults_and_round_trip(self):
        settings = CollectSettings()
        self.assertEqual(str(getattr(settings, 'lighter_meta_refresh_s', None)), '3600')
        self.assertEqual(str(getattr(settings, 'min_free_gb', None)), '2')
        self.assertEqual(CollectSettings.from_dict(settings.to_dict()), settings)

    def test_free_space_limit_is_checked_after_writing_and_flushes_complete_rows(self):
        import gzip
        with tempfile.TemporaryDirectory() as directory:
            writer = HourlyWriter(directory, 'd', 20)
            free = SimpleNamespace(total=10 * 1024 ** 3, used=9 * 1024 ** 3, free=1024 ** 3)
            with patch('shutil.disk_usage', return_value=free):
                self.assertTrue(writer.write_batch([dict(source='ondo', kind='test', received_ms=0)]))
            writer.close()
            path = next(Path(directory).rglob('*.jsonl.gz'))
            self.assertEqual(json.loads(gzip.decompress(path.read_bytes()))['kind'], 'test')

    def test_usage_accumulates_without_tree_scan_and_recalibrates_after_ten_minutes(self):
        with tempfile.TemporaryDirectory() as directory:
            mono = Clock()
            writer = HourlyWriter(directory, 'd', 20, monotonic=mono)
            with patch.object(writer, 'disk_usage', wraps=writer.disk_usage) as scan:
                for at in (0, 1000, 2000, 3600000):
                    writer.write_batch([dict(source='ondo', kind='test', received_ms=at)])
                actual = sum(path.stat().st_size for path in Path(directory).rglob('*') if path.is_file())
                self.assertEqual(writer.usage, actual)
                self.assertEqual(scan.call_count, 0)
                external = Path(directory) / 'external.dat'
                external.write_bytes(b'x' * 123)
                mono.now = 600
                writer.write_batch([dict(source='ondo', kind='test', received_ms=3600001)])
                actual = sum(path.stat().st_size for path in Path(directory).rglob('*') if path.is_file())
                self.assertEqual(writer.usage, actual)
                self.assertEqual(scan.call_count, 1)
            writer.close()
