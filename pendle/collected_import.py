"""逐行导入小时采集归档，容忍进程退出造成的 gzip 或 JSON 尾部截断。"""

import gzip
import json
from pathlib import Path

from .market import MarketError


def _lines(path, counts):
    """read1 先返回已解压内容，避免 gzip 尾部缺失时丢掉先前完整行。"""
    pending = b''
    opener = gzip.open if path.name.endswith('.gz') else open
    with opener(path, 'rb') as stream:
        while True:
            try:
                block = stream.read1(65536)
            except EOFError:
                counts['truncated_files'] += 1
                break
            if not block:
                break
            pending += block
            while b'\n' in pending:
                raw, pending = pending.split(b'\n', 1)
                yield raw + b'\n'
    if pending:
        yield pending


def _import_row(row, store, counts):
    kind, payload = row['kind'], row.get('payload')
    if kind == 'ondo_trade':
        counts['ondo_trades'] += store.upsert_ondo_trades([{**payload, 'received_ms': row['received_ms']}])
    elif kind == 'binance_trade':
        counts['binance_trades'] += store.upsert_binance_trades([{**payload, 'received_ms': row['received_ms']}])
    elif kind == 'depth_observation':
        counts['depth_observations'] += store.upsert_depth_observations([payload])
    elif kind == 'gap':
        counts['gaps'] += store.upsert_collector_gaps([payload])
    elif kind == 'contracts':
        counts['contracts'] += store.upsert_contracts(payload, row['received_ms'], row['source'])
    elif kind == 'markets':
        counts['contracts'] += store.upsert_markets(payload, row['received_ms'])
    elif kind in ('depth', 'error', 'market_stale'):
        counts['ignored'] += 1
        counts['ignored_by_kind'][kind] += 1
        if kind == 'market_stale':
            # 质量事件按归档行计数，开始和恢复各一条；不写入回测断档表。
            counts['market_stale'] += 1
    else:
        raise ValueError('采集记录 kind 无效')


def import_collected(src, store):
    """递归导入归档，重复导入仅计新增成交、观测、合约与断档实质更新。"""
    src = Path(src)
    if not src.exists():
        raise ValueError('采集文件或目录不存在：' + str(src))
    paths = (sorted(path for path in src.rglob('*') if path.is_file()
                    and (path.name.endswith('.jsonl.gz') or path.name.endswith('.jsonl')))
             if src.is_dir() else [src])
    counts = dict.fromkeys(('ondo_trades', 'binance_trades', 'depth_observations',
                           'gaps', 'contracts', 'market_stale', 'ignored',
                           'truncated_lines', 'truncated_files'), 0)
    counts['ignored_by_kind'] = dict.fromkeys(('depth', 'error', 'market_stale'), 0)
    for path in paths:
        for line_no, raw in enumerate(_lines(path, counts), 1):
            if not raw.strip():
                continue
            try:
                row = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                if not raw.endswith(b'\n'):
                    counts['truncated_lines'] += 1
                    continue
                raise ValueError(f'采集文件 {path.name} 第 {line_no} 行 JSON 无效') from exc
            try:
                if not isinstance(row, dict):
                    raise ValueError('采集记录必须是 JSON 对象')
                at = row.get('received_ms')
                if isinstance(at, bool) or not isinstance(at, int) or at < 0:
                    raise ValueError('received_ms 必须是非负整数')
                if not isinstance(row.get('source'), str) or not row['source']:
                    raise ValueError('采集记录 source 不能为空')
                _import_row(row, store, counts)
            except (KeyError, TypeError, ValueError, MarketError) as exc:
                raise ValueError(f'采集文件 {path.name} 第 {line_no} 行无效：{exc}') from exc
    return counts
