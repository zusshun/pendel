"""Transactional historical market-data storage, stable ordering, and public GET pagination."""

import json
import re
import sqlite3
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode

from .market import MarketError, RateLimited, number

ONDO_API = 'https://api.ondoperps.xyz/v1/perps'


def parse_time_ns(value):
    """Preserve all nanoseconds in ISO timestamps without converting through floating-point timestamps."""
    try:
        match = re.fullmatch(r'(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?(Z|[+-]\d\d:\d\d)', value)
        if not match:
            raise ValueError
        dt = datetime.fromisoformat(match[1] + match[3].replace('Z', '+00:00'))
        delta = dt.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
        result = (delta.days * 86400 + delta.seconds) * 1000000000
        result += int((match[2] or '').ljust(9, '0'))
        if not 0 <= result <= 9223372036854775807:
            raise ValueError
        return result
    except (TypeError, ValueError, OverflowError) as exc:
        raise MarketError('成交时间必须是有效的带时区纳秒 ISO 时间') from exc


def _integer(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MarketError(f'{name} 必须是非负整数')
    return value


def _received_ms(row):
    value = row.get('received_ms')
    return None if value is None else _integer(value, 'received_ms')


def _ondo_trade(row):
    if not isinstance(row, dict):
        raise MarketError('Ondo 成交必须是 JSON 对象')
    try:
        price, size, cost = (number(row[key]) for key in ('price', 'size', 'cost'))
        if price <= 0 or size < 0 or cost < 0:
            raise MarketError('成交价格必须为正，数量和金额不能为负')
        if row['aggressor_side'] not in ('buy', 'sell'):
            raise MarketError('成交主动方向无效')
        if not all(isinstance(row[k], str) and row[k] for k in ('id', 'market')):
            raise MarketError('成交 ID 与市场不能为空')
        return (row['id'], row['market'], parse_time_ns(row['time']), str(price),
                str(size), str(cost), row['aggressor_side'], json.dumps(row, ensure_ascii=False))
    except (KeyError, TypeError) as exc:
        raise MarketError('Ondo 成交字段缺失') from exc


class HistoryStore:
    def __init__(self, path, create=True):
        path = Path(path)
        if not create and not path.is_file():
            raise ValueError('历史数据库不存在：' + str(path))
        if create:
            if str(path) != ':memory:':
                path.parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(str(path))
            self.connection.executescript('''
                CREATE TABLE IF NOT EXISTS fetches (
                    id INTEGER PRIMARY KEY, source TEXT NOT NULL, params TEXT NOT NULL,
                    started_ms INTEGER NOT NULL, finished_ms INTEGER NOT NULL,
                    status TEXT NOT NULL, coverage_start_ms INTEGER, coverage_end_ms INTEGER,
                    error TEXT, row_count INTEGER NOT NULL, complete INTEGER NOT NULL,
                    request_key TEXT UNIQUE
                );
                CREATE TABLE IF NOT EXISTS ondo_trades (
                    id TEXT PRIMARY KEY, market TEXT NOT NULL, time_ns INTEGER NOT NULL,
                    price TEXT NOT NULL, size TEXT NOT NULL, cost TEXT NOT NULL,
                    aggressor_side TEXT NOT NULL, payload TEXT NOT NULL,
                    received_ms INTEGER
                );
                CREATE INDEX IF NOT EXISTS ondo_trade_time ON ondo_trades(market,time_ns,id);
                CREATE TABLE IF NOT EXISTS binance_trades (
                    symbol TEXT NOT NULL, agg_id INTEGER NOT NULL, price TEXT NOT NULL,
                    quantity TEXT NOT NULL, first_trade_id INTEGER NOT NULL,
                    last_trade_id INTEGER NOT NULL, transact_time INTEGER NOT NULL,
                    is_buyer_maker INTEGER NOT NULL, received_ms INTEGER,
                    PRIMARY KEY(symbol,agg_id)
                );
                CREATE INDEX IF NOT EXISTS binance_trade_time
                    ON binance_trades(symbol,transact_time,agg_id);
                CREATE TABLE IF NOT EXISTS contracts (
                    market TEXT NOT NULL, source TEXT NOT NULL, received_ms INTEGER NOT NULL,
                    payload TEXT NOT NULL, PRIMARY KEY(market,source,received_ms)
                );
                CREATE TABLE IF NOT EXISTS depth_observations (
                    market TEXT NOT NULL, source TEXT NOT NULL, received_ms INTEGER NOT NULL,
                    mid TEXT NOT NULL, bid TEXT NOT NULL, ask TEXT NOT NULL,
                    spread_bps TEXT NOT NULL, bid_last_distance_bps TEXT NOT NULL,
                    ask_last_distance_bps TEXT NOT NULL, book_band_complete INTEGER NOT NULL,
                    flat_bid_usd TEXT NOT NULL, flat_ask_usd TEXT NOT NULL,
                    linear_bid_usd TEXT NOT NULL, linear_ask_usd TEXT NOT NULL,
                    tick_depth TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(market,source,received_ms)
                );
                CREATE INDEX IF NOT EXISTS depth_observation_time
                    ON depth_observations(market,received_ms);
                CREATE TABLE IF NOT EXISTS collector_gaps (
                    source TEXT NOT NULL, market TEXT NOT NULL, channel TEXT NOT NULL,
                    start_ms INTEGER NOT NULL, end_ms INTEGER, reason TEXT NOT NULL,
                    PRIMARY KEY(source,market,channel,start_ms)
                );
            ''')
            # Support legacy tables when writing; read-only access leaves old databases unchanged.
            for table in ('ondo_trades', 'binance_trades'):
                columns = {row[1] for row in self.connection.execute(f'PRAGMA table_info({table})')}
                if 'received_ms' not in columns:
                    self.connection.execute(f'ALTER TABLE {table} ADD COLUMN received_ms INTEGER')
            self.connection.commit()
        else:
            self.connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
        self.connection.row_factory = sqlite3.Row

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.connection.close()

    def upsert_ondo_trades(self, rows):
        """Keep each trade's earliest known receipt time; live captures can fill missing historical values."""
        before = self.connection.total_changes
        with self.connection:
            self.connection.executemany('''INSERT INTO ondo_trades
                (id,market,time_ns,price,size,cost,aggressor_side,payload,received_ms)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET received_ms=excluded.received_ms
                WHERE excluded.received_ms IS NOT NULL AND
                    (ondo_trades.received_ms IS NULL OR
                     excluded.received_ms<ondo_trades.received_ms)''',
                ((*_ondo_trade(row), _received_ms(row)) for row in rows))
        return self.connection.total_changes - before

    def iter_ondo_trades(self, market, start_ms=None, end_ms=None):
        sql, params = 'SELECT * FROM ondo_trades WHERE market=?', [market]
        if start_ms is not None:
            sql += ' AND time_ns>=?'
            params.append(_integer(start_ms, 'start_ms') * 1000000)
        if end_ms is not None:
            sql += ' AND time_ns<?'
            params.append(_integer(end_ms, 'end_ms') * 1000000)
        for raw in self.connection.execute(sql + ' ORDER BY time_ns,id', params):
            row = dict(raw)
            row.setdefault('received_ms', None)
            row['payload'] = json.loads(row['payload'])
            for key in ('price', 'size', 'cost'):
                row[key] = Decimal(row[key])
            yield row

    def upsert_binance_trades(self, rows):
        def values(row):
            price, quantity = number(row['price']), number(row['quantity'])
            if price <= 0 or quantity < 0 or not isinstance(row['is_buyer_maker'], bool):
                raise MarketError('币安成交价格、数量或主动方向无效')
            ids = [_integer(row[k], k) for k in ('agg_id', 'first_trade_id', 'last_trade_id', 'transact_time')]
            if ids[2] < ids[1] or not isinstance(row['symbol'], str) or not row['symbol']:
                raise MarketError('币安成交 ID 或标的无效')
            return (row['symbol'], ids[0], str(price), str(quantity), *ids[1:],
                    int(row['is_buyer_maker']), _received_ms(row))
        before = self.connection.total_changes
        with self.connection:
            self.connection.executemany('''INSERT INTO binance_trades
                (symbol,agg_id,price,quantity,first_trade_id,last_trade_id,transact_time,
                 is_buyer_maker,received_ms) VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(symbol,agg_id) DO UPDATE SET received_ms=excluded.received_ms
                WHERE excluded.received_ms IS NOT NULL AND
                    (binance_trades.received_ms IS NULL OR
                     excluded.received_ms<binance_trades.received_ms)''',
                (values(row) for row in rows))
        return self.connection.total_changes - before

    def iter_binance_trades(self, symbol, start_ms=None, end_ms=None):
        sql, params = 'SELECT * FROM binance_trades WHERE symbol=?', [symbol]
        for value, op in ((start_ms, '>='), (end_ms, '<')):
            if value is not None:
                sql += f' AND transact_time{op}?'
                params.append(_integer(value, '成交时间'))
        for raw in self.connection.execute(sql + ' ORDER BY transact_time,agg_id', params):
            row = dict(raw)
            row.setdefault('received_ms', None)
            row['price'], row['quantity'] = Decimal(row['price']), Decimal(row['quantity'])
            row['is_buyer_maker'] = bool(row['is_buyer_maker'])
            yield row

    def record_fetch(self, source, params, *, started_ms, finished_ms, status,
                     coverage_start_ms=None, coverage_end_ms=None, error=None,
                     row_count=0, complete=False, request_key=None):
        values = (source, json.dumps(params, ensure_ascii=False), started_ms, finished_ms,
                  status, coverage_start_ms, coverage_end_ms, error, row_count,
                  int(complete), request_key)
        with self.connection:
            cursor = self.connection.execute('''INSERT OR IGNORE INTO fetches
                (source,params,started_ms,finished_ms,status,coverage_start_ms,
                 coverage_end_ms,error,row_count,complete,request_key)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)''', values)
        if request_key is None:
            raw = self.connection.execute('SELECT * FROM fetches WHERE id=?', (cursor.lastrowid,)).fetchone()
        else:
            raw = self.connection.execute('SELECT * FROM fetches WHERE request_key=?', (request_key,)).fetchone()
        return self._fetch_row(raw)

    @staticmethod
    def _fetch_row(raw):
        row = dict(raw)
        row['params'] = json.loads(row['params'])
        row['complete'] = bool(row['complete'])
        return row

    def iter_fetches(self, source=None):
        sql = 'SELECT * FROM fetches'
        params = () if source is None else (source,)
        if source is not None:
            sql += ' WHERE source=?'
        for raw in self.connection.execute(sql + ' ORDER BY id', params):
            yield self._fetch_row(raw)

    def upsert_contracts(self, payload, received_ms, source='ondo'):
        _integer(received_ms, 'received_ms')
        if (not isinstance(payload, dict) or payload.get('success') is not True
                or not isinstance(payload.get('result'), list)):
            raise MarketError('Ondo 合约快照格式无效')
        before = self.connection.total_changes
        with self.connection:
            for row in payload['result']:
                if (not isinstance(row, dict) or not isinstance(row.get('market'), str)
                        or not row['market']):
                    raise MarketError('合约快照缺少市场')
                self.connection.execute('INSERT OR IGNORE INTO contracts VALUES (?,?,?,?)',
                                        (row['market'], source, received_ms,
                                         json.dumps(row, ensure_ascii=False)))
        return self.connection.total_changes - before

    def upsert_markets(self, payload, received_ms):
        """Share markets precision validation and storage between raw history and live capture archives."""
        try:
            pairs = payload['result']['perps']['tradingPairs']
            if payload.get('success') is not True or not isinstance(pairs, list):
                raise ValueError
            for row in pairs:
                if (not isinstance(row, dict) or not isinstance(row.get('market'), str)
                        or not row['market'] or number(row['quoteIncrement']) <= 0
                        or number(row['baseIncrement']) <= 0):
                    raise ValueError
        except (KeyError, TypeError, ValueError) as exc:
            raise MarketError('Ondo markets 快照或交易精度无效') from exc
        return self.upsert_contracts({'success': True, 'result': pairs}, received_ms,
                                     source='ondo_markets')

    def iter_contracts(self, market=None):
        sql, params = 'SELECT * FROM contracts', ()
        if market is not None:
            sql += ' WHERE market=?'
            params = (market,)
        for raw in self.connection.execute(sql + ' ORDER BY received_ms,market', params):
            row = dict(raw)
            row['payload'] = json.loads(row['payload'])
            yield row


    def upsert_depth_observations(self, rows):
        columns = ('market', 'source', 'received_ms', 'mid', 'bid', 'ask', 'spread_bps',
                   'bid_last_distance_bps', 'ask_last_distance_bps', 'book_band_complete',
                   'flat_bid_usd', 'flat_ask_usd', 'linear_bid_usd', 'linear_ask_usd',
                   'tick_depth', 'payload')
        def values(row):
            _integer(row['received_ms'], 'received_ms')
            result = []
            for key in columns:
                value = row.get(key, {}) if key == 'payload' else row[key]
                if key in ('tick_depth', 'payload'):
                    value = json.dumps(value, ensure_ascii=False)
                elif isinstance(value, Decimal):
                    value = str(value)
                elif key == 'book_band_complete':
                    value = int(value)
                result.append(value)
            return result
        before = self.connection.total_changes
        with self.connection:
            self.connection.executemany('INSERT OR IGNORE INTO depth_observations VALUES ('
                                        + ','.join('?' for _ in columns) + ')',
                                        (values(row) for row in rows))
        return self.connection.total_changes - before

    def iter_depth_observations(self, market=None, start_ms=None, end_ms=None):
        sql, params = 'SELECT * FROM depth_observations WHERE 1=1', []
        for column, value, op in (('market', market, '='), ('received_ms', start_ms, '>='),
                                   ('received_ms', end_ms, '<')):
            if value is not None:
                sql += f' AND {column}{op}?'
                params.append(value)
        for raw in self.connection.execute(sql + ' ORDER BY received_ms,market,source', params):
            row = dict(raw)
            for key in ('mid', 'bid', 'ask', 'spread_bps', 'bid_last_distance_bps',
                        'ask_last_distance_bps', 'flat_bid_usd', 'flat_ask_usd',
                        'linear_bid_usd', 'linear_ask_usd'):
                row[key] = Decimal(row[key])
            row['book_band_complete'] = bool(row['book_band_complete'])
            for key in ('tick_depth', 'payload'):
                row[key] = json.loads(row[key])
            yield row

    def upsert_collector_gaps(self, rows):
        """Closing records can complete open gaps; duplicate or older records cannot reopen them."""
        def values(row):
            try:
                for key in ('source', 'market', 'channel', 'reason'):
                    if not isinstance(row[key], str) or not row[key]:
                        raise ValueError
                start = _integer(row['start_ms'], '断档开始时间')
                end = row.get('end_ms')
                if end is not None and _integer(end, '断档结束时间') < start:
                    raise ValueError
                return (row['source'], row['market'], row['channel'], start, end, row['reason'])
            except (KeyError, TypeError, ValueError, MarketError) as exc:
                raise ValueError('采集断档的来源、市场、频道或时间无效') from exc
        before = self.connection.total_changes
        with self.connection:
            self.connection.executemany('''INSERT INTO collector_gaps VALUES (?,?,?,?,?,?)
                ON CONFLICT(source,market,channel,start_ms) DO UPDATE SET
                    end_ms=excluded.end_ms, reason=excluded.reason
                WHERE excluded.end_ms IS NOT NULL AND
                    (collector_gaps.end_ms IS NULL OR excluded.end_ms>collector_gaps.end_ms)''',
                (values(row) for row in rows))
        return self.connection.total_changes - before

    def iter_collector_gaps(self, source=None, market=None, channel=None,
                            start_ms=None, end_ms=None):
        """Filter by exact source, market, and channel, returning gaps that intersect the half-open window."""
        sql, params = 'SELECT * FROM collector_gaps WHERE 1=1', []
        for column, value in (('source', source), ('market', market), ('channel', channel)):
            if value is not None:
                sql += f' AND {column}=?'
                params.append(value)
        if start_ms is not None:
            sql += ' AND (end_ms IS NULL OR end_ms>?)'
            params.append(_integer(start_ms, 'start_ms'))
        if end_ms is not None:
            sql += ' AND start_ms<?'
            params.append(_integer(end_ms, 'end_ms'))
        for raw in self.connection.execute(sql + ' ORDER BY start_ms,source,market,channel', params):
            yield dict(raw)


def _request_json(client, url, *, clock, sleep, request_interval_ms, max_retries, last_request):
    for attempt in range(max_retries + 1):
        now = clock()
        if last_request[0] is not None:
            remaining = request_interval_ms / 1000 - (now - last_request[0])
            if remaining > 0:
                sleep(remaining)
        last_request[0] = clock()
        try:
            return client.get_json(url)
        except RateLimited as exc:
            if attempt == max_retries:
                raise
            sleep(exc.retry_after_s)


def fetch_ondo_trades(client, market, start_ms, end_ms, store, *, clock=time.time,
                      sleep=time.sleep, request_interval_ms=200, max_retries=3,
                      limit=1000, base_url=ONDO_API):
    """Store a half-open window; preserve completed pages on failure and return auditable coverage records."""
    for key, value in (('start_ms', start_ms), ('end_ms', end_ms),
                       ('request_interval_ms', request_interval_ms), ('max_retries', max_retries)):
        _integer(value, key)
    if end_ms <= start_ms or not 1 <= limit <= 1000:
        raise ValueError('抓取窗口或每页数量无效')
    started_ms = int(clock() * 1000)
    params = {'market': market, 'start_ms': start_ms, 'end_ms': end_ms,
              'limit': limit, 'request_interval_ms': request_interval_ms, 'cursors': []}
    cursor, seen, last_request = None, set(), [None]
    oldest, newest, count, crossed = None, None, 0, False
    error, status = None, 'partial'
    try:
        while True:
            query = {'market': market, 'limit': limit}
            if cursor is not None:
                query['cursor'] = cursor
            params['cursors'].append(cursor)
            payload = _request_json(client, base_url + '/trades?' + urlencode(query),
                                    clock=clock, sleep=sleep, request_interval_ms=request_interval_ms,
                                    max_retries=max_retries, last_request=last_request)
            if not isinstance(payload, dict) or payload.get('success') is not True:
                raise MarketError('Ondo 成交接口返回失败标识')
            rows = payload['result']
            if not isinstance(rows, list):
                raise MarketError('Ondo 成交列表格式无效')
            page_info = payload.get('pageInfo', {})
            if not isinstance(page_info, dict):
                raise MarketError('Ondo 成交分页信息格式无效')
            parsed = [_ondo_trade(row) for row in rows]
            if any(row[1] != market for row in parsed):
                raise MarketError('Ondo 成交返回了其他市场')
            if parsed:
                page_oldest = min(row[2] for row in parsed)
                page_newest = max(row[2] for row in parsed)
                oldest = page_oldest if oldest is None else min(oldest, page_oldest)
                newest = page_newest if newest is None else max(newest, page_newest)
                count += store.upsert_ondo_trades(
                    [row for row, values in zip(rows, parsed)
                     if start_ms * 1000000 <= values[2] < end_ms * 1000000])
                crossed = oldest < start_ms * 1000000
            if crossed:
                break
            cursor = page_info.get('nextCursor')
            if not cursor:
                break
            if not isinstance(cursor, str) or cursor in seen:
                raise MarketError('Ondo 翻页游标无效或重复')
            seen.add(cursor)
        complete = crossed and newest >= end_ms * 1000000
        if complete:
            status = 'ok'
        else:
            error = '历史数据未完整覆盖请求窗口'
    except (MarketError, KeyError, TypeError, OSError) as exc:
        status, error, complete = 'error', str(exc), False
    return store.record_fetch('ondo', params, started_ms=started_ms,
                              finished_ms=int(clock() * 1000), status=status,
                              coverage_start_ms=None if oldest is None else oldest // 1000000,
                              coverage_end_ms=None if newest is None else newest // 1000000,
                              row_count=count, complete=complete, error=error)


def fetch_contracts(client, store, *, clock=time.time, sleep=time.sleep,
                    request_interval_ms=200, max_retries=3, base_url=ONDO_API):
    """Save the complete contract snapshot and fetch result."""
    started = int(clock() * 1000)
    try:
        payload = _request_json(client, base_url + '/contracts', clock=clock, sleep=sleep,
                                request_interval_ms=request_interval_ms,
                                max_retries=max_retries, last_request=[None])
        received = int(clock() * 1000)
        count = store.upsert_contracts(payload, received)
    except (MarketError, KeyError, TypeError, OSError) as exc:
        return store.record_fetch('ondo_contracts', {}, started_ms=started,
                                  finished_ms=int(clock() * 1000), status='error', error=str(exc))
    return store.record_fetch('ondo_contracts', {}, started_ms=started, finished_ms=received,
                              status='ok', coverage_start_ms=received,
                              coverage_end_ms=received, row_count=count, complete=True)
