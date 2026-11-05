"""Safety boundary for live Ondo access; callers explicitly inject all network transports."""

import asyncio
import hashlib
import hmac
import json
import logging
import math
import os
import subprocess
import re
import stat
import time
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode

from .market import number


ENVIRONMENTS = {
    'prod': ('https://api.ondoperps.xyz', 'wss://api.ondoperps.xyz/ws'),
    'sandbox': ('https://api.ondoperps-sandbox.xyz', 'wss://api.ondoperps-sandbox.xyz/ws'),
}
ORDERS_PATH = '/v1/perps/orders'
ORDER_QUERY_PATH = ORDERS_PATH + '/client:{client_order_id}'
OPEN_ORDERS_PATH = ORDERS_PATH
MARKETS_PATH = '/v1/markets'
POSITIONS_PATH = '/v1/perps/positions'
BALANCES_PATH = '/v1/perps/balance'
LEVERAGE_PATH = '/v1/perps/leverage'
FILLS_PATH = '/v1/perps/fills'
_READ_PATHS = {ORDERS_PATH, MARKETS_PATH, POSITIONS_PATH, BALANCES_PATH,
               LEVERAGE_PATH, FILLS_PATH}
_DEFERRED = object()
_ID = re.compile(r'[A-Za-z0-9_.~-]+\Z')


class EndpointNotAllowed(ValueError):
    """This module is not authorized to access the endpoint."""


class GateClosed(RuntimeError):
    """Outbound sending has been disabled."""


def rest_signature(secret, timestamp_ms, method, path_query, body=''):
    """Sign the exact transmitted path, query, and body, preserving the full key prefix."""
    prehash = str(timestamp_ms) + method.upper() + path_query + body
    return hmac.new(secret.encode(), prehash.encode(), hashlib.sha256).hexdigest()


def ws_login_signature(secret, timestamp_ms):
    return hmac.new(secret.encode(), (str(timestamp_ms) + 'ondo_perps_ws_login').encode(),
                    hashlib.sha256).hexdigest()


@dataclass(frozen=True, repr=False)
class Credentials:
    key: str
    secret: str

    def __post_init__(self):
        if not isinstance(self.key, str) or not self.key or not isinstance(self.secret, str) or not self.secret:
            raise ValueError('API 密钥及密钥内容不能为空')

    def __repr__(self):
        return 'Credentials(key=***, secret=***)'


def _read_secret_file(path):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(descriptor, encoding='utf-8') as stream:
        mode = os.fstat(stream.fileno()).st_mode
        if not stat.S_ISREG(mode) or stat.S_IMODE(mode) != 0o600:
            raise ValueError('密钥文件必须是权限为 600 的普通文件')
        return mode, stream.read(65537)


def load_credentials(*, env=None, secret_file=None, file_reader=_read_secret_file):
    env = os.environ if env is None else env
    if secret_file is None:
        return Credentials(env.get('ONDO_API_KEY'), env.get('ONDO_API_SECRET'))
    try:
        mode, content = file_reader(secret_file)
        if not stat.S_ISREG(mode) or stat.S_IMODE(mode) != 0o600 or len(content) > 65536:
            raise ValueError
        payload = json.loads(content)
        return Credentials(payload['key'], payload['secret'])
    except (OSError, KeyError, TypeError, ValueError):
        raise ValueError('密钥文件无效，必须使用权限为 600 的 JSON 普通文件') from None


class SecretFilter(logging.Filter):
    """Redact keys and signatures from access-module logs without retaining original exception text."""

    def __init__(self, credentials):
        super().__init__()
        self.credentials = credentials

    def filter(self, record):
        text = record.getMessage()
        for secret in (self.credentials.key, self.credentials.secret):
            text = text.replace(secret, '***')
        text = re.sub(r'(?i)(ONDO-SIGN[\s=:]+)[0-9a-f]{64}', r'\1***', text)
        text = re.sub(r'(?i)(["\']?(?:sign|signature)["\']?\s*[:=]\s*["\']?)[0-9a-f]{64}', r'\1***', text)
        record.msg, record.args, record.exc_info, record.exc_text = text, (), None, None
        return True


def classify_error(status, code=None, *, cancelling=False, order_found=None):
    if status == 401 or code in {'ip_not_permitted', 'key_doesnt_have_scope',
            'account_in_liquidation', 'insufficient_margin', 'net_position_too_large',
            'reduce_only_flipping_position', 'reduce_only_increasing_position', 'timestamp_too_far'}:
        return 'halt'
    if 500 <= status < 600:
        return 'guard'
    if code == 'clientOrderID_collision':
        return 'halt' if order_found is False else 'reconcile'
    if status == 400 and code == 'order_not_found':
        return 'not_found'
    if code == 'post_only_has_match':
        return 'recompute'
    if code == 'reduce_only_no_open_position' or (cancelling and (
            status == 404 or isinstance(code, str) and code.startswith('order_already_'))):
        return 'reconcile'
    if code == 'reduce_only_invalid_tif':
        return 'reduce_only_fallback'
    return 'guard'


class ApiError(RuntimeError):
    def __init__(self, status, code=None, *, cancelling=False, order_found=None, error=None):
        self.status = status
        # Allow only short error-code identifiers; retain exchange error text separately after access-layer redaction.
        self.code = code if isinstance(code, str) and re.fullmatch(r'[A-Za-z0-9_]{1,100}', code) else None
        self.error = error if isinstance(error, str) else None
        self.category = classify_error(status, self.code, cancelling=cancelling, order_found=order_found)
        super().__init__(f'交易接口失败：HTTP {status}，类别 {self.category}')


class TransportError(ApiError, OSError):
    def __init__(self):
        super().__init__(0)


class ResponseFormatError(ApiError):
    """The response cannot be parsed according to endpoint semantics; retain its actual status and exchange error code."""

    response_format = True

    def __init__(self, status=0, code=None, *, cancelling=False):
        super().__init__(status, code, cancelling=cancelling)
        message = str(self) if status else f'交易接口失败：HTTP 状态码未知，类别 {self.category}'
        self.args = (message + '，响应格式不符',)


def _error_code(payload):
    code = payload.get('error')
    if isinstance(code, dict):
        code = code.get('code')
    return payload.get('error_code', payload.get('code', code))


class _ResponsePayload(dict):
    """Retain response provenance to prevent mixing status codes across concurrent requests."""

    def __init__(self, response, *, cancelling=False):
        super().__init__(response.payload)
        self.status = response.status
        self.cancelling = cancelling
        self.result_missing = 'result' not in self


def _format_error(payload):
    return ResponseFormatError(getattr(payload, 'status', 0), _error_code(payload),
                               cancelling=getattr(payload, 'cancelling', False))


@dataclass(frozen=True)
class HttpResponse:
    status: int
    payload: object
    headers: dict = field(default_factory=dict)


@dataclass(frozen=True)
class HttpRequest:
    method: str
    url: str
    path_query: str
    headers: dict = field(repr=False)
    body: str = field(repr=False)


def _endpoint(method, path_query):
    path, _, query = path_query.partition('?')
    if any(c in path_query for c in ('#', '\\', '\r', '\n')) or '%' in path or '..' in path or '//' in path:
        raise EndpointNotAllowed('接口不在交易白名单内')
    pairs = parse_qsl(query, keep_blank_values=True)
    params = dict(pairs)
    if len(params) != len(pairs):
        raise EndpointNotAllowed('不允许重复的查询参数')
    suffix = path[len(ORDERS_PATH) + 1:] if path.startswith(ORDERS_PATH + '/') else ''
    order_id = suffix[7:] if suffix.startswith('client:') else suffix
    single = bool(order_id and _ID.fullmatch(order_id) and suffix != 'batch')
    allowed = (method == 'POST' and path == ORDERS_PATH and not query or
               method == 'GET' and (path in _READ_PATHS or single) or
               method == 'DELETE' and (single or
               path == ORDERS_PATH and set(params) == {'market'} and params['market'] or
               path == ORDERS_PATH + '/batch' and set(params) == {'orderIDs'} and params['orderIDs']))
    if not allowed:
        raise EndpointNotAllowed('接口不在交易白名单内')
    return path, params


class SendGate:
    """An irreversible send gate for one event loop, retaining only a limited shutdown path after closure."""

    def __init__(self):
        self.is_open = True
        self.trading_halted = False
        self._finished = False
        self._attempts = {'DELETE': 0, 'GET': 0}
        self._listeners = []

    def halt_trading(self):
        self.trading_halted = True
        for callback in self._listeners:
            callback()

    def validate_trade(self, body, *, emergency=False):
        self.check()
        if type(emergency) is not bool:
            raise GateClosed('紧急交易标记无效')
        if emergency:
            if (not self.trading_halted or not isinstance(body, dict)
                    or body.get('reduceOnly') is not True
                    or body.get('timeInForce') != 'IOC' or body.get('postOnly') is not False):
                raise GateClosed('熔断后只允许明确标记的 reduceOnly IOC 紧急减仓')
        elif self.trading_halted:
            raise GateClosed('交易已熔断，禁止普通下单')

    def close(self):
        self.is_open = False
        for callback in self._listeners:
            callback()

    def finish_stop(self):
        self._finished = True
        self.close()

    def check_rest(self, method, path_query, *, stop=False, consume=False, emergency=False):
        if type(emergency) is not bool or emergency and (method != 'POST' or
                path_query != ORDERS_PATH or not self.trading_halted or not self.is_open):
            raise GateClosed('紧急通道仅授权健康通信下的熔断减仓')
        if self.is_open:
            if method == 'POST' and self.trading_halted and not emergency:
                raise GateClosed('交易已熔断，禁止普通下单')
            return
        path, params = _endpoint(method, path_query)
        eligible = stop and path == ORDERS_PATH and (
            method == 'DELETE' and set(params) == {'market'} and bool(params['market']) or
            method == 'GET' and {'market', 'status'} <= set(params) <= {'market', 'status', 'cursor', 'limit'} and
            bool(params['market']) and params['status'] == 'open')
        if self._finished or not eligible or self._attempts[method] >= 3:
            raise GateClosed('对外发送已关闭，停止例外未授权或次数已用尽')
        if consume:
            self._attempts[method] += 1

    def check(self):
        if not self.is_open:
            raise GateClosed('对外发送已关闭')

    async def send_ws(self, sender, message):
        self.check()
        # Do not yield between the check and transport entry, for either public or private connections.
        return await sender(message)

    async def connect(self, factory, *args, **kwargs):
        self.check()
        return await factory(*args, **kwargs)


@dataclass
class _Pending:
    method: str
    path_query: str
    body: str
    future: object
    stop: bool
    before_send: object
    sequence: int
    timestamp_retry: bool = False
    emergency: bool = False

    @property
    def priority(self):
        path = self.path_query.partition('?')[0]
        if self.method == 'DELETE':
            return 0 if path == ORDERS_PATH else 2
        if self.method == 'POST':
            return 1 if json.loads(self.body).get('reduceOnly') is True else 3
        return 4


class OndoAPI:
    """Async client with a priority token bucket; transports accept HttpRequest and return HttpResponse."""

    def __init__(self, credentials, *, transport, environment='sandbox', gate=None,
                 rate=1, timeout_s=5, clock=time.time, monotonic=time.monotonic, sleep=asyncio.sleep,
                 ntp_check=None, sibling_markets=()):
        if environment not in ENVIRONMENTS:
            raise ValueError('交易环境只能为 prod 或 sandbox')
        if isinstance(rate, bool) or not math.isfinite(rate) or rate <= 0:
            raise ValueError('请求速率必须是有限正数')
        if isinstance(timeout_s, bool) or not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError('传输超时必须是有限正数')
        self.timeout_s = timeout_s
        self._active = set()
        self._transmissions = {}
        self.credentials, self.transport = credentials, transport
        self.sibling_markets = tuple(sibling_markets)
        self.base_url, self.ws_url = ENVIRONMENTS[environment]
        self.gate = gate or SendGate()
        self.rate, self.clock, self.monotonic, self.sleep = rate, clock, monotonic, sleep
        self.offset_ms = 0
        self.ntp_check = ntp_check or check_ntp_synchronized
        self.clock_verified = False
        self._date_observation = None
        self._tokens, self._last_refill = 1.0, monotonic()
        self._blocked_until = 0
        self._pending, self._runner, self._sequence = [], None, 0
        self._wake = asyncio.Event()
        self.gate._listeners.append(self._wake.set)
        self.logger = logging.getLogger(__name__ + '.' + str(id(self)))
        self.logger.addFilter(SecretFilter(credentials))

    @property
    def in_flight_writes(self):
        """Include locally timed-out order submissions whose transports are still running; shutdown must account for them."""
        return sum(method == 'POST' and not task.done()
                   for task, method in self._transmissions.items())

    async def request(self, method, path, *, params=None, body=None, stop=False, before_send=None,
                      emergency=False):
        method = method.upper()
        if params:
            if '?' in path:
                raise EndpointNotAllowed('查询字符串不能重复传入')
            path += '?' + urlencode(params)
        _endpoint(method, path)
        if body is not None and method != 'POST':
            raise EndpointNotAllowed('只允许下单接口携带请求正文')
        if method == 'POST' and not isinstance(body, dict):
            raise ValueError('下单正文必须是字典')
        serialized = '' if body is None else json.dumps(body, ensure_ascii=False,
                                                       separators=(',', ':'), allow_nan=False)
        self.gate.check_rest(method, path, stop=stop, emergency=emergency)
        if method == 'POST':
            self.gate.validate_trade(body, emergency=emergency)
        return await self._enqueue(method, path, serialized, stop, before_send, emergency=emergency)

    async def _enqueue(self, method, path, body, stop, before_send, timestamp_retry=False, emergency=False):
        future = asyncio.get_running_loop().create_future()
        self._sequence += 1
        self._pending.append(_Pending(method, path, body, future, stop, before_send, self._sequence, timestamp_retry, emergency))
        self._wake.set()
        if self._runner is None or self._runner.done():
            self._runner = asyncio.create_task(self._drain())
        try:
            return await future
        finally:
            if future.cancelled():
                self._wake.set()

    async def _wait(self, delay):
        self._wake.clear()
        sleeper = asyncio.create_task(self.sleep(max(0, delay)))
        waker = asyncio.create_task(self._wake.wait())
        try:
            await asyncio.wait((sleeper, waker), return_when=asyncio.FIRST_COMPLETED)
        finally:
            sleeper.cancel()
            waker.cancel()
            await asyncio.gather(sleeper, waker, return_exceptions=True)

    async def _drain(self):
        while self._pending:
            self._pending = [item for item in self._pending if not item.future.done()]
            now = self.monotonic()
            self._tokens = min(1.0, self._tokens + max(0, now - self._last_refill) * self.rate)
            self._last_refill = now
            candidates = []
            for item in self._pending[:]:
                try:
                    self.gate.check_rest(item.method, item.path_query, stop=item.stop, emergency=item.emergency)
                except GateClosed as exc:
                    item.future.set_exception(exc.with_traceback(None))
                    self._pending.remove(item)
                    continue
                if now >= self._blocked_until or item.method == 'DELETE':
                    candidates.append(item)
            if not self._pending:
                break
            if not candidates or self._tokens < 1:
                delay = max(0, (1 - self._tokens) / self.rate)
                if not candidates:
                    delay = max(delay, self._blocked_until - now)
                await self._wait(delay)
                continue
            item = min(candidates, key=lambda entry: (entry.priority, entry.sequence))
            self._pending.remove(item)
            self._tokens -= 1
            active = asyncio.create_task(self._dispatch(item))
            self._active.add(active)
            active.add_done_callback(self._active.discard)

    async def _dispatch(self, item):
        if item.future.done():
            return
        transmission = asyncio.create_task(self._transmit(item))
        self._transmissions[transmission] = item.method
        transmission.add_done_callback(lambda task: self._transmissions.pop(task, None))
        timer = asyncio.create_task(self.sleep(self.timeout_s))
        try:
            done, _ = await asyncio.wait((transmission, timer), return_when=asyncio.FIRST_COMPLETED)
            if transmission not in done:
                raise TransportError()
            result = transmission.result()
            if result is _DEFERRED:
                self._pending.append(item)
                self._wake.set()
                if self._runner is None or self._runner.done():
                    self._runner = asyncio.create_task(self._drain())
                return
        except asyncio.CancelledError:
            # A self-cancelled transport may still have been accepted; return an UNKNOWN error while the caller is waiting.
            if not item.future.done():
                item.future.set_exception(TransportError())
        except Exception as exc:
            if not item.future.done():
                item.future.set_exception(exc.with_traceback(None))
        else:
            if not item.future.done():
                item.future.set_result(result)
        finally:
            timer.cancel()
            if not transmission.done():
                transmission.cancel()
            # Transports may ignore cancellation; do not let them block later cancellations or shutdown.
            transmission.add_done_callback(self._consume_task)
            await asyncio.gather(timer, return_exceptions=True)

    @staticmethod
    def _consume_task(task):
        if not task.cancelled():
            task.exception()

    async def _transmit(self, item):
        timestamp = int(self.clock() * 1000 + self.offset_ms)
        headers = {'ONDO-KEY-ID': self.credentials.key, 'ONDO-TIMESTAMP': str(timestamp),
                   'ONDO-SIGN': rest_signature(self.credentials.secret, timestamp,
                                              item.method, item.path_query, item.body),
                   'Content-Type': 'application/json', 'Accept': 'application/json'}
        request = HttpRequest(item.method, self.base_url + item.path_query,
                              item.path_query, headers, item.body)
        self.gate.check_rest(item.method, item.path_query, stop=item.stop, emergency=item.emergency)
        if item.future.done():
            raise asyncio.CancelledError
        if item.method != 'DELETE' and self.monotonic() < self._blocked_until:
            return _DEFERRED
        if item.method == 'POST':
            self.gate.validate_trade(json.loads(item.body), emergency=item.emergency)
        if item.before_send is not None:
            result = item.before_send(timestamp_retry=True) if item.timestamp_retry else item.before_send()
            if result is not None:
                if hasattr(result, 'close'):
                    result.close()
                raise TypeError('发送前回调必须同步执行且返回 None')
        self.gate.check_rest(item.method, item.path_query, stop=item.stop, consume=True,
                             emergency=item.emergency)
        if item.method == 'POST':
            self.gate.validate_trade(json.loads(item.body), emergency=item.emergency)
        started = self.clock()
        try:
            response = await self.transport(request)
        except Exception as exc:
            if getattr(exc, 'response_format', False) and type(getattr(exc, 'status', None)) is int:
                raise ResponseFormatError(exc.status, cancelling=item.method == 'DELETE') from None
            raise TransportError() from None
        if not isinstance(response, HttpResponse) or type(response.status) is not int:
            raise ResponseFormatError()
        self._date_observation = (response.headers, (started + self.clock()) / 2)
        if response.status == 429:
            self._blocked_until = max(self._blocked_until,
                                      self.monotonic() + self._retry_after(response.headers))
        if not isinstance(response.payload, dict):
            raise ResponseFormatError(response.status, cancelling=item.method == 'DELETE')
        payload = _ResponsePayload(response, cancelling=item.method == 'DELETE')
        code = _error_code(payload)
        if ('success' in payload and type(payload['success']) is not bool
                or payload.get('success') is not True and payload.get('success') is not False
                and (response.status < 400 or code is None)):
            raise _format_error(payload)
        if response.status >= 400 or payload.get('success') is False:
            error = payload.get('error')
            if isinstance(error, str):
                record = logging.LogRecord('pendle.maker', logging.WARNING, '', 0, error, (), None)
                SecretFilter(self.credentials).filter(record)
                error = record.getMessage()
            raise ApiError(response.status, code, cancelling=item.method == 'DELETE', error=error)
        if item.method == 'POST':
            result = self._result(payload)
            if not isinstance(result, dict) or not result.get('orderId'):
                raise _format_error(payload)
        elif payload.result_missing:
            path = item.path_query.partition('?')[0]
            single = path == BALANCES_PATH or path.startswith(ORDERS_PATH + '/')
            payload['result'] = None if item.method == 'DELETE' or single else []
        return payload

    def _retry_after(self, headers):
        if not isinstance(headers, dict):
            return 2.0
        raw = next((value for key, value in headers.items()
                    if isinstance(key, str) and key.lower() == 'retry-after'), '2')
        try:
            delay = float(raw)
        except (ValueError, TypeError):
            try:
                delay = parsedate_to_datetime(raw).timestamp() - self.clock()
            except (ValueError, TypeError, OverflowError):
                delay = 2
        return max(0.0, delay) if math.isfinite(delay) else 2.0

    @staticmethod
    def _result(payload):
        if payload.get('success') is not True or 'result' not in payload:
            raise _format_error(payload)
        return payload['result']

    async def sync_time(self):
        """Validate system NTP and REST Date without adjusting the local clock through a nonexistent endpoint."""
        self.clock_verified = False
        try:
            synchronized = self.ntp_check()
            if hasattr(synchronized, '__await__'):
                synchronized = await synchronized
            if synchronized is not True:
                raise ValueError
            self._date_observation = None
            await self.request('GET', MARKETS_PATH)
            headers, local_time = self._date_observation
            date = next(value for key, value in headers.items() if key.lower() == 'date')
            remote = parsedate_to_datetime(date)
            if remote.tzinfo is None or abs(remote.timestamp() - local_time) > 2:
                raise ValueError
        except ResponseFormatError:
            raise
        except Exception:
            error = ApiError(0, 'clock_not_synchronized')
            error.category = 'halt'
            raise error from None
        self.offset_ms = 0
        self.clock_verified = True
        return 0.0

    def ws_login(self):
        timestamp = str(int(self.clock() * 1000 + self.offset_ms))
        return {'op': 'login', 'args': {'key': self.credentials.key, 'time': timestamp,
                'sign': ws_login_signature(self.credentials.secret, timestamp)}}

    async def place_order(self, body, *, before_send=None, emergency=False):
        return self._result(await self.request('POST', ORDERS_PATH, body=body,
                                               before_send=before_send, emergency=emergency))

    async def cancel_all(self, market, *, stop=False):
        return self._result(await self.request('DELETE', ORDERS_PATH,
                                               params={'market': market}, stop=stop))

    async def cancel_order(self, client_order_id, *, by_order_id=False):
        reference = client_order_id if by_order_id else 'client:' + client_order_id
        return self._result(await self.request('DELETE', ORDERS_PATH + '/' + reference))

    async def get_order(self, client_order_id, *, by_order_id=False):
        try:
            reference = client_order_id if by_order_id else 'client:' + client_order_id
            payload = await self.request('GET', ORDERS_PATH + '/' + reference)
        except ApiError as exc:
            if not isinstance(exc, ResponseFormatError) and exc.category != 'halt' and (exc.status == 404 or
                    exc.status == 400 and exc.code == 'order_not_found'):
                return None
            raise
        result = self._result(payload)
        if result is None or result == []:
            return None
        if not isinstance(result, dict):
            raise _format_error(payload)
        return result

    async def _pages(self, path, params, *, stop=False, normalize=None):
        result, cursors = [], set()
        while True:
            payload = await self.request('GET', path, params=params, stop=stop)
            rows = self._result(payload)
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise _format_error(payload)
            page = payload.get('pageInfo', {})
            if not isinstance(page, dict):
                raise _format_error(payload)
            cursor = page.get('nextCursor')
            if cursor not in (None, '') and (not isinstance(cursor, str) or cursor in cursors):
                raise _format_error(payload)
            if (any(payload.get(key) not in (None, '') for key in ('cursor', 'nextCursor', 'next'))
                    or any(payload.get(key) is not None and payload[key] is not False
                           for key in ('hasMore', 'truncated'))
                    or 'complete' in payload and payload['complete'] is not True):
                raise _format_error(payload)
            if self.sibling_markets and path in (OPEN_ORDERS_PATH, FILLS_PATH):
                # Retain requested peer orders so peer cancellation cannot falsely prove zero.
                rows = [row for row in rows if row.get('market') == params['market']
                        or row.get('market') not in self.sibling_markets]
            if path == OPEN_ORDERS_PATH and any(
                    row.get('market') != params['market']
                    or not row.get('orderId') for row in rows):
                raise _format_error(payload)
            if normalize is not None:
                rows = [normalize(row, status=payload.status, code=_error_code(payload)) for row in rows]
            result.extend(rows)
            if cursor in (None, ''):
                return result
            cursors.add(cursor)
            params = {**params, 'cursor': cursor}

    async def get_open_orders(self, market, *, stop=False):
        return await self._pages(OPEN_ORDERS_PATH, {'market': market, 'status': 'open'}, stop=stop)

    async def get_positions(self):
        payload = await self.request('GET', POSITIONS_PATH)
        rows = self._result(payload)
        if not isinstance(rows, list):
            raise _format_error(payload)
        if self.sibling_markets:
            rows = [row for row in rows if not isinstance(row, dict)
                    or row.get('market') not in self.sibling_markets]
        return [normalize_position(row, status=payload.status, code=_error_code(payload)) for row in rows]

    async def get_balances(self):
        payload = await self.request('GET', BALANCES_PATH)
        if payload.result_missing:
            return None
        return normalize_balance(self._result(payload), status=payload.status, code=_error_code(payload))

    async def get_fills(self, market, start_time=None, end_time=None):
        params = {'market': market}
        for key, value in (('startTime', start_time), ('endTime', end_time)):
            if value is not None:
                params[key] = value
        return await self._pages(FILLS_PATH, params, normalize=lambda row, **source:
                                 normalize_fill(row, market, **source))


def check_ntp_synchronized(*, run=subprocess.run):
    """On Linux, read only timedatectl synchronization status and fail closed on errors."""
    try:
        result = run(['timedatectl', 'show', '-p', 'NTPSynchronized'],
                     capture_output=True, text=True, timeout=3, check=False)
        return result.returncode == 0 and result.stdout.strip() == 'NTPSynchronized=yes'
    except (OSError, subprocess.SubprocessError):
        return False


def normalize_position(row, *, status=0, code=None):
    try:
        if not isinstance(row, dict) or not isinstance(row.get('market'), str):
            raise ValueError
        if 'direction' in row or 'netQuantity' in row:
            direction, size = row['direction'], number(row['netQuantity'])
            if direction not in ('long', 'short', 'neutral') or size < 0:
                raise ValueError
            quantity = size if direction == 'long' else -size if direction == 'short' else number(0)
        else:
            quantity = number(row['quantity'])
        result = {**row, 'quantity': quantity}
        for original, normalized in (('markPrice', 'mark_price'), ('unrealizedPnl', 'unrealized_pnl')):
            if original in row:
                number(row[original])
                result[normalized] = row[original]
        return result
    except (KeyError, TypeError, ValueError):
        raise ResponseFormatError(status, code) from None


def normalize_balance(row, *, status=0, code=None):
    try:
        if isinstance(row, list) and not row:
            return {'equity': '0', 'available': '0', 'used_margin': '0',
                    'under_liquidation': False, 'margin_ratio': None}
        if not isinstance(row, dict):
            raise ValueError
        if 'marginBalance' not in row:
            number(row['equity'])
            number(row['available'])
            return dict(row)
        result = {**row, 'equity': row['marginBalance'], 'available': row['availableMargin'],
                  'used_margin': row['usedMargin'], 'under_liquidation': row['underLiquidation']}
        for key in ('equity', 'available', 'used_margin'):
            number(result[key])
        if type(result['under_liquidation']) is not bool:
            raise ValueError
        # Verify marginRatio units in the sandbox; the risk layer estimates and alerts when unavailable.
        try:
            ratio = number(row.get('marginRatio'))
            result['margin_ratio'] = row['marginRatio'] if ratio >= 0 else None
        except ValueError:
            result['margin_ratio'] = None
        return result
    except (KeyError, TypeError, ValueError):
        raise ResponseFormatError(status, code) from None


def exchange_time_ms(value, *, status=0, code=None):
    """Accept the official trade time as integer milliseconds or a timezone-aware ISO timestamp."""
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if parsed.tzinfo is not None and parsed.timestamp() >= 0:
                return int(parsed.timestamp() * 1000)
        except (ValueError, OverflowError):
            pass
    raise ResponseFormatError(status, code)


def normalize_fill(row, market=None, *, status=0, code=None):
    try:
        if not isinstance(row, dict):
            raise ValueError
        result = {'fill_id': row['id'], 'market': row.get('market', market),
                  'order_id': row['orderId'], 'client_order_id': row['clientOrderId'],
                  'price': row['price'], 'quantity': row['size'], 'side': row['side'],
                  'fee': row['fee'], 'realized_pnl': row['pnl'],
                  'time_ms': exchange_time_ms(row['time'], status=status, code=code), 'is_maker': row['isMaker']}
        if (any(not isinstance(result[key], str) or not result[key]
                for key in ('fill_id', 'market', 'order_id', 'client_order_id'))
                or result['side'] not in ('buy', 'sell') or type(result['is_maker']) is not bool
                or number(result['quantity']) <= 0 or number(result['price']) <= 0):
            raise ValueError
        number(result['fee'])
        number(result['realized_pnl'])
        if market is not None and result['market'] != market:
            raise ValueError
        return result
    except (KeyError, TypeError, ValueError):
        raise ResponseFormatError(status, code) from None
