"""Command-line entry point and offline demo scenarios."""

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

from .config import Settings
from .market import Book
from .paper import replay
from .recorder import Recorder
from .storage import Sample, Store


def demo_data(settings):
    symbol = settings.symbol
    metadata = {
        'symbol': symbol, 'ondo_market': settings.ondo_market, 'synthetic': True,
        'ondo_contract': {'market': settings.ondo_market, 'baseCurrency': symbol,
                          'quoteCurrency': 'USD', 'contractType': 'linear',
                          'disabled': False, 'isClosed': False, 'reduceOnly': False},
        'lighter_market': {'symbol': symbol, 'market_id': 0, 'market_type': 'perp',
                           'status': 'active', 'multiplier': '1',
                           'supported_size_decimals': 3, 'supported_price_decimals': 3,
                           'min_base_amount': '0.001', 'min_quote_amount': '10'}}
    rows = []
    for i, mid in enumerate(('100', '99.7', '99.7', '100.2', '100.2', '100.0', '100.0')):
        at = 100000 + i * 2000
        mid = Decimal(mid)
        books = [Book.make(venue, symbol, at, None,
                           [(mid - Decimal('.1'), '20')], [(mid + Decimal('.1'), '20')])
                 for venue in ('ondo', 'lighter')]
        rows.append(Sample(at, *books, raw={'synthetic': True}))
    return metadata, rows


def protect_inputs(report, db, config):
    if report is None:
        return
    for source in (db, config):
        if source is None:
            continue
        same = Path(report).resolve() == Path(source).resolve()
        if not same and Path(report).exists() and Path(source).exists():
            same = os.path.samefile(report, source)
        if same:
            raise ValueError('报告输出不能覆盖录制数据库或配置文件')


def output_json(value, report=None):
    text = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    if report:
        path = Path(report)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                             prefix='.pendle-', delete=False) as stream:
                temporary = stream.name
                stream.write(text)
            os.replace(temporary, path)
        finally:
            if temporary and Path(temporary).exists():
                Path(temporary).unlink()
    else:
        print(text, end='')


def main(argv=None):
    parser = argparse.ArgumentParser(description='Ondo／Lighter 公开行情研究与做市，默认只读')
    commands = parser.add_subparsers(dest='command', required=True)
    from .bt_cli import COMMANDS, dispatch, register_commands
    register_commands(commands)
    from . import maker_cli
    maker_cli.register_commands(commands)
    collect = commands.add_parser('collect', help='常驻采集公开实时行情')
    collect.add_argument('--config', type=Path, required=True, help='采集 TOML 配置')
    collected = commands.add_parser('import-collected', help='导入采集服务的压缩 JSONL')
    collected.add_argument('--src', type=Path, required=True, help='采集文件或目录')
    collected.add_argument('--db', type=Path, default=Path('data/history.sqlite'), help='历史数据库')
    for name, help_text in (('record', '有限时长录制公开盘口'),
                            ('replay', '只读回放已录制会话'), ('demo', '离线生成演示数据并回放')):
        sub = commands.add_parser(name, help=help_text, description=help_text)
        sub.add_argument('--config', type=Path, help='TOML 配置；回放时只允许调整策略假设')
        sub.add_argument('--db', type=Path,
                         default=Path('data/demo.sqlite' if name == 'demo' else 'data/market.sqlite'),
                         help='SQLite 录制文件')
        if name == 'record':
            sub.add_argument('--seconds', type=float, default=60, help='录制秒数，默认 60')
        else:
            sub.add_argument('--report', type=Path, help='JSON 报告文件；省略时写标准输出')
        if name == 'replay':
            sub.add_argument('--run-id', help='会话 ID；省略时使用最新会话')
    args = parser.parse_args(argv)
    try:
        if args.command in maker_cli.COMMANDS:
            return maker_cli.dispatch(args)
        if args.command == 'collect':
            from .bt_config import CollectSettings
            settings = CollectSettings.from_file(args.config)
            from .collector import run_collect
            return run_collect(settings)
        if args.command == 'import-collected':
            from .collected_import import import_collected
            from .history import HistoryStore
            with HistoryStore(args.db) as store:
                result = import_collected(args.src, store)
            output_json(result)
            return 0
        if args.command in COMMANDS:
            return dispatch(args, output_json=output_json, protect_inputs=protect_inputs)
        protect_inputs(getattr(args, 'report', None), args.db, args.config)
        settings = ((Settings.read_values(args.config) if args.command == 'replay'
                     else Settings.from_file(args.config)) if args.config else None)
        if args.command == 'record':
            summary = Recorder(settings or Settings()).record(args.db, args.seconds)
            output_json(summary)
            return 0 if summary['failed_samples'] == 0 else 1
        if args.command == 'demo':
            settings = settings or Settings()
            meta, samples = demo_data(settings)
            with Store(args.db) as store:
                run_id = store.create_run(settings, meta, 'demo')
                for sample in samples:
                    store.append(run_id, sample)
                run = store.get_run(run_id)
                result = replay(run, store.iter_samples(run_id), settings)
        else:
            with Store(args.db, create=False) as store:
                run = store.get_run(args.run_id)
                result = replay(run, store.iter_samples(run['id']), settings)
        output_json(result, args.report)
        return 0
    except (ValueError, OSError, sqlite3.Error, KeyError, TypeError) as exc:
        print(f'错误：{exc}', file=sys.stderr)
        return 2
