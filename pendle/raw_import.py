"""Import backed-up public market data offline, preserving provenance, validation results, and actual coverage."""

import gzip
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .binance import _verify_checksum, parse_agg_trades_zip
from .bt_config import SymbolSettings
from .history import _integer, _ondo_trade
from .market import MarketError, number


def load_symbol_settings(store, settings, symbol):
    """Prefer trading precision from markets snapshots, with explicit per-asset overrides taking highest priority."""
    fallback = settings.for_symbol(symbol)
    tick, step = None, None
    for row in store.iter_contracts(fallback.ondo_market):
        if row['source'] == 'ondo_markets':
            tick = number(row['payload']['quoteIncrement'])
            step = number(row['payload']['baseIncrement'])
            if tick <= 0 or step <= 0:
                raise MarketError(f'{symbol} markets 快照中的交易精度必须为正')
    override = settings.symbol_overrides.get(symbol, {})
    tick, step = override.get('price_tick', tick), override.get('quantity_step', step)
    if tick is None or step is None:
        missing = '、'.join(name for name, value in (('价格', tick), ('数量', step)) if value is None)
        raise MarketError(f'{symbol} 缺少{missing}精度：请导入 Ondo markets 快照或设置标的精度覆盖')
    return SymbolSettings(symbol, tick, step, fallback.binance_symbol)


def _identity(path, params):
    digest = hashlib.sha256()
    if path.is_file():
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(block)
    digest.update(json.dumps(params, sort_keys=True).encode())
    return 'raw:' + digest.hexdigest()


def _record(store, source, path, params, *, received_ms=0, count=0, oldest=None,
            newest=None, complete=False, error=None, status=None):
    return store.record_fetch(source, params, started_ms=received_ms,
                              finished_ms=received_ms,
                              status=status or ('error' if error else
                                                ('ok' if complete else 'partial')),
                              coverage_start_ms=oldest, coverage_end_ms=newest,
                              row_count=count, complete=complete, error=error,
                              request_key=_identity(path, {'source': source, **params}))


def _import_markets(path, store):
    payload = json.loads(path.read_text())
    match = re.search(r'(\d{8})', path.name)
    received = int(datetime.strptime(match[1], '%Y%m%d').replace(
        tzinfo=timezone.utc).timestamp() * 1000) if match else 0
    count = store.upsert_markets(payload, received)
    _record(store, 'ondo_markets', path, {'raw_file': path.name}, received_ms=received,
            count=count, oldest=received, newest=received, complete=True)
    return count


def _import_ondo(path, market, start_ms, end_ms, store):
    params = {'market': market, 'start_ms': start_ms, 'end_ms': end_ms,
              'raw_file': path.name, 'offline': True, 'cursors': []}
    count, oldest, newest, received, error = 0, None, None, 0, None
    try:
        with gzip.open(path, 'rt', encoding='utf-8') as stream:
            for line_no, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                row = json.loads(line)
                received = _integer(row['received_ms'], 'received_ms')
                payload = row['payload']
                if (not isinstance(payload, dict) or payload.get('success') is not True
                        or not isinstance(payload.get('result'), list)):
                    raise MarketError(f'Ondo 原始文件第 {line_no} 行成交响应无效')
                rows = payload['result']
                parsed = [_ondo_trade(trade) for trade in rows]
                if any(trade[1] != market for trade in parsed):
                    raise MarketError(f'Ondo 原始文件第 {line_no} 行包含其他市场')
                if parsed:
                    low = min(trade[2] for trade in parsed)
                    high = max(trade[2] for trade in parsed)
                    oldest = low if oldest is None else min(oldest, low)
                    newest = high if newest is None else max(newest, high)
                count += store.upsert_ondo_trades(rows)
                params['cursors'].append(row.get('cursor'))
    except (ValueError, OSError, KeyError, TypeError, EOFError) as exc:
        error = f'Ondo 离线导入失败：{path.name}：{exc}'
    failed = error is not None
    complete = (error is None and oldest is not None
                and oldest < start_ms * 1000000 and newest >= end_ms * 1000000)
    if not complete and error is None:
        error = '历史数据未完整覆盖请求窗口'
    record = _record(store, 'ondo', path, params, received_ms=received, count=count,
                     oldest=None if oldest is None else oldest // 1000000,
                     newest=None if newest is None else newest // 1000000,
                     complete=complete, error=error,
                     status='error' if failed else ('ok' if complete else 'partial'))
    return count, record


def _import_binance(path, symbol, store):
    params = {'symbol': symbol, 'raw_file': path.name, 'offline': True}
    count, times, error = 0, [], None
    try:
        body = path.read_bytes()
        checksum = Path(str(path) + '.CHECKSUM').read_bytes()
        params['checksum_sha256'] = hashlib.sha256(checksum).hexdigest()
        _verify_checksum(body, checksum, path.name)
        rows = parse_agg_trades_zip(body, symbol)
        count = store.upsert_binance_trades(rows)
        times = [row['transact_time'] for row in rows]
    except (ValueError, OSError) as exc:
        error = f'币安离线导入失败：{path.name}：{exc}'
    record = _record(store, 'binance', path, params, count=count,
                     oldest=min(times) if times else None,
                     newest=max(times) if times else None,
                     complete=error is None, error=error)
    return count, record


def import_raw_history(raw_dir, symbols, start_ms, end_ms, store, settings):
    """Import complete local backups to retain warmup data; record per-asset failures and continue."""
    _integer(start_ms, 'start_ms')
    _integer(end_ms, 'end_ms')
    if end_ms <= start_ms:
        raise ValueError('历史导入结束时间必须晚于开始时间')
    raw_dir = Path(raw_dir)
    result = {'contracts': 0, 'ondo_trades': 0, 'binance_trades': 0,
              'errors': [], 'symbols': {}}
    market_paths = sorted(raw_dir.glob('ondo_markets_*.json'))
    if not market_paths:
        result['errors'].append('缺少 Ondo markets 原始快照')
    for path in market_paths:
        try:
            result['contracts'] += _import_markets(path, store)
        except (ValueError, OSError) as exc:
            message = f'Ondo markets 离线导入失败：{path.name}：{exc}'
            result['errors'].append(message)
            _record(store, 'ondo_markets', path, {'raw_file': path.name}, error=message)
    for symbol in symbols:
        resolved = settings.for_symbol(symbol)
        details = result['symbols'][symbol] = {'ondo': [], 'binance': []}
        ondo_paths = sorted((raw_dir / 'ondo_trades').glob(f'{symbol}_*.jsonl.gz'))
        binance_paths = sorted((raw_dir / 'binance').glob(
            f'{resolved.binance_symbol}-aggTrades-*.zip'))
        for kind, paths in (('ondo', ondo_paths), ('binance', binance_paths)):
            if not paths:
                message = f'{symbol} 缺少 {kind} 本地原始数据'
                result['errors'].append(message)
                path = raw_dir / kind / (symbol + '.missing')
                details[kind].append(_record(store, kind, path,
                                            {'symbol': symbol, 'offline': True}, error=message))
            for path in paths:
                if kind == 'ondo':
                    count, record = _import_ondo(path, resolved.ondo_market,
                                                 start_ms, end_ms, store)
                else:
                    count, record = _import_binance(path, resolved.binance_symbol, store)
                result[kind + '_trades'] += count
                details[kind].append(record)
                if record['error']:
                    result['errors'].append(f'{symbol}：{record["error"]}')
    return result
