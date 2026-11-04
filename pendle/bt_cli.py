"""Historical research commands, separate from existing recording and replay entry points."""

import json
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from .bt_config import BacktestSettings
from .history import HistoryStore, parse_time_ns


COMMANDS = ('fetch-history', 'import-scan', 'competition', 'backtest')


def register_commands(commands):
    """Add subcommands without changing existing command arguments."""
    descriptions = ('抓取公开历史或离线导入原始归档', '导入盘口扫描 JSONL',
                    '生成分时竞争排名', '运行确定性历史奖励回测')
    for name, description in zip(COMMANDS, descriptions):
        sub = commands.add_parser(name, help=description, description=description)
        sub.add_argument('--db', type=Path, default=Path('data/history.sqlite'), help='历史数据库')
        sub.add_argument('--config', type=Path, help='回测 TOML 配置')
        if name in ('fetch-history', 'backtest'):
            sub.add_argument('--symbols', help='逗号分隔的标的；默认使用配置')
            sub.add_argument('--start', required=True, help='带时区的 ISO 起始时间（含）')
            sub.add_argument('--end', required=True, help='带时区的 ISO 结束时间（不含）')
        if name == 'backtest':
            sub.add_argument('--events', action='store_true', help='输出完整回测事件及分钟明细')
        if name == 'fetch-history':
            sub.add_argument('--raw-dir', type=Path, help='仅从本地原始归档导入，不联网')
        if name == 'import-scan':
            sub.add_argument('--src', type=Path, required=True, help='扫描 JSONL 文件或目录')
        if name in ('competition', 'backtest'):
            sub.add_argument('--report', type=Path, help='JSON 输出文件')
            sub.add_argument('--summary', type=Path, help='中文 Markdown 输出文件')


def json_value(value):
    """Preserve decimal precision by serializing monetary amounts as JSON strings."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_value(item) for item in value]
    return value


def _write_markdown(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.pendle-', delete=False) as stream:
            temporary = stream.name
            stream.write(content)
        os.replace(temporary, path)
    finally:
        if temporary and Path(temporary).exists():
            Path(temporary).unlink()


def _guard_outputs(args, protect):
    outputs = [getattr(args, name, None) for name in ('report', 'summary')]
    for output in outputs:
        protect(output, args.db, args.config)
    if all(outputs):
        protect(outputs[0], outputs[1], None)


def _settings(args):
    settings = BacktestSettings.from_file(args.config) if args.config else BacktestSettings()
    if getattr(args, 'symbols', None) is not None:
        symbols = tuple(item.strip() for item in args.symbols.split(','))
        settings = replace(settings, symbols=symbols)
    return settings


def _precision(store, settings, symbols):
    from .raw_import import load_symbol_settings
    overrides = {symbol: dict(value) for symbol, value in settings.symbol_overrides.items()}
    for symbol in symbols:
        resolved = load_symbol_settings(store, settings, symbol)
        overrides[symbol] = {'price_tick': resolved.price_tick,
                             'quantity_step': resolved.quantity_step,
                             'binance_symbol': resolved.binance_symbol}
    return replace(settings, symbol_overrides=overrides)


def _fetch(args, settings, start_ms, end_ms):
    with HistoryStore(args.db) as store:
        if args.raw_dir is not None:
            from .raw_import import import_raw_history
            return import_raw_history(args.raw_dir, settings.symbols, start_ms, end_ms,
                                      store, settings)
        from .binance import BinaryHttpClient, fetch_binance_day
        from .history import fetch_contracts, fetch_ondo_trades
        client = BinaryHttpClient(timeout_s=float(settings.http_timeout_s))
        results = [fetch_contracts(client, store,
                                  request_interval_ms=settings.request_interval_ms)]
        day = datetime.fromtimestamp(start_ms / 1000, timezone.utc).date()
        final_day = datetime.fromtimestamp((end_ms - 1) / 1000, timezone.utc).date()
        for symbol in settings.symbols:
            identity = settings.for_symbol(symbol)
            results.append(fetch_ondo_trades(client, identity.ondo_market, start_ms, end_ms,
                store, request_interval_ms=settings.request_interval_ms))
            current = day
            while current <= final_day:
                try:
                    results.append(fetch_binance_day(client, identity.binance_symbol,
                        current, store, request_interval_ms=settings.request_interval_ms))
                except (ValueError, OSError):
                    results.append(list(store.iter_fetches('binance'))[-1])
                current += timedelta(days=1)
        return {'fetches': results, 'errors': sum(row['status'] != 'ok' for row in results)}


def _backtests(store, settings, start_ms, end_ms, *, include_events=False):
    from .backtest import run_backtest
    from .bt_report import build_report
    reports = []
    for symbol in settings.symbols:
        try:
            symbol_settings = _precision(store, settings, (symbol,))
        except ValueError as exc:
            reports.extend({'symbol': symbol, 'hedge_mode': mode, 'ref_fallback': fallback,
                            'status': 'rejected', 'reason': str(exc)}
                           for fallback in ('ondo_mid', 'none')
                           for mode in ('none', 'lighter_proxy'))
            continue
        identity = symbol_settings.for_symbol(symbol)
        # Use full history to warm up both sides; the replay clock controls individual sample validity.
        # Exclude data after the end time to prevent lookahead in quoting and mapping validation.
        ondo = list(store.iter_ondo_trades(identity.ondo_market, end_ms=end_ms))
        binance = list(store.iter_binance_trades(identity.binance_symbol, end_ms=end_ms))
        observations = list(store.iter_depth_observations(identity.ondo_market))
        gaps = [row for market in dict.fromkeys((identity.ondo_market, identity.binance_symbol, symbol, '*'))
                for row in store.iter_collector_gaps(market=market,
                    start_ms=start_ms, end_ms=end_ms)]
        fetches = [row for row in store.iter_fetches()
                   if row['params'].get('market') == identity.ondo_market
                   or row['params'].get('symbol') in (symbol, identity.binance_symbol)]
        for fallback, hedge_mode in ((fallback, mode) for fallback in ('ondo_mid', 'none')
                                     for mode in ('none', 'lighter_proxy')):
            mode_settings = replace(symbol_settings, hedge_mode=hedge_mode, ref_fallback=fallback)
            try:
                result = run_backtest(iter(ondo), iter(binance), mode_settings, symbol=symbol,
                    start_ms=start_ms, end_ms=end_ms, depth_observations=observations, gaps=gaps)
                sensitivity = []
                ratios = mode_settings.at_price_fill_ratios if fallback == 'ondo_mid' else ()
                for ratio in ratios:
                    scenario_settings = replace(mode_settings, at_price_fill_ratio=ratio)
                    scenario = result if ratio == mode_settings.at_price_fill_ratio else run_backtest(
                        iter(ondo), iter(binance), scenario_settings, symbol=symbol,
                        start_ms=start_ms, end_ms=end_ms, depth_observations=observations, gaps=gaps)
                    sensitivity.append({'ratio': ratio, 'result': scenario})
                result['coverage'].update(_fetch_coverage(fetches, identity, start_ms, end_ms,
                                                         result['coverage'].get('gaps', gaps)))
                report = build_report(result, mode_settings, depth_observations=observations,
                                      binance_trades=binance, sensitivity=sensitivity)
                report.update(symbol=symbol, hedge_mode=hedge_mode, ref_fallback=fallback, status='ok')
                report['precision'] = {'price_tick': identity.price_tick,
                                       'quantity_step': identity.quantity_step,
                                       'binance_symbol': identity.binance_symbol}
                report['fetches'] = fetches
                if not include_events:
                    for field in ('events', 'ondo_trades', 'binance_prices', 'center_prices'):
                        report.pop(field, None)
                    # After aggregation, retain only minute fields needed to recalculate rewards and track quote state.
                    minute_fields = ('time_ms', 'my_weight', 'two_sided', 'online',
                                     'guard_paused', 'center', 'reward_mid')
                    report['minutes'] = [{field: minute[field] for field in minute_fields
                                          if field in minute}
                                         for minute in report.get('minutes', ())]
                reports.append(report)
            except ValueError as exc:
                reports.append({'symbol': symbol, 'hedge_mode': hedge_mode, 'ref_fallback': fallback,
                                'status': 'rejected', 'reason': str(exc)})
    return {'start_ms': start_ms, 'end_ms': end_ms, 'default_ref_fallback': 'ondo_mid',
            'ref_fallbacks': ['ondo_mid', 'none'], 'reports': reports}


def _fetch_coverage(fetches, identity, start_ms, end_ms, gaps):
    """Establish full-window coverage from fetch provenance; sparse trades alone do not imply capture gaps."""
    ondo = any(row['source'] == 'ondo' and row['complete']
               and row['params'].get('market') == identity.ondo_market
               and row['params'].get('start_ms', end_ms) <= start_ms
               and row['params'].get('end_ms', start_ms) >= end_ms for row in fetches)
    day = datetime.fromtimestamp(start_ms / 1000, timezone.utc).date()
    last = datetime.fromtimestamp((end_ms - 1) / 1000, timezone.utc).date()
    expected = set()
    while day <= last:
        expected.add(day.isoformat())
        day += timedelta(days=1)
    available = set()
    for row in fetches:
        if row['source'] != 'binance' or not row['complete']:
            continue
        params = row['params']
        if params.get('symbol') != identity.binance_symbol:
            continue
        date = params.get('date')
        match = re.search(r'(\d{4}-\d\d-\d\d)\.zip$', params.get('raw_file', ''))
        if date or match:
            available.add(date or match[1])
    binance = expected <= available
    return {'complete': ondo and binance and not gaps,
            'source_fetch_coverage': {'ondo': ondo, 'binance': binance},
            'missing_binance_dates': sorted(expected - available)}


def dispatch(args, *, output_json, protect_inputs):
    """Run standalone historical commands using the main CLI's Chinese exceptions and exit-code conventions."""
    _guard_outputs(args, protect_inputs)
    settings = _settings(args)
    start_ms = end_ms = None
    if args.command in ('fetch-history', 'backtest'):
        start_ms = parse_time_ns(args.start) // 1000000
        end_ms = parse_time_ns(args.end) // 1000000
        if start_ms >= end_ms:
            raise ValueError('起始时间必须早于结束时间')
    if args.command == 'fetch-history':
        result = _fetch(args, settings, start_ms, end_ms)
        output_json(json_value(result))
        errors = result.get('errors', 0)
        return 1 if errors else 0
    if args.command == 'import-scan':
        from .scan_import import import_scan
        with HistoryStore(args.db) as store:
            symbols = {row['market'].removesuffix('-USD.P') for row in store.iter_contracts()}
            resolved = _precision(store, settings, symbols)
            result = import_scan(args.src, store, price_tick=settings.price_tick,
                                 band_bps=settings.band_bps,
                                 symbol_overrides=resolved.symbol_overrides)
        output_json(json_value(result))
        return 0
    from .bt_report import competition_markdown, competition_report, markdown_summary
    with HistoryStore(args.db, create=False) as store:
        if args.command == 'competition':
            result = competition_report(store.iter_depth_observations(), settings)
            markdown = competition_markdown(result)
        else:
            result = _backtests(store, settings, start_ms, end_ms,
                                include_events=getattr(args, 'events', False))
            markdown = markdown_summary(result['reports'])
    output_json(json_value(result), args.report)
    if args.summary:
        _write_markdown(args.summary, markdown)
    return 0
