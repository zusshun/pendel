"""导入公开扫描 JSONL，并保存两种奖励权重下的盘口竞争指标。"""

import hashlib
import json
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path

from .market import MarketError, number, parse_ondo
from .reward import distance_bps, quote_weight


def depth_observation(book, *, price_tick='.01', band_bps=25, max_tick=10,
                      source='scan'):
    """第 k 档以 mid 两侧最近合法价格为第零档，保存数量与 USD 深度。"""
    tick, band = number(price_tick), number(band_bps)
    if tick <= 0 or band <= 0:
        raise ValueError('price_tick 和 band_bps 必须为正')
    if isinstance(max_tick, bool) or not isinstance(max_tick, int) or not 0 <= max_tick <= 10:
        raise ValueError('max_tick 必须在 0–10 之间')
    mid = book.mid
    result = {'market': book.symbol + '-USD.P', 'source': source,
              'received_ms': book.received_ms, 'mid': mid,
              'bid': book.bids[0][0], 'ask': book.asks[0][0],
              'spread_bps': (book.asks[0][0] - book.bids[0][0]) / mid * 10000,
              'bid_last_distance_bps': distance_bps(book.bids[-1][0], mid),
              'ask_last_distance_bps': distance_bps(book.asks[-1][0], mid),
              'tick_depth': {},
              'payload': {'band_bps': str(band), 'price_tick': str(tick), 'max_tick': max_tick}}
    result['book_band_complete'] = (result['bid_last_distance_bps'] > band
                                    and result['ask_last_distance_bps'] > band)
    for side, levels, rounding, sign in (('bid', book.bids, ROUND_FLOOR, -1),
                                          ('ask', book.asks, ROUND_CEILING, 1)):
        for weight in ('flat', 'linear'):
            result[f'{weight}_{side}_usd'] = sum(
                (p * q * quote_weight(p, mid, band, weight) for p, q in levels), Decimal(0))
        grid = (mid / tick).to_integral_value(rounding=rounding) * tick
        sizes = dict(levels)
        ticks = []
        for k in range(max_tick + 1):
            price = grid + sign * k * tick
            quantity = sizes.get(price, Decimal(0))
            ticks.append({'tick': k, 'price': str(price), 'quantity': str(quantity),
                          'usd': str(price * quantity)})
        result['tick_depth'][side + 's'] = ticks
    return result


def _scan_error(store, row, error):
    params = {'kind': row['kind'], 'market': row.get('market'), 'received_ms': row['received_ms']}
    key = hashlib.sha256(json.dumps([params, error], sort_keys=True,
                                    ensure_ascii=False).encode()).hexdigest()
    before = store.connection.total_changes
    store.record_fetch('scan', params, started_ms=row['received_ms'],
                        finished_ms=row['received_ms'], status='error', error=error,
                        request_key='scan:' + key)
    return int(store.connection.total_changes > before)


def import_scan(src, store, *, price_tick='.01', band_bps=25, max_tick=10,
                 symbol_overrides=None):
    """逐行导入，只有末尾未结束且无法解析的半行会被跳过。"""
    src = Path(src)
    paths = sorted(src.glob('*.jsonl')) if src.is_dir() else [src]
    counts = {'contracts': 0, 'depth': 0, 'errors': 0, 'truncated_lines': 0}
    for path in paths:
        with path.open('rb') as stream:
            for line_no, raw in enumerate(stream, 1):
                if not raw.strip():
                    continue
                try:
                    row = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    if not raw.endswith(b'\n'):
                        counts['truncated_lines'] += 1
                        continue
                    raise ValueError(f'扫描文件 {path.name} 第 {line_no} 行 JSON 无效') from exc
                if not isinstance(row, dict) or row.get('kind') not in ('contracts', 'depth', 'error'):
                    raise ValueError(f'扫描文件 {path.name} 第 {line_no} 行 kind 无效')
                at = row.get('received_ms')
                if isinstance(at, bool) or not isinstance(at, int) or at < 0:
                    raise ValueError(f'扫描文件 {path.name} 第 {line_no} 行 received_ms 无效')
                try:
                    if row['kind'] == 'error':
                        if not isinstance(row.get('error'), str) or not row['error']:
                            raise ValueError('扫描错误记录缺少 error')
                        counts['errors'] += _scan_error(store, row, row['error'])
                    elif row['kind'] == 'contracts':
                        counts['contracts'] += store.upsert_contracts(row['payload'], at, source='scan')
                    else:
                        market = row['market']
                        book = parse_ondo(row['payload'], market, at)
                        symbol = market.removesuffix('-USD.P')
                        override = (symbol_overrides or {}).get(symbol, {})
                        tick = override.get('price_tick', price_tick)
                        observation = depth_observation(book, price_tick=tick, band_bps=band_bps,
                                                        max_tick=max_tick)
                        observation['payload'].update(source_file=path.name, source_line=line_no)
                        counts['depth'] += store.upsert_depth_observations([observation])
                except (MarketError, KeyError, TypeError) as exc:
                    counts['errors'] += _scan_error(store, row, str(exc))
    return counts
