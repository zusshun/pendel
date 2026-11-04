"""Offline behavior tests for the persistent collector without network access or port binding."""

import asyncio
import gzip
import json
import signal
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

from pendle.bt_config import CollectSettings


class Clock:
    def __init__(self, now=0):
        self.now = now

    def __call__(self):
        return self.now


def depth(market='INTC-USD.P'):
    return {'market': market, 'time': '2026-11-04T00:00:00Z',
            'bids': [['99.99', '2'], ['99', '3']],
            'asks': [['100.01', '4'], ['101', '5']]}


def trade(identifier='t1'):
    return {'id': identifier, 'market': 'INTC-USD.P', 'price': '100',
            'size': '2', 'cost': '200', 'aggressor_side': 'buy',
            'time': '2026-11-04T00:00:00.123456789Z'}


def message(channel, rows):
    return json.dumps({'type': 'update', 'channel': channel, 'data': rows})


class FakeWS:
    """WS asynchronous iterator with injectable exceptions and delays."""

    def __init__(self, events):
        self.events = iter(events)
        self.sent = []

    async def send(self, value):
        self.sent.append(json.loads(value))

    def __aiter__(self):
        return self

    async def __anext__(self):
        event = next(self.events, None)
        if event is None:
            raise StopAsyncIteration
        if isinstance(event, Exception):
            raise event
        if isinstance(event, tuple):
            delay, event = event
            await asyncio.sleep(delay)
        return event


class CollectorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()

    def make_collector(self, **kwargs):
        from pendle.collector import Collector
        values = {'data_dir': self.tmp.name, 'log_dir': self.tmp.name + '/log',
                  'focus_symbols': ('INTC',), 'request_interval_ms': 0}
        values.update(kwargs.pop('settings', {}))
        return Collector(CollectSettings(**values), clock=self.clock,
                         monotonic=self.clock, **kwargs)

    @staticmethod
    def drain(service):
        rows = []
        while not service.queue.empty():
            rows.append(service.queue.get_nowait())
            service.queue.task_done()
        return rows

    async def test_downsampling_and_trade_preservation(self):
        service = self.make_collector()
        service.tradable_markets = {'INTC-USD.P', 'DRAM-USD.P'}
        for now in (0, .25, .99, 1, 59, 60):
            self.clock.now = now
            await service.handle_ondo(message('depthBooksPerps', [depth(), depth('DRAM-USD.P')]))
            await service.handle_ondo(message('tradesPerps', [trade(str(now))]))
        rows = self.drain(service)
        self.assertEqual(len([r for r in rows if r['kind'] == 'depth']), 4)
        self.assertEqual(len([r for r in rows if r['kind'] == 'ondo_trade']), 6)
        observations = [r['payload'] for r in rows if r['kind'] == 'depth_observation']
        self.assertEqual(len(observations), 4)
        self.assertEqual({r['source'] for r in observations}, {'ws'})
        from pendle.market import parse_ondo
        from pendle.scan_import import depth_observation
        expected = depth_observation(parse_ondo({'success': True, 'result': depth()},
                                    'INTC-USD.P', 0), source='ws')
        self.assertEqual(observations[0], expected)

    async def test_invalid_messages_and_pong_do_not_close_channel_gaps(self):
        service = self.make_collector()
        key = ('ondo', 'INTC-USD.P', 'depthBooksPerps')
        await service.open_gap(key, '断线', 0)
        await service.handle_ondo(json.dumps({'type': 'pong'}))
        await service.handle_ondo(message('depthBooksPerps', [{'market': key[1]}]))
        self.assertIn(key, service.gaps)
        self.clock.now = 2
        await service.handle_ondo(message('depthBooksPerps', [depth()]))
        self.assertNotIn(key, service.gaps)
        gaps = [r['payload'] for r in self.drain(service) if r['kind'] == 'gap']
        self.assertEqual([g['end_ms'] for g in gaps], [None, 2000])

    async def test_silence_of_entire_channel_opens_wildcard_gap(self):
        service = self.make_collector(settings={'silence_timeout_s': '5'})
        await service.handle_ondo(message('depthBooksPerps', [depth()]))
        await service.handle_ondo(message('tradesPerps', [trade()]))
        self.clock.now = 4
        await service.handle_ondo(message('depthBooksPerps', [depth()]))
        self.clock.now = 6
        await service.check_silence()
        self.assertIn(('ondo', '*', 'tradesPerps'), service.gaps)
        self.assertNotIn(('ondo', '*', 'depthBooksPerps'), service.gaps)
        self.clock.now = 7
        await service.handle_ondo(message('tradesPerps', [trade('new')]))
        self.assertNotIn(('ondo', '*', 'tradesPerps'), service.gaps)

    async def test_startup_opens_only_connection_gaps_and_first_valid_message_closes_source(self):
        service = self.make_collector()
        await service.start_state()
        self.assertEqual(set(service.gaps), {(source, '*', '*')
                         for source in ('ondo', 'binance', 'lighter')})
        await service.handle_ondo(json.dumps({'type': 'pong'}))
        self.assertIn(('ondo', '*', '*'), service.gaps)
        self.clock.now = 2
        await service.handle_ondo(message('tradesPerps', [trade()]))
        self.assertNotIn(('ondo', '*', '*'), service.gaps)
        self.assertIn(('binance', '*', '*'), service.gaps)

    async def test_connection_silence_opens_one_wildcard_gap(self):
        service = self.make_collector(settings={'silence_timeout_s': '5'})
        await service.handle_ondo(message('depthBooksPerps', [depth()]))
        await service.handle_ondo(message('tradesPerps', [trade()]))
        self.clock.now = 6
        await service.check_silence()
        ondo_gaps = [key for key in service.gaps if key[0] == 'ondo']
        self.assertEqual(ondo_gaps, [('ondo', '*', '*')])
        self.clock.now = 7
        await service.handle_ondo(message('tradesPerps', [trade('recovered')]))
        self.assertNotIn(('ondo', '*', '*'), service.gaps)

    async def test_single_market_silence_is_quality_only_and_recovers(self):
        service = self.make_collector(settings={'focus_symbols': ('INTC', 'DRAM'),
                                      'silence_timeout_s': '5'})
        await service.handle_ondo(message('depthBooksPerps', [depth(), depth('DRAM-USD.P')]))
        await service.handle_ondo(message('tradesPerps', [trade()]))
        self.clock.now = 601
        other = {**trade('dram'), 'market': 'DRAM-USD.P'}
        await service.handle_ondo(message('depthBooksPerps', [depth('DRAM-USD.P')]))
        await service.handle_ondo(message('tradesPerps', [other]))
        await service.check_silence()
        await service.check_silence()
        self.assertFalse(any(key[0] == 'ondo' for key in service.gaps))
        stale = [r for r in self.drain(service) if r['kind'] == 'market_stale'
                 and r['market'] == 'INTC-USD.P']
        self.assertEqual({r['payload']['channel'] for r in stale},
                         {'depthBooksPerps', 'tradesPerps'})
        self.assertEqual(len(stale), 2)
        self.assertTrue(all(r['payload']['end_ms'] is None for r in stale))
        self.clock.now = 602
        await service.handle_ondo(message('tradesPerps', [trade('recovered')]))
        recovered = [r for r in self.drain(service) if r['kind'] == 'market_stale']
        self.assertEqual(recovered[0]['payload']['end_ms'], 602000)

    async def test_websocket_endpoints_are_configurable_and_default_to_market_stream(self):
        service = self.make_collector()
        self.assertEqual(service.ws_url('binance'),
                         'wss://fstream.binance.com/market/stream?streams=intcusdt@aggTrade')
        custom = self.make_collector(settings={
            'binance_ws_url': 'wss://example.test/market/stream?streams=',
            'ondo_ws_url': 'wss://example.test/ondo'})
        self.assertEqual(custom.ws_url('binance'),
                         'wss://example.test/market/stream?streams=intcusdt@aggTrade')
        self.assertEqual(custom.ws_url('ondo'), 'wss://example.test/ondo')

    async def test_health_logs_source_gaps_market_stale_and_binance_symbol_age(self):
        service = self.make_collector(settings={'silence_timeout_s': '5'})
        await service.open_gap(('binance', '*', '*'), '静默超时', 0)
        self.clock.now = 601
        await service.check_silence()
        await service.health_once()
        records = [json.loads(line) for line in
                   (Path(self.tmp.name) / 'log' / 'collect_19700101.log').read_text().splitlines()]
        health = next(row for row in records if '来源' in row)
        self.assertGreaterEqual(health['来源']['binance']['断档数'], 1)
        self.assertGreaterEqual(health['来源']['binance']['market_stale 数'], 1)
        self.assertEqual(health['币安各符号最近消息年龄秒'], {'INTCUSDT': None})
        self.clock.now = 602
        await service.handle_binance(json.dumps({'stream': 'intcusdt@aggTrade', 'data': {
            'e': 'aggTrade', 'a': 5, 'p': '100', 'q': '2', 'f': 6, 'l': 7, 'T': 123, 'm': True}}))
        self.clock.now = 605
        await service.health_once()
        latest = json.loads((Path(self.tmp.name) / 'log' / 'collect_19700101.log')
                            .read_text().splitlines()[-1])
        self.assertEqual(latest['币安各符号最近消息年龄秒']['INTCUSDT'], 3)

    async def test_connected_source_without_messages_emits_one_chinese_warning(self):
        service = self.make_collector()
        service.connected_since['binance'] = 0
        self.clock.now = 181
        await service.health_once()
        await service.health_once()
        rows = [json.loads(line) for line in
                (Path(self.tmp.name) / 'log' / 'collect_19700101.log').read_text().splitlines()]
        warnings = [row for row in rows if row.get('级别') == 'WARNING']
        self.assertEqual(len(warnings), 1)
        self.assertIn('binance', warnings[0]['警告'])
        self.assertIn('连接保持', warnings[0]['警告'])
        self.assertIn('无有效行情消息', warnings[0]['警告'])

    async def test_silent_source_warning_survives_reconnects_and_resets_after_valid_data(self):
        service = self.make_collector(settings={'source_silence_warn_s': '180'})
        for now in (0, 30, 60, 90, 120, 150, 180):
            self.clock.now = now
            service.connected_since['binance'] = now * 1000
            await service.health_once()
            service.connected_since.pop('binance')
        path = Path(self.tmp.name) / 'log' / 'collect_19700101.log'
        warnings = [json.loads(line) for line in path.read_text().splitlines()
                    if json.loads(line).get('级别') == 'WARNING']
        self.assertEqual(len(warnings), 1)
        self.clock.now = 181
        await service.handle_binance(json.dumps({'stream': 'intcusdt@aggTrade', 'data': {
            'e': 'aggTrade', 'a': 5, 'p': '100', 'q': '2', 'f': 6, 'l': 7, 'T': 123, 'm': True}}))
        service.connected_since['binance'] = 181000
        self.clock.now = 360
        await service.health_once()
        self.clock.now = 361
        await service.health_once()
        warnings = [json.loads(line) for line in path.read_text().splitlines()
                    if json.loads(line).get('级别') == 'WARNING']
        self.assertEqual(len(warnings), 2)

    async def test_market_stale_threshold_is_independent_of_connection_silence(self):
        service = self.make_collector(settings={'market_stale_s': '10',
                                      'silence_timeout_s': '30'})
        await service.handle_ondo(message('tradesPerps', [trade()]))
        self.clock.now = 11
        await service.check_silence()
        self.assertFalse(service.gaps)
        stale = [row for row in self.drain(service) if row['kind'] == 'market_stale']
        self.assertTrue(stale)
        self.assertEqual(stale[0]['payload']['stale_ms'], 11000)

    async def test_channel_gap_recovers_only_on_its_own_valid_messages(self):
        service = self.make_collector(settings={'silence_timeout_s': '5'})
        await service.handle_ondo(message('tradesPerps', [trade()]))
        self.clock.now = 4
        await service.handle_ondo(message('depthBooksPerps', [depth()]))
        self.clock.now = 6
        await service.check_silence()
        await service.handle_ondo(message('depthBooksPerps', [depth('DRAM-USD.P')]))
        self.assertIn(('ondo', '*', 'tradesPerps'), service.gaps)
        await service.handle_ondo(message('tradesPerps', [trade('recovered')]))
        self.assertNotIn(('ondo', '*', 'tradesPerps'), service.gaps)

    async def test_legacy_checkpoint_gaps_collapse_to_source_and_keep_earliest_start(self):
        service = self.make_collector()
        service.state_path.write_text(json.dumps({'checkpoint_ms': 12000, 'channels': [
            {'key': ['ondo', 'INTC-USD.P', 'depthBooksPerps'],
             'last_seen_ms': 5000, 'gap_start_ms': 5000},
            {'key': ['ondo', 'DRAM-USD.P', 'tradesPerps'],
             'last_seen_ms': 2000, 'gap_start_ms': 2000}]}))
        self.clock.now = 20
        restarted = self.make_collector()
        await restarted.start_state()
        self.assertEqual(restarted.gaps[('ondo', '*', '*')]['start_ms'], 2000)
        self.assertTrue(all(key[1] == '*' for key in restarted.gaps))

    async def test_lighter_single_market_poll_failure_does_not_open_gap(self):
        from pendle.market import MarketError
        class HTTP:
            def get_json(self, url):
                if url.endswith('/orderBooks'):
                    return json.loads((Path(__file__).parent / 'fixtures' / 'lighter_markets.json').read_text())
                raise MarketError('单市场不可用')
        async def sleep(delay):
            service.request_stop()
        service = self.make_collector(client=HTTP(), sleep=sleep)
        await service.lighter_loop()
        self.assertFalse(service.gaps)
        errors = [row for row in self.drain(service) if row['kind'] == 'error']
        self.assertEqual(errors[0]['source'], 'lighter')
        self.assertEqual(errors[0]['market'], 'INTC')

    async def test_abnormal_restart_uses_last_checkpoint_and_waits_for_valid_data(self):
        service = self.make_collector()
        await service.start_state()
        self.clock.now = 5
        await service.handle_ondo(message('depthBooksPerps', [depth()]))
        await service.save_state()
        self.clock.now = 20
        restarted = self.make_collector()
        await restarted.start_state()
        key = ('ondo', '*', '*')
        self.assertEqual(restarted.gaps[key]['start_ms'], 5000)
        self.assertIn('异常', restarted.gaps[key]['reason'])
        await restarted.handle_ondo(message('depthBooksPerps', [depth()]))
        closed = [r['payload'] for r in self.drain(restarted)
                  if r['kind'] == 'gap' and r['payload']['end_ms'] is not None]
        self.assertEqual(closed[0]['end_ms'], 20000)

    async def test_disconnect_backoff_and_first_valid_recovery(self):
        calls, waits, sockets = [], [], []

        @asynccontextmanager
        async def connect(url, **options):
            calls.append((url, options))
            ws = FakeWS([OSError('断线')] if len(calls) < 3 else
                        [message('depthBooksPerps', [depth()]), OSError('断线')])
            sockets.append(ws)
            yield ws

        async def sleep(delay):
            # The heartbeat task's waiter must not advance the virtual connection time.
            if delay >= 30:
                await asyncio.Event().wait()
            waits.append(delay)
            self.clock.now += delay
            if len(waits) >= 3:
                service.request_stop()
            await asyncio.sleep(0)

        service = self.make_collector(connect=connect, sleep=sleep)
        await service.open_gap(('ondo', 'INTC-USD.P', 'depthBooksPerps'), '启动', 0)
        await service.ws_loop('ondo')
        self.assertEqual(waits, [1, 2, 1])
        self.assertEqual(service.reconnects['ondo'], 3)
        self.assertEqual(sockets[0].sent, [
            {'op': 'subscribe', 'channel': 'depthBooksPerps', 'limit': 100},
            {'op': 'subscribe', 'channel': 'tradesPerps', 'limit': 100}])
        closed = [r['payload'] for r in self.drain(service) if r['kind'] == 'gap'
                  and r['payload']['end_ms'] is not None]
        self.assertEqual(closed[0]['end_ms'], 3000)

    async def test_binance_mapping_and_proactive_reconnect(self):
        service = self.make_collector(settings={
            'symbol_overrides': {'INTC': {'binance_symbol': 'INTCUSDC'}},
            'binance_reconnect_s': '2'})
        self.assertTrue(service.ws_url('binance').endswith('intcusdc@aggTrade'))
        raw = json.dumps({'stream': 'intcusdc@aggTrade', 'data': {
            'e': 'aggTrade', 'a': 5, 'p': '100', 'q': '2', 'f': 6, 'l': 7, 'T': 123, 'm': True}})
        await service.handle_binance(raw)
        saved = self.drain(service)[0]
        self.assertEqual(saved['payload']['symbol'], 'INTCUSDC')
        self.clock.now = 3
        with self.assertRaisesRegex(TimeoutError, '轮换'):
            await service._receive('binance', FakeWS([raw]), 0)

    async def test_heartbeat_and_delayed_iterator_timeout(self):
        async def advance(delay):
            self.clock.now += delay
            await asyncio.sleep(0)
        service = self.make_collector(settings={'silence_timeout_s': '5'}, sleep=advance)
        with self.assertRaises(TimeoutError):
            await service._receive('ondo', FakeWS([(3600, '{}')]), 0)
        self.assertEqual(self.clock.now, 5)
        ws = FakeWS([])
        async def sleep(delay):
            self.assertEqual(delay, 30)
            if ws.sent:
                raise asyncio.CancelledError
        service.sleep = sleep
        with self.assertRaises(asyncio.CancelledError):
            await service._heartbeat(ws)
        self.assertEqual(ws.sent, [{'op': 'ping'}])

    async def test_rest_fallback_only_scans_when_ws_depth_has_a_gap(self):
        class HTTP:
            def __init__(self):
                self.urls = []

            def get_json(self, url):
                self.urls.append(url)
                return {'success': True, 'result': depth()}
        client = HTTP()
        service = self.make_collector(client=client)
        service.tradable_markets = {'INTC-USD.P'}
        await service.rest_scan_once()
        self.assertEqual(client.urls, [])
        await service.open_gap(('ondo', 'INTC-USD.P', 'depthBooksPerps'), '断线')
        await service.rest_scan_once()
        self.assertEqual(len(client.urls), 1)
        self.assertIn(('ondo', 'INTC-USD.P', 'depthBooksPerps'), service.gaps)
        observation = [r for r in self.drain(service) if r['kind'] == 'depth_observation'][0]
        self.assertEqual(observation['payload']['source'], 'rest')

    async def test_hour_rotation_and_disk_limit(self):
        from pendle.collector import HourlyWriter
        writer = HourlyWriter(self.tmp.name, 'run1', '1')
        for at in (3599999, 3600000):
            self.assertFalse(writer.write_batch([{'source': 'ondo', 'received_ms': at, 'kind': 'test'}]))
        writer.close()
        paths = sorted(Path(self.tmp.name).rglob('*.jsonl.gz'))
        self.assertEqual([p.name for p in paths],
                         ['19700101T00Z-run1.jsonl.gz', '19700101T01Z-run1.jsonl.gz'])
        self.assertEqual([json.loads(gzip.decompress(p.read_bytes()))['received_ms'] for p in paths],
                         [3599999, 3600000])
        service = self.make_collector(settings={'max_disk_gb': '.000000001'})
        self.assertEqual(await service.run(), 75)

    async def test_signal_flushes_queue_and_removes_running_marker(self):
        service = self.make_collector()
        async def ws_loop(source):
            if source == 'ondo':
                await service.handle_ondo(message('tradesPerps', [trade()]))
                service.request_stop(signal.SIGTERM)
            else:
                await asyncio.Event().wait()
        async def idle():
            await asyncio.Event().wait()
        service.ws_loop = ws_loop
        service.contracts_loop = service.rest_loop = service.lighter_loop = service.monitor_loop = idle
        with patch('asyncio.get_running_loop', wraps=asyncio.get_running_loop) as get_loop:
            self.assertEqual(await service.run(), 0)
            self.assertGreater(get_loop.call_count, 0)
        self.assertFalse(service.state_path.exists())
        rows = []
        for path in Path(self.tmp.name).rglob('*.jsonl.gz'):
            rows.extend(json.loads(line) for line in gzip.decompress(path.read_bytes()).splitlines())
        self.assertEqual(len([r for r in rows if r['kind'] == 'ondo_trade']), 1)
        self.assertEqual(service.queue.qsize(), 0)

    async def test_health_log_contains_sources_age_reconnect_and_queue(self):
        service = self.make_collector()
        await service.handle_ondo(message('tradesPerps', [trade()]))
        self.clock.now = 60
        await service.health_once()
        path = Path(self.tmp.name) / 'log' / 'collect_19700101.log'
        text = path.read_text()
        for value in ('ondo', 'binance', 'lighter', '消息数', '最近消息年龄', '重连次数', '队列长度'):
            self.assertIn(value, text)

    async def test_gap_transition_survives_crash_between_health_checkpoints(self):
        service = self.make_collector()
        await service.start_state()
        self.clock.now = 5
        await service.handle_ondo(message('depthBooksPerps', [depth()]))
        await service.save_state()
        self.clock.now = 10
        await service.handle_ondo(message('depthBooksPerps', [depth()]))
        key = ('ondo', '*', '*')
        self.clock.now = 15
        await service.open_gap(key, '断线')
        restarted = self.make_collector()
        await restarted.start_state()
        self.assertEqual(restarted.gaps[key]['start_ms'], 10000)

    async def test_gap_close_updates_restart_state_before_next_health_checkpoint(self):
        service = self.make_collector()
        await service.start_state()
        self.clock.now = 5
        await service.handle_ondo(message('depthBooksPerps', [depth()]))
        self.clock.now = 10
        restarted = self.make_collector()
        await restarted.start_state()
        self.assertEqual(restarted.gaps[('ondo', '*', '*')]['start_ms'], 5000)

    async def test_cancelled_checkpoint_finishes_before_clean_shutdown_state(self):
        import threading
        entered, release = threading.Event(), threading.Event()
        service = self.make_collector()
        replace = Path.replace
        def delayed_replace(path, target):
            if path == service.state_path.with_suffix('.tmp'):
                entered.set()
                release.wait(2)
            return replace(path, target)
        with patch.object(Path, 'replace', delayed_replace):
            checkpoint = asyncio.create_task(service.save_state())
            clean = None
            try:
                while not entered.is_set():
                    await asyncio.sleep(0)
                checkpoint.cancel()
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                self.assertTrue(service.state_lock.locked(), '取消后仍应等保存线程完成再释放锁')
                clean = asyncio.create_task(service.save_state(clean=True))
            finally:
                release.set()
                await asyncio.gather(checkpoint, return_exceptions=True)
                if clean:
                    await clean
        self.assertFalse(service.state_path.exists())

    async def test_observation_interval_and_exponential_backoff_cap(self):
        service = self.make_collector(settings={'other_snapshot_s': '10'})
        service.tradable_markets = {'INTC-USD.P'}
        for at in (0, 9, 10, 19, 20):
            self.clock.now = at
            await service.handle_ondo(message('depthBooksPerps', [depth()]))
        self.assertEqual(len([r for r in self.drain(service) if r['kind'] == 'depth_observation']), 3)
        @asynccontextmanager
        async def connect(url, **kwargs):
            raise OSError('断线')
            yield
        waits = []
        async def wait(delay):
            waits.append(delay)
            if len(waits) == 8:
                service.request_stop()
        service.connect, service.sleep = connect, wait
        await service.ws_loop('binance')
        self.assertEqual(waits, [1, 2, 4, 8, 16, 32, 60, 60])

    async def test_http_is_bounded_and_does_not_block_event_loop(self):
        import threading
        release, entered = threading.Event(), threading.Event()
        lock = threading.Lock()
        class HTTP:
            active, maximum = 0, 0
            def get_json(self, url):
                with lock:
                    self.active += 1
                    self.maximum = max(self.maximum, self.active)
                    entered.set()
                release.wait(2)
                with lock:
                    self.active -= 1
                return {'ok': True}
        client = HTTP()
        service = self.make_collector(client=client, settings={'max_http_workers': 2})
        tasks = [asyncio.create_task(service.http(str(i))) for i in range(5)]
        try:
            while not entered.is_set():
                await asyncio.sleep(0)
            # The main event loop can still process fills; even if all HTTP threads block, WS processing remains responsive.
            await service.handle_ondo(message('tradesPerps', [trade()]))
            self.assertEqual(service.counts['ondo'], 1)
            self.assertLessEqual(client.maximum, 2)
        finally:
            release.set()
            await asyncio.gather(*tasks)
        self.assertEqual(client.maximum, 2)

    async def test_contract_schedule_and_lighter_poll_use_fake_http(self):
        folder = Path(__file__).parent / 'fixtures'
        class HTTP:
            def __init__(self):
                self.urls = []
            def get_json(self, url):
                self.urls.append(url)
                if url.endswith('/markets'):
                    return {'success': True, 'result': {'perps': {'tradingPairs': []}}}
                name = ('ondo_contracts.json' if url.endswith('/contracts') else
                        'lighter_markets.json' if url.endswith('/orderBooks') else 'lighter_orders.json')
                return json.loads((folder / name).read_text())
        client = HTTP()
        waits = []
        async def sleep(delay):
            waits.append(delay)
            service.request_stop()
        service = self.make_collector(client=client, sleep=sleep)
        await service.contracts_loop()
        self.assertEqual(waits, [300])
        self.assertEqual(service.tradable_markets, {'INTC-USD.P'})
        service.stop.clear()
        waits.clear()
        await service.open_gap(('lighter', 'INTC', 'orderBookOrders'), '开始')
        await service.lighter_loop()
        self.assertEqual(waits, [2])
        self.assertTrue(any('/orderBookOrders?market_id=137&limit=100' in url for url in client.urls))
        self.assertNotIn(('lighter', 'INTC', 'orderBookOrders'), service.gaps)
        self.assertTrue(any(row['source'] == 'lighter' and row['kind'] == 'depth'
                            for row in self.drain(service)))

    async def test_shutdown_finishes_already_received_trade_before_flushing(self):
        service = self.make_collector()
        original = service.handle_ondo
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed(raw):
            entered.set()
            await release.wait()
            await original(raw)
        service.handle_ondo = delayed
        receiver = asyncio.create_task(service._receive('ondo',
            FakeWS([message('tradesPerps', [trade()])]), 0))
        await entered.wait()
        receiver.cancel()
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(receiver, return_exceptions=True)
        self.assertEqual(len([r for r in self.drain(service) if r['kind'] == 'ondo_trade']), 1)

    async def test_cancel_during_receive_timer_cleanup_preserves_received_trade(self):
        service = self.make_collector()
        entered, release = asyncio.Event(), asyncio.Event()
        gather = asyncio.gather
        async def delayed_gather(*tasks, **kwargs):
            entered.set()
            await release.wait()
            return await gather(*tasks, **kwargs)
        with patch('pendle.collector.asyncio.gather', delayed_gather):
            receiver = asyncio.create_task(service._receive('ondo',
                FakeWS([message('tradesPerps', [trade()])]), 0))
            await entered.wait()
            receiver.cancel()
            release.set()
            await gather(receiver, return_exceptions=True)
        self.assertEqual(len([r for r in self.drain(service) if r['kind'] == 'ondo_trade']), 1)

    async def test_cancel_after_socket_receive_before_wait_resumes_preserves_trade(self):
        service = self.make_collector()
        wait = asyncio.wait
        async def cancelled_wait(*args, **kwargs):
            result = await wait(*args, **kwargs)
            asyncio.current_task().cancel()
            await asyncio.sleep(0)
            return result
        with patch('pendle.collector.asyncio.wait', cancelled_wait):
            receiver = asyncio.create_task(service._receive('ondo',
                FakeWS([message('tradesPerps', [trade()])]), 0))
            await asyncio.gather(receiver, return_exceptions=True)
        self.assertEqual(len([r for r in self.drain(service) if r['kind'] == 'ondo_trade']), 1)

    async def test_lighter_failed_market_id_is_resolved_again(self):
        from pendle.market import MarketError
        fixture = Path(__file__).parent / 'fixtures'
        class HTTP:
            def __init__(self):
                self.urls, self.resolves = [], 0
            def get_json(self, url):
                self.urls.append(url)
                if url.endswith('/orderBooks'):
                    self.resolves += 1
                    row = json.loads((fixture / 'lighter_markets.json').read_text())
                    row['order_books'][0]['market_id'] = 137 if self.resolves == 1 else 138
                    return row
                if 'market_id=137' in url:
                    raise MarketError('市场已更新')
                return json.loads((fixture / 'lighter_orders.json').read_text())
        http = HTTP()
        polls = []
        async def sleep(delay):
            polls.append(delay)
            self.clock.now += delay
            if len(polls) == 2:
                service.request_stop()
        service = self.make_collector(client=http, sleep=sleep,
                                      settings={'lighter_meta_refresh_s': '2'})
        await service.lighter_loop()
        self.assertEqual(http.resolves, 2)
        self.assertEqual(service.counts['lighter'], 1)
        self.assertNotIn(('lighter', 'INTC', 'orderBookOrders'), service.gaps)

    async def test_running_disk_limit_flushes_and_returns_75(self):
        service = self.make_collector(settings={'max_disk_gb': '.000001'})
        async def ws_loop(source):
            for i in range(40):
                await service.handle_ondo(message('tradesPerps', [trade(str(i))]))
            await asyncio.Event().wait()
        async def idle():
            await asyncio.Event().wait()
        service.ws_loop = ws_loop
        service.contracts_loop = service.rest_loop = service.lighter_loop = service.monitor_loop = idle
        self.assertEqual(await asyncio.wait_for(service.run(), 3), 75)
        self.assertEqual(service.queue.qsize(), 0)
        self.assertFalse(service.state_path.exists())
        for path in Path(self.tmp.name).rglob('*.jsonl.gz'):
            self.assertTrue(gzip.decompress(path.read_bytes()).endswith(b'\n'))


class MissingDependencyTests(unittest.TestCase):
    def test_collect_without_websockets_returns_chinese_error(self):
        import io
        from pendle.collector import run_collect
        with patch.dict('sys.modules', {'websockets': None}), patch('sys.stderr', new_callable=io.StringIO) as err:
            self.assertEqual(run_collect(CollectSettings()), 2)
        self.assertIn('websockets', err.getvalue())
        self.assertIn('安装', err.getvalue())
