"""Public market-data GET requests and order book normalization, without trading or authentication paths."""

import json
import math
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime


class MarketError(ValueError):
    """Public market data is unavailable or has an unexpected format."""


class RateLimited(MarketError):
    def __init__(self, retry_after_s=2):
        self.retry_after_s = min(60.0, max(0.0, retry_after_s))
        super().__init__(f'接口限流，等待 {self.retry_after_s:g} 秒')


def number(value):
    try:
        if isinstance(value, bool):
            raise ValueError
        result = Decimal(str(value))
        if not result.is_finite():
            raise ValueError
        return result
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise MarketError('盘口包含无效数字') from exc


def timestamp_ms(value):
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            raise ValueError
        result = int(dt.timestamp() * 1000)
        if result < 0:
            raise ValueError
        return result
    except (AttributeError, TypeError, ValueError, OverflowError) as exc:
        raise MarketError('交易所时间格式无效') from exc


@dataclass(frozen=True)
class Book:
    venue: str
    symbol: str
    received_ms: int
    source_ms: int | None
    bids: tuple
    asks: tuple

    @classmethod
    def make(cls, venue, symbol, received_ms, source_ms, bids, asks, *, allow_empty=False):
        if venue not in ('ondo', 'lighter') or not isinstance(symbol, str) or not symbol:
            raise MarketError('盘口来源或标的无效')
        for value in (received_ms, source_ms):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise MarketError('盘口时间必须是非负毫秒整数')
        if received_ms is None:
            raise MarketError('缺少接收时间')
        sides = []
        try:
            for rows, reverse in ((bids, True), (asks, False)):
                levels = {}
                for raw_price, raw_size in rows:
                    price, size = number(raw_price), number(raw_size)
                    if price <= 0 or size < 0:
                        raise MarketError('价格必须为正，数量不能为负')
                    if size:
                        levels[price] = levels.get(price, Decimal(0)) + size
                if not levels and not allow_empty:
                    raise MarketError('盘口缺少有效双边深度')
                sides.append(tuple(sorted(levels.items(), reverse=reverse)))
        except (TypeError, KeyError, ValueError) as exc:
            raise MarketError('盘口价格数量格式无效：' + str(exc)) from exc
        if sides[0] and sides[1] and sides[0][0][0] >= sides[1][0][0]:
            raise MarketError('盘口买卖价交叉或相等')
        return cls(venue, symbol, received_ms, source_ms, sides[0], sides[1])

    @property
    def mid(self):
        return (self.bids[0][0] + self.asks[0][0]) / 2

    def to_dict(self):
        return {'venue': self.venue, 'symbol': self.symbol, 'received_ms': self.received_ms,
                'source_ms': self.source_ms,
                'bids': [[str(p), str(q)] for p, q in self.bids],
                'asks': [[str(p), str(q)] for p, q in self.asks]}

    @classmethod
    def from_dict(cls, value):
        try:
            return cls.make(**value)
        except TypeError as exc:
            raise MarketError('保存的盘口字段无效') from exc


def parse_ondo(payload, market, received_ms, *, allow_empty=False):
    try:
        if payload.get('success') is not True:
            raise MarketError('Ondo 返回失败标识')
        result = payload['result']
        if result['market'] != market:
            raise MarketError('Ondo 返回了其他标的')
        return Book.make('ondo', market.removesuffix('-USD.P'), received_ms,
                         timestamp_ms(result['time']), result['bids'], result['asks'],
                         allow_empty=allow_empty)
    except (KeyError, TypeError, AttributeError) as exc:
        raise MarketError('Ondo 盘口字段缺失') from exc


def parse_lighter(payload, symbol, received_ms):
    try:
        if payload.get('code') != 200:
            raise MarketError('Lighter 返回失败标识')
        sides = [[(row['price'], row['remaining_base_amount']) for row in payload[side]]
                 for side in ('bids', 'asks')]
        return Book.make('lighter', symbol, received_ms, None, *sides)
    except (KeyError, TypeError, AttributeError) as exc:
        raise MarketError('Lighter 盘口字段缺失') from exc


def resolve_lighter(payload, symbol):
    try:
        matches = [row for row in payload['order_books'] if row['symbol'] == symbol
                   and row['market_type'] == 'perp' and row['status'] == 'active']
        if payload.get('code') != 200 or len(matches) != 1:
            raise MarketError('Lighter 标的匹配失败或不唯一')
        row = matches[0]
        if number(row['multiplier']) != 1:
            raise MarketError('首版只支持一倍线性合约')
        for name in ('market_id', 'supported_size_decimals', 'supported_price_decimals'):
            if isinstance(row[name], bool) or not isinstance(row[name], int) or not 0 <= row[name] <= (1000000 if name == 'market_id' else 18):
                raise MarketError('Lighter 合约精度或市场 ID 无效')
        return row
    except (KeyError, TypeError, AttributeError) as exc:
        raise MarketError('Lighter 市场元数据无效') from exc


def validate_ondo_contract(payload, market):
    try:
        matches = [row for row in payload['result'] if row['market'] == market]
        if payload.get('success') is not True or len(matches) != 1:
            raise MarketError('Ondo 合约匹配失败')
        row = matches[0]
        if (row['contractType'] != 'linear' or row['quoteCurrency'] != 'USD'
                or row['baseCurrency'] != market.removesuffix('-USD.P')
                or row.get('disabled') is not False or row.get('isClosed') is not False
                or row.get('reduceOnly') is not False):
            raise MarketError('Ondo 合约类型或交易状态不支持')
        return row
    except (KeyError, TypeError, AttributeError) as exc:
        raise MarketError('Ondo 合约字段缺失') from exc


class HttpClient:
    def __init__(self, timeout_s=5):
        self.timeout_s = timeout_s

    def get_json(self, url):
        request = urllib.request.Request(url, headers={
            'User-Agent': 'pendle-research/0.1', 'Accept': 'application/json'}, method='GET')
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                body = response.read(5 * 1024 * 1024 + 1)
                if len(body) > 5 * 1024 * 1024:
                    raise MarketError('公开行情响应超过 5 MiB')
                value = json.loads(body)
                if not isinstance(value, dict):
                    raise MarketError('公开行情响应必须是 JSON 对象')
                return value
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code == 429:
                raw = exc.headers.get('Retry-After', '2')
                try:
                    delay = float(raw)
                    if not math.isfinite(delay):
                        raise ValueError
                except ValueError:
                    try:
                        delay = parsedate_to_datetime(raw).timestamp() - time.time()
                    except (TypeError, ValueError, OverflowError):
                        delay = 2
                raise RateLimited(delay) from exc
            raise MarketError(f'公开行情 HTTP 错误：{exc.code}') from exc
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise MarketError(f'公开行情读取失败：{type(exc).__name__}') from exc
