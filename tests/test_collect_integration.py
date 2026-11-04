"""从公开消息的替身贯通真实采集、gzip、CLI 导入与断档回测。"""

import asyncio
import contextlib
import gzip
import io
import json
import signal
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from pendle.bt_config import CollectSettings
from pendle.cli import main
from pendle.collector import Collector
from pendle.history import HistoryStore, parse_time_ns


BASE = parse_time_ns('2026-11-04T00:00:00Z') // 1000000
FIXTURES = Path(__file__).parent / 'fixtures'


def iso(at):
    return datetime.fromtimestamp(at / 1000, timezone.utc).isoformat()


def ondo(channel, second):
    row = {'market': 'INTC-USD.P', 'time': iso(BASE + second * 1000)}
    if channel == 'depthBooksPerps':
        row.update(bids=[['99.99', '2'], ['99', '3']],
                   asks=[['100.01', '4'], ['101', '5']])
    else:
        row.update(id=str(second), price='100', size='2', cost='200', aggressor_side='buy')
    return json.dumps({'type': 'update', 'channel': channel, 'data': [row]})


def binance(second):
    return json.dumps({'stream': 'intcusdt@aggTrade', 'data': {
        'e': 'aggTrade', 'a': second, 'p': '100', 'q': '2', 'f': second,
        'l': second, 'T': BASE + second * 1000, 'm': False}})


class FakeClock:
    def __init__(self):
        self.now = BASE / 1000

    def __call__(self):
        return self.now


class FakeHTTP:
    def __init__(self):
        self.urls = []

    def get_json(self, url):
        self.urls.append(url)
        if url.endswith('/markets'):
            return {'success': True, 'result': {'perps': {'tradingPairs': [
                {'market': 'INTC-USD.P', 'quoteIncrement': '0.01', 'baseIncrement': '0.1'}]}}}
        filename = ('ondo_contracts.json' if url.endswith('/contracts') else
                    'lighter_markets.json' if url.endswith('/orderBooks') else
                    'lighter_orders.json' if '/orderBookOrders?' in url else None)
        if filename:
            return json.loads((FIXTURES / filename).read_text())
        return {'success': True, 'result': json.loads(ondo('depthBooksPerps', 0))['data'][0]}


class FakeTimeline:
    """下一条消息等上一来源处理完毕，虚拟时钟不会在处理途中前跳。"""

    def __init__(self, clock):
        self.clock, self.condition = clock, asyncio.Condition()
        self.index, self.inflight, self.service = 0, None, None
        self.events = [
            ('binance', 0, binance(0)), ('ondo', 0, ondo('depthBooksPerps', 0)),
            ('ondo', 0, ondo('tradesPerps', 0)), ('binance', 10, binance(10)),
            ('ondo', 10, ondo('tradesPerps', 10)), ('ondo', 10, ondo('depthBooksPerps', 10)),
            ('ondo', 20, OSError('离线测试断线')), ('binance', 30, binance(30)),
            ('ondo', 40, ondo('depthBooksPerps', 40)), ('ondo', 40, ondo('tradesPerps', 40)),
            ('ondo', 61, ondo('depthBooksPerps', 61)), ('binance', 61, binance(61)),
            ('ondo', 61, ondo('tradesPerps', 61)), ('ondo', 62, None)]

    async def next(self, source):
        while not self.service.tradable_markets:
            await asyncio.sleep(0)
        async with self.condition:
            if self.inflight == source:
                self.inflight = None
                self.condition.notify_all()
            while self.index < len(self.events):
                owner, second, value = self.events[self.index]
                if self.inflight is None and owner == source:
                    self.index += 1
                    self.inflight = source
                    self.clock.now = BASE / 1000 + second
                    if value is None:
                        self.service.request_stop(signal.SIGTERM)
                        await self.condition.wait()
                    if isinstance(value, Exception):
                        raise value
                    return value
                await self.condition.wait()
            await self.condition.wait()


class FakeWS:
    def __init__(self, timeline, source):
        self.timeline, self.source, self.sent = timeline, source, []

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.timeline.next(self.source)

    async def send(self, value):
        self.sent.append(json.loads(value))


class CollectIntegrationTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def cli(*arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(list(map(str, arguments)))
        return code, stdout.getvalue(), stderr.getvalue()

    async def test_collect_gzip_import_twice_and_backtest_with_real_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            root, clock, client = Path(directory), FakeClock(), FakeHTTP()
            timeline, connections = FakeTimeline(clock), []

            @contextlib.asynccontextmanager
            async def connect(url, **options):
                source = 'ondo' if 'ondoperps' in url else 'binance'
                ws = FakeWS(timeline, source)
                connections.append(ws)
                yield ws

            async def sleep(delay):
                if delay == 1 and asyncio.current_task().get_coro().__name__ == 'ws_loop':
                    await asyncio.sleep(0)
                    return
                await asyncio.Event().wait()

            data = root / 'collect'
            service = Collector(CollectSettings(data_dir=str(data), log_dir=str(root / 'log'),
                focus_symbols=('INTC',), request_interval_ms=0, silence_timeout_s=120),
                client=client, connect=connect, clock=clock, monotonic=clock,
                sleep=sleep, run_id='offline')
            timeline.service = service
            self.assertEqual(await asyncio.wait_for(service.run(), 5), 0)
            self.assertEqual(service.queue.qsize(), 0)
            self.assertFalse(service.state_path.exists())
            self.assertTrue(any(url.endswith('/contracts') for url in client.urls))
            self.assertTrue(any(url.endswith('/markets') for url in client.urls))
            self.assertGreaterEqual(len([ws for ws in connections if ws.source == 'ondo']), 2)
            paths = list(data.rglob('*.jsonl.gz'))
            self.assertTrue(paths)
            rows = [json.loads(line) for path in paths
                    for line in gzip.decompress(path.read_bytes()).splitlines()]
            self.assertEqual(len([row for row in rows if row['kind'] == 'ondo_trade']), 4)
            self.assertTrue(any(row['kind'] == 'depth_observation' for row in rows))
            self.assertTrue(all(row['run_id'] == 'offline' for row in rows))
            db = root / 'history.sqlite'
            first, first_out, error = self.cli('import-collected', '--src', data, '--db', db)
            self.assertEqual((first, error), (0, ''))
            second, second_out, error = self.cli('import-collected', '--src', data, '--db', db)
            self.assertEqual((second, error), (0, ''))
            self.assertEqual(json.loads(first_out)['ondo_trades'], 4)
            for kind in ('ondo_trades', 'binance_trades', 'depth_observations', 'contracts', 'gaps'):
                self.assertEqual(json.loads(second_out)[kind], 0, kind)
            with HistoryStore(db, create=False) as store:
                self.assertEqual(len(list(store.iter_binance_trades('INTCUSDT'))), 4)
                closed = list(store.iter_collector_gaps(source='ondo', market='*', channel='*'))
                self.assertTrue(any(row['start_ms'] == BASE + 10000 and
                                    row['end_ms'] == BASE + 40000 for row in closed))
            # 采集替身原本只有主动买单；回测单独补足双边中点预热，保留采集断言。
            with HistoryStore(db) as store:
                store.upsert_ondo_trades([dict(id='b3-opposite-seed', market='INTC-USD.P',
                    time=iso(BASE), price='100', size='1', cost='100', aggressor_side='sell')])
            config, report, summary = root / 'backtest.toml', root / 'report.json', root / 'summary.md'
            config.write_text('symbols = ["INTC"]\ndata_latency_ms = 0\nplace_latency_ms = 0\n'
                'cancel_latency_ms = 0\nquote_offset_bps = "100"\n'
                'max_ref_age_s = "120"\nguard_move_bps = "10000"\n', encoding='utf-8')
            code, _, error = self.cli('backtest', '--db', db, '--config', config,
                '--start', iso(BASE), '--end', iso(BASE + 63000), '--events',
                '--report', report, '--summary', summary)
            self.assertEqual((code, error), (0, ''))
            result = next(row for row in json.loads(report.read_text())['reports']
                          if row['hedge_mode'] == 'none')
            self.assertEqual(result['status'], 'ok')
            self.assertEqual(result['coverage']['gap_duration_ms'], 30000)
            self.assertFalse(result['pnl_determinate'])
            orders = [row['time_ms'] for row in result['events'] if row['type'] == 'order_sent']
            self.assertTrue(any(at < BASE + 10000 for at in orders))
            self.assertTrue(any(at >= BASE + 40000 for at in orders))
            self.assertFalse(any(BASE + 10000 <= at < BASE + 40000 for at in orders))
            self.assertIn('断档总时长：30 秒', summary.read_text())
            self.assertIn('损益不可判定', summary.read_text())
