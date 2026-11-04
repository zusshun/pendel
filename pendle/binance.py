"""校验并导入币安 USDⓈ-M 公开逐笔成交日归档。"""

import csv
import hashlib
import io
import math
import re
import time
import urllib.error
import urllib.request
import zipfile
import zlib
from datetime import date
from email.utils import parsedate_to_datetime

from .market import HttpClient, MarketError, RateLimited, number


MAX_FILE_BYTES = 200 * 1024 * 1024
BASE_URL = 'https://data.binance.vision/data/futures/um/daily/aggTrades'
CSV_COLUMNS = ('agg_trade_id', 'price', 'quantity', 'first_trade_id',
               'last_trade_id', 'transact_time', 'is_buyer_maker')


class BinaryHttpClient(HttpClient):
    """沿用公开 HTTP 客户端的超时，增加有上限的二进制 GET。"""

    def __init__(self, timeout_s=5, *, opener=urllib.request.urlopen, clock=time.time):
        super().__init__(timeout_s)
        self.opener = opener
        self.clock = clock

    def get_bytes(self, url, max_bytes=MAX_FILE_BYTES):
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ValueError('下载字节上限必须是正整数')
        limit = min(max_bytes, MAX_FILE_BYTES)
        request = urllib.request.Request(url, headers={
            'User-Agent': 'pendle-research/0.1',
            'Accept': 'application/octet-stream'}, method='GET')
        try:
            with self.opener(request, timeout=self.timeout_s) as response:
                body = response.read(limit + 1)
                if len(body) > limit:
                    raise MarketError('币安下载超过单文件字节上限')
                return body
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code == 429:
                raw = exc.headers.get('Retry-After', '2')
                try:
                    delay = float(raw)
                    if not math.isfinite(delay):
                        raise ValueError
                except (ValueError, TypeError):
                    try:
                        delay = parsedate_to_datetime(raw).timestamp() - self.clock()
                    except (ValueError, TypeError, OverflowError):
                        delay = 2
                raise RateLimited(delay) from exc
            raise MarketError(f'币安下载 HTTP 错误：{exc.code}') from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise MarketError(f'币安下载失败：{type(exc).__name__}') from exc


def _symbol(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Z][A-Z0-9]{0,39}', value):
        raise ValueError('币安标的必须是大写交易对代码')
    return value


def _trade(row, symbol):
    if len(row) != len(CSV_COLUMNS):
        raise MarketError('币安成交 CSV 列数无效')
    try:
        agg_id, first_id, last_id, at = (int(row[index]) for index in (0, 3, 4, 5))
        price, quantity = number(row[1]), number(row[2])
        maker = row[6].strip().lower()
        if (min(agg_id, first_id, last_id, at) < 0 or first_id > last_id
                or price <= 0 or quantity <= 0 or maker not in ('true', 'false')):
            raise ValueError
        return {'symbol': symbol, 'agg_id': agg_id, 'price': price,
                'quantity': quantity, 'first_trade_id': first_id,
                'last_trade_id': last_id, 'transact_time': at,
                'is_buyer_maker': maker == 'true'}
    except (ValueError, TypeError) as exc:
        raise MarketError('币安成交 CSV 包含无效字段') from exc


def parse_agg_trades_zip(body, symbol):
    """解析有表头或无表头的七列 aggTrades；整份有效后才返回。"""
    symbol = _symbol(symbol)
    if len(body) > MAX_FILE_BYTES:
        raise MarketError('币安归档超过单文件字节上限')
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            files = [item for item in archive.infolist()
                     if not item.is_dir() and item.filename.endswith('.csv')]
            if len(files) != 1:
                raise MarketError('币安归档必须包含唯一的 CSV 文件')
            if files[0].file_size > MAX_FILE_BYTES:
                raise MarketError('币安 CSV 超过单文件字节上限')
            with archive.open(files[0]) as stream:
                reader = csv.reader(io.TextIOWrapper(stream, encoding='utf-8-sig', newline=''))
                result = []
                first = True
                for row in reader:
                    if not row:
                        continue
                    if first and tuple(row) == CSV_COLUMNS:
                        first = False
                        continue
                    first = False
                    result.append(_trade(row, symbol))
                return result
    except (zipfile.BadZipFile, zipfile.LargeZipFile, UnicodeError, csv.Error,
            RuntimeError, OSError, EOFError, zlib.error) as exc:
        raise MarketError(f'币安归档读取失败：{type(exc).__name__}') from exc


def _verify_checksum(body, checksum, filename):
    try:
        for line in checksum.decode('ascii').splitlines():
            parts = line.split()
            if (len(parts) == 2 and parts[1] == filename
                    and re.fullmatch(r'[0-9a-fA-F]{64}', parts[0])):
                if hashlib.sha256(body).hexdigest() != parts[0].lower():
                    raise MarketError('币安归档 SHA256 校验不一致')
                return
    except (UnicodeError, AttributeError) as exc:
        raise MarketError('币安 CHECKSUM 格式无效') from exc
    raise MarketError('币安 CHECKSUM 缺少对应文件的 SHA256')


def fetch_binance_day(client, symbol, day, store, *, clock=time.time, sleep=time.sleep,
                      request_interval_ms=0, max_retries=3):
    """下载并校验一个 UTC 日；失败保留抓取记录且不写入成交。"""
    symbol = _symbol(symbol)
    try:
        day = date.fromisoformat(day) if isinstance(day, str) else day
        if type(day) is not date:
            raise ValueError
    except (ValueError, TypeError) as exc:
        raise ValueError('币安归档日期必须是 YYYY-MM-DD') from exc
    for name, value in (('request_interval_ms', request_interval_ms),
                        ('max_retries', max_retries)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f'{name} 必须是非负整数')
    filename = f'{symbol}-aggTrades-{day.isoformat()}.zip'
    url = f'{BASE_URL}/{symbol}/{filename}'
    params = {'symbol': symbol, 'date': day.isoformat(), 'url': url}
    started_ms = int(clock() * 1000)
    requested = False

    def download(target):
        nonlocal requested
        for attempt in range(max_retries + 1):
            if requested and request_interval_ms:
                sleep(request_interval_ms / 1000)
            requested = True
            try:
                return client.get_bytes(target, max_bytes=MAX_FILE_BYTES)
            except RateLimited as exc:
                if attempt == max_retries:
                    raise
                sleep(exc.retry_after_s)

    try:
        body = download(url)
        checksum = download(url + '.CHECKSUM')
        _verify_checksum(body, checksum, filename)
        rows = parse_agg_trades_zip(body, symbol)
        store.upsert_binance_trades(rows)
    except (ValueError, OSError) as exc:
        store.record_fetch('binance', params, started_ms=started_ms,
                           finished_ms=int(clock() * 1000), status='error',
                           error=str(exc), row_count=0, complete=False)
        raise
    times = [row['transact_time'] for row in rows]
    return store.record_fetch(
        'binance', params, started_ms=started_ms, finished_ms=int(clock() * 1000),
        status='ok', coverage_start_ms=min(times) if times else None,
        coverage_end_ms=max(times) if times else None, row_count=len(rows), complete=True)
