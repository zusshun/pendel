"""Continuous read-only collection with a separate disk writer, connection gaps, and per-market quality statistics."""

import asyncio
import gzip
import json
import shutil
import signal
import sys
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from .history import ONDO_API, _ondo_trade
from .market import (HttpClient, MarketError, RateLimited, number, parse_lighter,
                     parse_ondo, resolve_lighter, validate_ondo_contract)
from .scan_import import depth_observation

ONDO_WS = 'wss://api.ondoperps.xyz/ws'
BINANCE_WS = 'wss://fstream.binance.com/market/stream?streams='
LIGHTER_API = 'https://mainnet.zklighter.elliot.ai/api/v1'
ONDO_MARKETS = 'https://api.ondoperps.xyz/v1/markets'


def _json(value):
    return json.dumps(value, ensure_ascii=False, default=str, separators=(',', ':'))


class HourlyWriter:
    """Use in the writer thread; flush gzip periodically to recover complete lines after an abnormal exit."""

    def __init__(self, directory, run_id, max_disk_gb, min_free_gb='2', *,
                 monotonic=time.monotonic):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self.limit = int(number(max_disk_gb) * 1024 ** 3)
        self.min_free = int(number(min_free_gb) * 1024 ** 3)
        self.monotonic = monotonic
        self.files = {}
        self.file_sizes = {}
        self.usage = self.disk_usage()
        self.last_calibration = self.monotonic()

    def disk_usage(self):
        return sum(p.stat().st_size for p in self.directory.rglob('*') if p.is_file())

    def _update_size(self, stream):
        path = Path(stream.name)
        size = path.stat().st_size
        self.usage += size - self.file_sizes.get(path, 0)
        self.file_sizes[path] = size

    def at_capacity(self):
        return self.usage >= self.limit or shutil.disk_usage(self.directory).free < self.min_free

    def write_batch(self, rows):
        for row in rows:
            source = row['source']
            hour = datetime.fromtimestamp(row['received_ms'] / 1000, timezone.utc).strftime('%Y%m%dT%H')
            current = self.files.get(source)
            if current is None or current[0] != hour:
                if current:
                    current[1].close()
                    self._update_size(current[1])
                folder = self.directory / source
                folder.mkdir(parents=True, exist_ok=True)
                path = folder / f'{hour}Z-{self.run_id}.jsonl.gz'
                self.file_sizes[path] = path.stat().st_size if path.exists() else 0
                self.files[source] = (hour, gzip.open(path, 'ab'))
            self.files[source][1].write((_json(row) + '\n').encode())
        for _, stream in self.files.values():
            stream.flush()
            self._update_size(stream)
        if self.monotonic() - self.last_calibration >= 600:
            self.usage = self.disk_usage()
            self.last_calibration = self.monotonic()
        return self.at_capacity()

    def close(self):
        for _, stream in self.files.values():
            stream.close()
            self._update_size(stream)
        self.files.clear()


class Collector:
    def __init__(self, settings, *, client=None, connect=None, clock=time.time,
                 monotonic=time.monotonic, sleep=asyncio.sleep, run_id=None):
        self.settings = settings
        self.client = client or HttpClient(float(settings.http_timeout_s))
        self.connect = connect
        self.clock, self.sleep = clock, sleep
        self.monotonic = monotonic
        self.run_id = run_id or uuid.uuid4().hex
        self.queue = asyncio.Queue(maxsize=settings.write_queue_size)
        self.stop = asyncio.Event()
        self.exit_code = 0
        self.started_ms = self.now_ms()
        self.started_monotonic = self.monotonic()
        self.expected = set()
        self.last_seen, self.gaps = {}, {}
        self.last_seen_monotonic, self.source_last_monotonic = {}, {}
        self.market_stale = {}
        self.market_stale_since = {}
        self.gap_counts, self.stale_counts = Counter(), Counter()
        self.counts, self.reconnects = Counter(), Counter()
        self.source_last, self.connections = {}, {}
        self.connected_since, self.silence_warned = {}, {}
        self.last_snapshot, self.last_observation = {}, {}
        self.lighter_metadata, self.lighter_unsupported = {}, set()
        self.lighter_warned = set()
        self.last_lighter_meta = None
        self.tradable_markets = set()
        self.http_semaphore = asyncio.Semaphore(settings.max_http_workers)
        self.http_lock = asyncio.Lock()
        self.last_http = None
        self.state_path = Path(settings.data_dir) / '.collector-running.json'
        self.saved_path = Path(settings.data_dir) / '.collector-state.json'
        self.state_lock = asyncio.Lock()
        self.state_started = False
        self.writer_active = False
        self._register_focus()

    def now_ms(self):
        return int(self.clock() * 1000)

    def _register_focus(self):
        for symbol in self.settings.focus_symbols:
            market = self.settings.for_symbol(symbol).ondo_market
            for channel in ('depthBooksPerps', 'tradesPerps'):
                self.expected.add(('ondo', market, channel))
            self.expected.add(('binance', self.settings.for_symbol(symbol).binance_symbol, 'aggTrade'))
            self.expected.add(('lighter', symbol, 'orderBookOrders'))

    def scope_keys(self):
        """Connection-wide and channel-wide gaps define quote suspension scopes; asterisks satisfy nonempty history-store constraints."""
        channels = {(source, '*', channel) for source, _, channel in self.expected}
        return channels | {(source, '*', '*') for source, _, _ in self.expected}

    async def emit(self, source, kind, market, payload, received_ms=None):
        await self.queue.put({'source': source, 'kind': kind, 'market': market,
                              'received_ms': self.now_ms() if received_ms is None else received_ms,
                              'run_id': self.run_id, 'connection_id': self.connections.get(source),
                              'payload': payload})

    async def open_gap(self, key, reason, start_ms=None):
        if key in self.gaps:
            return
        row = dict(zip(('source', 'market', 'channel'), key))
        row.update(start_ms=self.last_seen.get(key, self.started_ms) if start_ms is None else start_ms,
                   end_ms=None, reason=reason)
        self.gaps[key] = row
        self.gap_counts[key[0]] += 1
        await self.emit(key[0], 'gap', key[1], dict(row))
        if self.state_started:
            await self.save_state()

    async def valid(self, key):
        self.expected.add(key)
        at = self.now_ms()
        elapsed_at = self.monotonic()
        changed = False
        for scope in (key, (key[0], '*', key[2]), (key[0], '*', '*')):
            if scope in self.gaps:
                row = self.gaps.pop(scope)
                await self.emit(scope[0], 'gap', scope[1], {**row, 'end_ms': at})
                changed = True
            self.last_seen[scope] = at
            self.last_seen_monotonic[scope] = elapsed_at
        if key in self.market_stale:
            row = self.market_stale.pop(key)
            await self.emit(key[0], 'market_stale', key[1],
                            {**row, 'end_ms': at, 'stale_ms':
                             int((elapsed_at - self.market_stale_since.pop(key)) * 1000)})
        self.source_last[key[0]] = at
        self.source_last_monotonic[key[0]] = elapsed_at
        self.counts[key[0]] += 1
        self.silence_warned.pop(key[0], None)
        if changed and self.state_started:
            await self.save_state()

    async def start_state(self):
        previous = self.state_path if self.state_path.exists() else self.saved_path
        if previous.exists():
            old = json.loads(await asyncio.to_thread(previous.read_text, encoding='utf-8'))
            abnormal = previous == self.state_path
            previous_channels = old.get('channels', [])
            for source in sorted({item['key'][0] for item in previous_channels}):
                items = [item for item in previous_channels if item['key'][0] == source]
                connection = next((item for item in items if item['key'][1:] == ['*', '*']), None)
                if connection is not None:
                    start = connection.get('gap_start_ms')
                    if start is None:
                        start = connection.get('last_seen_ms', old['checkpoint_ms'])
                else:
                    # Aggregate legacy per-market checkpoints into connection scope, preserving the earliest recorded gap start.
                    starts = [item['gap_start_ms'] for item in items
                              if item.get('gap_start_ms') is not None]
                    start = min(starts) if starts else max(
                        item.get('last_seen_ms', old['checkpoint_ms']) for item in items)
                await self.open_gap((source, '*', '*'),
                                    '上次异常退出' if abnormal else '停机后重启', start)
                for item in items:
                    key = tuple(item['key'])
                    if key[1] != '*':
                        self.expected.add(key)
                    elif key[2] != '*' and item.get('gap_start_ms') is not None:
                        await self.open_gap(key, '上次异常退出' if abnormal else '停机后重启',
                                            item['gap_start_ms'])
        for source in sorted({key[0] for key in self.expected}):
            await self.open_gap((source, '*', '*'), '启动等待首条有效消息', self.started_ms)
        await self.save_state()
        self.state_started = True

    async def save_state(self, clean=False):
        async with self.state_lock:
            if self.writer_active:
                await self.queue.join()
            data = {'run_id': self.run_id, 'checkpoint_ms': self.now_ms(), 'channels': [
                {'key': key, 'last_seen_ms': self.last_seen.get(key, self.started_ms),
                 'gap_start_ms': self.gaps.get(key, {}).get('start_ms')}
                for key in sorted(self.expected | self.scope_keys() | self.gaps.keys())]}
            path = self.saved_path if clean else self.state_path
            def save():
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_suffix('.tmp')
                temporary.write_text(_json(data), encoding='utf-8')
                temporary.replace(path)
                if clean:
                    self.state_path.unlink(missing_ok=True)
            saving = asyncio.create_task(asyncio.to_thread(save))
            try:
                await asyncio.shield(saving)
            except asyncio.CancelledError:
                # The thread outlives coroutine cancellation; hold the lock until it finishes to avoid recreating run markers after exit.
                await saving
                raise

    async def check_silence(self):
        now = self.monotonic()
        for source in sorted({key[0] for key in self.expected}):
            connection = (source, '*', '*')
            last = self.last_seen.get(connection, self.started_ms)
            since = self.last_seen_monotonic.get(connection, self.started_monotonic)
            if now - since > float(self.settings.silence_timeout_s):
                await self.open_gap(connection, '连接静默超时', last)
                continue
            for key in sorted(self.scope_keys()):
                if key[0] != source or key[2] == '*':
                    continue
                last = self.last_seen.get(key, self.started_ms)
                since = self.last_seen_monotonic.get(key, self.started_monotonic)
                if now - since > float(self.settings.silence_timeout_s):
                    await self.open_gap(key, '整频道静默超时', last)
        for key in sorted(self.expected):
            last = self.last_seen.get(key, self.started_ms)
            since = self.last_seen_monotonic.get(key, self.started_monotonic)
            if now - since > float(self.settings.market_stale_s) and key not in self.market_stale:
                row = dict(zip(('source', 'market', 'channel'), key))
                row.update(start_ms=last, end_ms=None, reason='单市场更新陈旧',
                           stale_ms=int((now - since) * 1000))
                self.market_stale[key] = row
                self.market_stale_since[key] = since
                self.stale_counts[key[0]] += 1
                await self.emit(key[0], 'market_stale', key[1], dict(row))

    async def record_book(self, book, payload, source='ws'):
        market, at = book.symbol + '-USD.P', book.received_ms
        settings = self.settings
        elapsed_at = self.monotonic()
        snapshot_key = (source, market)
        if book.symbol in settings.focus_symbols:
            last = self.last_snapshot.get(snapshot_key)
            if last is None or elapsed_at - last >= float(settings.focus_snapshot_s):
                await self.emit('ondo', 'depth', market, payload, at)
                self.last_snapshot[snapshot_key] = elapsed_at
        # Sample all tradable markets each minute; full order books cannot replace derived competition records.
        if market in self.tradable_markets:
            last = self.last_observation.get(snapshot_key)
            if last is None or elapsed_at - last >= float(settings.other_snapshot_s):
                observation = depth_observation(book,
                    price_tick=settings.for_symbol(book.symbol).price_tick,
                    band_bps=settings.band_bps, max_tick=settings.max_tick_depth, source=source)
                await self.emit('ondo', 'depth_observation', market, observation, at)
                self.last_observation[snapshot_key] = elapsed_at

    async def handle_ondo(self, raw):
        try:
            message = json.loads(raw)
            if not isinstance(message, dict):
                return
            channel = message.get('channel')
            if channel not in ('depthBooksPerps', 'tradesPerps'):
                return
            rows = message.get('data')
            if not isinstance(rows, list):
                return
        except (ValueError, TypeError):
            return
        for row in rows:
            try:
                market = row['market']
                if channel == 'depthBooksPerps':
                    payload = {'success': True, 'result': row}
                    book = parse_ondo(payload, market, self.now_ms())
                    await self.valid(('ondo', market, channel))
                    await self.record_book(book, payload)
                else:
                    _ondo_trade(row)
                    await self.valid(('ondo', market, channel))
                    await self.emit('ondo', 'ondo_trade', market, row)
            except (MarketError, KeyError, TypeError, ValueError):
                continue

    async def handle_binance(self, raw):
        try:
            message = json.loads(raw)
            row = message['data']
            if row.get('e') != 'aggTrade':
                return
            symbol = message['stream'].split('@')[0].upper()
            if ('binance', symbol, 'aggTrade') not in self.expected:
                return
            price, quantity = number(row['p']), number(row['q'])
            ids = [row[key] for key in ('a', 'f', 'l', 'T')]
            if (price <= 0 or quantity < 0 or type(row['m']) is not bool
                    or any(type(i) is not int or i < 0 for i in ids) or ids[2] < ids[1]):
                return
            payload = dict(symbol=symbol, agg_id=ids[0], price=str(price), quantity=str(quantity),
                           first_trade_id=ids[1], last_trade_id=ids[2], transact_time=ids[3],
                           is_buyer_maker=row['m'])
            await self.valid(('binance', symbol, 'aggTrade'))
            await self.emit('binance', 'binance_trade', symbol, payload)
        except (ValueError, KeyError, TypeError, AttributeError):
            return

    def ws_url(self, source):
        if source == 'ondo':
            return self.settings.ondo_ws_url
        streams = sorted({self.settings.for_symbol(s).binance_symbol.lower() + '@aggTrade'
                          for s in self.settings.focus_symbols})
        return self.settings.binance_ws_url + '/'.join(streams)

    async def _heartbeat(self, ws):
        while True:
            await self.sleep(float(self.settings.ondo_ping_s))
            await ws.send('{"op":"ping"}')

    async def _receive(self, source, ws, opened):
        iterator = ws.__aiter__()
        while not self.stop.is_set():
            timeout = float(self.settings.silence_timeout_s)
            if source == 'binance':
                remaining = float(self.settings.binance_reconnect_s) - (self.monotonic() - opened)
                if remaining <= 0:
                    raise TimeoutError('连接达到主动轮换时限')
                timeout = min(timeout, remaining)
            incoming = asyncio.create_task(anext(iterator))
            timer = asyncio.create_task(self.sleep(timeout))
            processing = None
            try:
                done, _ = await asyncio.wait([incoming, timer], return_when=asyncio.FIRST_COMPLETED)
                if incoming not in done:
                    raise TimeoutError('WS 静默超时')
                raw = incoming.result()
                processing = asyncio.create_task(
                    self.handle_ondo(raw) if source == 'ondo' else self.handle_binance(raw))
                await asyncio.shield(processing)
            except asyncio.CancelledError:
                # Cancellation may arrive before wait resumes; messages already received must still finish enqueueing.
                if (processing is None and incoming.done() and not incoming.cancelled()
                        and incoming.exception() is None):
                    raw = incoming.result()
                    processing = asyncio.create_task(
                        self.handle_ondo(raw) if source == 'ondo' else self.handle_binance(raw))
                if processing is not None:
                    await processing
                raise
            finally:
                incoming.cancel()
                timer.cancel()
                await asyncio.gather(incoming, timer, return_exceptions=True)

    async def ws_loop(self, source):
        backoff = 1
        while not self.stop.is_set():
            before = self.counts[source]
            reason = 'WS 连接断开'
            try:
                self.connections[source] = uuid.uuid4().hex
                async with self.connect(self.ws_url(source), max_size=32768 if source == 'ondo' else 1048576,
                                        ping_interval=None if source == 'ondo' else 20,
                                        open_timeout=float(self.settings.http_timeout_s), close_timeout=5) as ws:
                    self.connected_since[source] = self.now_ms()
                    if source == 'ondo':
                        for channel in ('depthBooksPerps', 'tradesPerps'):
                            await ws.send(_json({'op': 'subscribe', 'channel': channel, 'limit': 100}))
                    receiver = asyncio.create_task(self._receive(source, ws, self.monotonic()))
                    heartbeat = asyncio.create_task(self._heartbeat(ws)) if source == 'ondo' else None
                    tasks = [receiver] + ([heartbeat] if heartbeat else [])
                    try:
                        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                        for task in done:
                            task.result()
                    finally:
                        for task in tasks:
                            task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                reason = 'WS 静默或轮换' if isinstance(exc, TimeoutError) else 'WS 连接断开'
            finally:
                self.connected_since.pop(source, None)
            if self.stop.is_set():
                break
            await self.open_gap((source, '*', '*'), reason)
            self.reconnects[source] += 1
            if self.counts[source] > before:
                backoff = 1
            await self.sleep(backoff)
            backoff = min(60, backoff * 2)

    async def http(self, url):
        for attempt in range(4):
            async with self.http_semaphore:
                async with self.http_lock:
                    if self.last_http is not None:
                        delay = self.settings.request_interval_ms / 1000 - (self.monotonic() - self.last_http)
                        if delay > 0:
                            await self.sleep(delay)
                    self.last_http = self.monotonic()
                try:
                    return await asyncio.to_thread(self.client.get_json, url)
                except RateLimited as exc:
                    if attempt == 3:
                        raise
                    delay = exc.retry_after_s
            await self.sleep(delay)

    async def _contracts_once(self):
        payload = await self.http(ONDO_API + '/contracts')
        if payload.get('success') is not True or not isinstance(payload.get('result'), list):
            raise MarketError('Ondo 合约列表无效')
        tradable = set()
        for row in payload['result']:
            try:
                market = row['market']
                validate_ondo_contract(payload, market)
                tradable.add(market)
            except (MarketError, KeyError, TypeError):
                continue
        for market in sorted(tradable):
            for channel in ('depthBooksPerps', 'tradesPerps'):
                key = ('ondo', market, channel)
                self.expected.add(key)
        self.tradable_markets = tradable
        await self.emit('ondo', 'contracts', None, payload)

    async def markets_once(self):
        payload = await self.http(ONDO_MARKETS)
        try:
            pairs = payload['result']['perps']['tradingPairs']
            valid = payload.get('success') is True and isinstance(pairs, list)
        except (KeyError, TypeError, AttributeError):
            valid = False
        if not valid:
            raise MarketError('Ondo 市场精度元数据无效')
        await self.emit('ondo', 'markets', None, payload)

    async def contracts_once(self):
        # Save each endpoint independently within a sampling cycle so one failure cannot discard the other's valid response.
        async def collect(channel, operation):
            try:
                await operation()
            except (MarketError, OSError, ValueError, KeyError, TypeError) as exc:
                await self.emit('ondo', 'error', None, {'error': str(exc), 'channel': channel})
        await asyncio.gather(collect('contracts', self._contracts_once),
                             collect('markets', self.markets_once))

    async def rest_scan_once(self):
        if not any(key[0] == 'ondo' and key[2] in ('*', 'depthBooksPerps') for key in self.gaps):
            return
        async def scan(market):
            try:
                payload = await self.http(ONDO_API + '/depth?' + urlencode({
                    'market': market, 'depth': self.settings.depth}))
                book = parse_ondo(payload, market, self.now_ms())
                # REST only supplements observations; it neither proves WS recovery nor closes WS gaps.
                await self.record_book(book, payload, source='rest')
            except (MarketError, OSError, ValueError, TypeError) as exc:
                await self.emit('ondo', 'error', market, {'error': str(exc), 'channel': 'rest_depth'})
        await asyncio.gather(*(scan(market) for market in sorted(self.tradable_markets)))

    async def contracts_loop(self):
        while not self.stop.is_set():
            try:
                await self.contracts_once()
            except (MarketError, OSError, ValueError, KeyError, TypeError) as exc:
                await self.emit('ondo', 'error', None, {'error': str(exc), 'channel': 'contracts'})
            await self.sleep(float(self.settings.contracts_interval_s))

    async def rest_loop(self):
        while not self.stop.is_set():
            await self.rest_scan_once()
            await self.sleep(float(self.settings.rest_scan_interval_s))

    async def refresh_lighter_metadata(self):
        self.last_lighter_meta = self.monotonic()
        try:
            payload = await self.http(LIGHTER_API + '/orderBooks')
            if payload.get('code') != 200 or not isinstance(payload.get('order_books'), list):
                raise MarketError('Lighter 市场元数据无效')
        except (MarketError, OSError, ValueError, TypeError) as exc:
            await self.emit('lighter', 'error', None, {'error': str(exc), 'channel': 'orderBooks'})
            return
        metadata, unsupported = {}, set()
        for symbol in self.settings.focus_symbols:
            key = ('lighter', symbol, 'orderBookOrders')
            try:
                metadata[symbol] = resolve_lighter(payload, symbol)
                self.expected.add(key)
            except MarketError:
                unsupported.add(symbol)
                self.expected.discard(key)
                self.market_stale.pop(key, None)
                self.market_stale_since.pop(key, None)
                if symbol not in self.lighter_warned:
                    await self.append_log([{'时间': datetime.fromtimestamp(
                        self.now_ms() / 1000, timezone.utc).isoformat(), '级别': 'WARNING',
                        '来源名称': 'lighter', '警告':
                        f'Lighter 不支持标的 {symbol}，已跳过轮询；等待定期元数据刷新'}])
                    self.lighter_warned.add(symbol)
        self.lighter_metadata, self.lighter_unsupported = metadata, unsupported

    async def lighter_loop(self):
        while not self.stop.is_set():
            if (self.last_lighter_meta is None or self.monotonic() - self.last_lighter_meta
                    >= float(self.settings.lighter_meta_refresh_s)):
                await self.refresh_lighter_metadata()
            async def poll(symbol):
                key = ('lighter', symbol, 'orderBookOrders')
                try:
                    market_id = self.lighter_metadata[symbol]['market_id']
                    payload = await self.http(LIGHTER_API + '/orderBookOrders?' + urlencode({
                        'market_id': market_id, 'limit': self.settings.depth}))
                    book = parse_lighter(payload, symbol, self.now_ms())
                    await self.valid(key)
                    await self.emit('lighter', 'depth', symbol, book.to_dict())
                except (MarketError, OSError, ValueError, KeyError, TypeError):
                    await self.emit('lighter', 'error', symbol,
                                    {'error': 'Lighter 单市场轮询失败', 'channel': 'orderBookOrders'})
            await asyncio.gather(*(poll(symbol) for symbol in self.lighter_metadata))
            await self.sleep(float(self.settings.lighter_poll_s))

    async def health_once(self):
        now = self.now_ms()
        elapsed_at = self.monotonic()
        stats = {source: {'消息数': self.counts[source],
                         '最近消息年龄秒': None if source not in self.source_last else
                         elapsed_at - self.source_last_monotonic[source],
                         '重连次数': self.reconnects[source],
                         '断档数': self.gap_counts[source],
                         '当前断档数': sum(key[0] == source for key in self.gaps),
                         'market_stale 数': self.stale_counts[source]}
                 for source in ('ondo', 'binance', 'lighter')}
        ages = {}
        for symbol in self.settings.focus_symbols:
            name = self.settings.for_symbol(symbol).binance_symbol
            seen = self.last_seen_monotonic.get(('binance', name, 'aggTrade'))
            ages[name] = None if seen is None else elapsed_at - seen
        at = datetime.fromtimestamp(now / 1000, timezone.utc).isoformat()
        lines = [{'时间': at, '来源': stats, '队列长度': self.queue.qsize(),
                  '断档数量': len(self.gaps), '币安各符号最近消息年龄秒': ages}]
        for source, opened in self.connected_since.items():
            # Reconnection must preserve evidence of ongoing market-data silence; only valid data resets this silence period.
            since = self.source_last_monotonic.get(source, self.started_monotonic)
            if elapsed_at - since >= float(self.settings.source_silence_warn_s):
                if self.silence_warned.get(source) == since:
                    continue
                self.silence_warned[source] = since
                warning = (f'{source} 连接保持，但已连续 {(elapsed_at - since) / 60:.1f} 分钟'
                           '无有效行情消息，请检查订阅和行情 WS 地址')
                lines.append({'时间': at, '级别': 'WARNING', '来源名称': source, '警告': warning})
        await self.append_log(lines, now)

    async def append_log(self, rows, now=None):
        now = self.now_ms() if now is None else now
        def append():
            folder = Path(self.settings.log_dir)
            folder.mkdir(parents=True, exist_ok=True)
            day = datetime.fromtimestamp(now / 1000, timezone.utc).strftime('%Y%m%d')
            with (folder / f'collect_{day}.log').open('a', encoding='utf-8') as stream:
                stream.write('\n'.join(_json(row) for row in rows) + '\n')
        await asyncio.to_thread(append)

    async def monitor_loop(self):
        last_health = None
        while not self.stop.is_set():
            await self.check_silence()
            if last_health is None or self.monotonic() - last_health >= 60:
                await self.health_once()
                await self.save_state()
                last_health = self.monotonic()
            await self.sleep(min(1, float(self.settings.silence_timeout_s)))

    def request_stop(self, signum=None):
        self.stop.set()

    async def writer_loop(self, writer):
        failed = False
        try:
            while True:
                row = await self.queue.get()
                if row is None:
                    self.queue.task_done()
                    break
                batch = [row]
                while not self.queue.empty() and len(batch) < 256:
                    batch.append(self.queue.get_nowait())
                try:
                    if not failed and await asyncio.to_thread(writer.write_batch, batch):
                        self.exit_code = 75
                        self.request_stop()
                except Exception as exc:
                    failed = True
                    self.exit_code = 2
                    print(f'错误：采集写盘失败：{exc}', file=sys.stderr)
                    self.request_stop()
                finally:
                    for _ in batch:
                        self.queue.task_done()
        finally:
            await asyncio.to_thread(writer.close)

    async def run(self):
        writer = await asyncio.to_thread(HourlyWriter, self.settings.data_dir,
                                         self.run_id, self.settings.max_disk_gb,
                                         self.settings.min_free_gb, monotonic=self.monotonic)
        if await asyncio.to_thread(writer.at_capacity):
            await asyncio.to_thread(writer.close)
            return 75
        loop = asyncio.get_running_loop()
        installed = []
        for signum in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(signum, self.request_stop, signum)
                installed.append(signum)
            except (NotImplementedError, RuntimeError):
                pass
        consumer = asyncio.create_task(self.writer_loop(writer))
        self.writer_active = True
        producers = []
        clean = False
        try:
            await self.start_state()
            producers = [asyncio.create_task(job) for job in (
                self.ws_loop('ondo'), self.ws_loop('binance'), self.contracts_loop(),
                self.rest_loop(), self.lighter_loop(), self.monitor_loop())]
            stopper = asyncio.create_task(self.stop.wait())
            try:
                done, _ = await asyncio.wait([stopper, consumer, *producers],
                                              return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
                if not self.stop.is_set():
                    raise RuntimeError('采集工作任务意外结束')
                clean = True
            finally:
                stopper.cancel()
                await asyncio.gather(stopper, return_exceptions=True)
        except Exception as exc:
            self.exit_code = 2
            print(f'错误：采集服务失败：{exc}', file=sys.stderr)
        finally:
            for task in producers:
                task.cancel()
            await asyncio.gather(*producers, return_exceptions=True)
            await self.queue.join()
            await self.queue.put(None)
            await consumer
            self.writer_active = False
            await self.save_state(clean=clean and self.exit_code != 2)
            for signum in installed:
                loop.remove_signal_handler(signum)
        return self.exit_code


def run_collect(settings, **dependencies):
    """Only collect requires the optional dependency; other offline commands do not require websockets."""
    if dependencies.get('connect') is None:
        try:
            from websockets import connect
        except ImportError:
            print("错误：实时采集需要 websockets，请运行 pip install '.[live]' 安装。", file=sys.stderr)
            return 2
        dependencies['connect'] = connect
    return asyncio.run(Collector(settings, **dependencies).run())
