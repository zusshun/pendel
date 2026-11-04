"""CLI 运行入口与网络适配器；测试必须注入传输以避免联网。"""

import asyncio
import json as json_module
import math
import logging
import os
import re
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .alerts import Alerter, load_telegram_credentials
from .maker_risk import DurableFiles, SingleInstanceLock
from .ondo_api import ApiError, GateClosed, HttpResponse, OndoAPI, SendGate, SecretFilter, load_credentials


MAX_RESPONSE_BYTES = 5 * 1024 * 1024
USER_AGENT = 'pendle-maker/0.1'


class NetworkError(OSError):
    """网络适配失败；错误文本不携带响应、URL 或认证信息。"""


class FormatNetworkError(NetworkError):
    """已收到 HTTP 响应，但响应头或正文格式无效。"""

    response_format = True

    def __init__(self, status):
        self.status = status
        super().__init__(f'网络响应格式不符：HTTP {status}')


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


def _sync_http(method, url, *, headers, body, timeout, opener, expect_json):
    try:
        parts = urlsplit(url)
        if (parts.scheme != 'https' or not parts.hostname or parts.username or parts.password
                or parts.fragment or not math.isfinite(timeout) or timeout <= 0):
            raise ValueError
        request = Request(url, data=body, headers=headers, method=method)
        opener = opener or build_opener(_NoRedirect())
        try:
            response = opener.open(request, timeout=timeout)
        except HTTPError as error:
            response = error
        with response:
            status = response.status if hasattr(response, 'status') else response.code
            if 300 <= status < 400:
                raise NetworkError('网络响应重定向已拒绝')
            response_headers = dict(response.headers.items())
            length = next((value for key, value in response_headers.items()
                           if key.lower() == 'content-length'), None)
            try:
                length = int(length) if length is not None else None
            except (TypeError, ValueError, OverflowError):
                raise FormatNetworkError(status) from None
            if length is not None and length > MAX_RESPONSE_BYTES:
                raise NetworkError('网络响应超过大小限制')
            content = response.read(MAX_RESPONSE_BYTES + 1)
            if len(content) > MAX_RESPONSE_BYTES:
                raise NetworkError('网络响应超过大小限制')
            try:
                payload = json_module.loads(content.decode('utf-8'))
            except (ValueError, UnicodeError):
                if expect_json:
                    raise FormatNetworkError(status) from None
                payload = None
            return HttpResponse(status, payload, response_headers)
    except NetworkError:
        raise
    except Exception:
        raise NetworkError('网络请求失败或响应无效') from None


async def _in_thread(function, *args, **kwargs):
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # urllib 所在线程不能被取消；保留在途状态直到真正结束，避免迟到写请求。
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()
        raise


async def rest_transport(request, *, opener=None, timeout=10):
    """只发送调用方签名后的精确正文，禁用全部重定向。"""
    headers = {key: value for key, value in request.headers.items()
               if key.lower() != 'user-agent'}
    headers['User-Agent'] = USER_AGENT
    return await _in_thread(_sync_http, request.method, request.url,
        headers=headers, body=request.body.encode('utf-8') if request.body else None,
        timeout=timeout, opener=opener, expect_json=True)


async def public_http(method, url, *, json=None, timeout=10, opener=None):
    """公开行情专用 GET；要求 JSON，限制响应大小并禁止重定向。"""
    if method != 'GET' or json is not None:
        raise NetworkError('公开行情 HTTP 仅允许无正文 GET')
    return await _in_thread(_sync_http, method, url, headers={
        'User-Agent': USER_AGENT, 'Accept': 'application/json'}, body=None,
        timeout=timeout, opener=opener, expect_json=True)


async def alert_http(method, url, *, json=None, timeout=10, opener=None):
    """Telegram POST 与健康心跳 GET；不记录敏感地址或正文。"""
    if method not in ('GET', 'POST') or method == 'GET' and json is not None:
        raise NetworkError('告警 HTTP 方法或正文无效')
    try:
        body = (None if json is None else json_module.dumps(json, ensure_ascii=False,
                separators=(',', ':'), allow_nan=False).encode('utf-8'))
    except Exception:
        raise NetworkError('告警 HTTP 正文无效') from None
    return await _in_thread(_sync_http, method, url, headers={
        'Content-Type': 'application/json', 'Accept': 'application/json'}, body=body,
        timeout=timeout, opener=opener, expect_json=method == 'POST')


async def websocket_connect(url, *, gate=None, **kwargs):
    """惰性导入可选依赖；闸门关闭立即中断连接，阻止自动 pong 或 close 续期。"""
    parts = urlsplit(url)
    if parts.scheme != 'wss' or not parts.hostname or parts.username or parts.password or parts.fragment:
        raise NetworkError('WebSocket 地址无效')
    if gate is not None:
        gate.check()
    try:
        import websockets
    except ImportError:
        raise NetworkError('请安装 live 可选依赖后运行 WebSocket') from None
    options = {'open_timeout': 10, 'close_timeout': 1, **kwargs,
               'ping_interval': None, 'ping_timeout': None}
    size = options.get('max_size', MAX_RESPONSE_BYTES)
    options['max_size'] = min(size, MAX_RESPONSE_BYTES) if type(size) is int and size > 0 else MAX_RESPONSE_BYTES
    try:
        connection = await websockets.connect(url, **options)
    except Exception as error:
        failure = NetworkError('WebSocket 连接失败')
        detail = str(error)
        status = getattr(getattr(error, 'response', None), 'status_code', None)
        if status is None:
            status = getattr(error, 'status_code', None)
        if type(status) is int and 100 <= status <= 599 and f'HTTP {status}' not in detail:
            detail = f'HTTP {status}：{detail}'
        # 对外异常保持通用文本；内部诊断必须经过 MakerSecretFilter 后才能记录。
        failure._ws_diagnostic = (type(error).__name__, detail)
        raise failure from None
    if gate is not None:
        def abort_closed():
            if not gate.is_open:
                connection.transport.abort()
        gate._listeners.append(abort_closed)
        abort_closed()
        gate.check()
    return connection


_MISSING = object()


class _ReadOnlyGate(SendGate):
    def check_rest(self, method, path_query, *, stop=False, consume=False, emergency=False):
        if method != 'GET':
            raise GateClosed('dry-run 传输闸门禁止写请求')
        return super().check_rest(method, path_query, stop=stop, consume=consume, emergency=emergency)


def _build_api(settings, dependencies):
    settings._validate_addresses()
    if 'api' in dependencies:
        return dependencies['api']
    env = dependencies.get('env', os.environ)
    credentials = dependencies.get('credentials', _MISSING)
    if credentials is _MISSING:
        if (settings.mode == 'dry-run' and settings.secret_file is None
                and not env.get('ONDO_API_KEY') and not env.get('ONDO_API_SECRET')):
            return None
        reader = ({'file_reader': dependencies['file_reader']}
                  if 'file_reader' in dependencies else {})
        credentials = load_credentials(env=env, secret_file=settings.secret_file, **reader)
    if credentials is None:
        if settings.mode == 'dry-run':
            return None
        raise ValueError('交易模式必须提供 API 密钥')
    options = {key: dependencies[key] for key in ('clock', 'monotonic', 'sleep', 'ntp_check')
               if key in dependencies}
    api = OndoAPI(credentials, transport=dependencies.get('transport', rest_transport),
        environment='sandbox' if settings.mode == 'sandbox' else 'prod',
        gate=_ReadOnlyGate() if settings.mode == 'dry-run' else dependencies.get('gate'),
        rate=settings.rate_per_second,
        timeout_s=settings.request_timeout_s, **options)
    api.base_url, api.ws_url = settings.rest_url.rstrip('/'), settings.ws_url
    return api


def _build_alerter(settings, dependencies):
    if 'alerter' in dependencies:
        return dependencies['alerter']
    env, credentials = dependencies.get('env', os.environ), None
    secret_file = settings.telegram_secret_file
    if secret_file is None and not (env.get('TELEGRAM_BOT_TOKEN') or env.get('TELEGRAM_CHAT_ID')):
        secret_file = settings.secret_file
    reader = ({'file_reader': dependencies['file_reader']} if 'file_reader' in dependencies else {})
    try:
        credentials = load_telegram_credentials(env=env, secret_file=secret_file, **reader)
    except ValueError:
        if settings.mode == 'live' or settings.telegram_secret_file or (
                not secret_file and (env.get('TELEGRAM_BOT_TOKEN') or env.get('TELEGRAM_CHAT_ID'))):
            raise
    return Alerter(credentials, http=dependencies.get('http', alert_http),
        monotonic=dependencies.get('monotonic', time.monotonic),
        healthcheck_url=settings.healthcheck_url)


class MakerSecretFilter(logging.Filter):
    """统一遮蔽 API、Telegram 与签名，并丢弃异常自由文本。"""

    def __init__(self, credentials=None, alerter=None, *, secrets=()):
        super().__init__()
        self.credentials, self.alerter = credentials, alerter
        self.secrets = tuple(value for value in secrets if isinstance(value, str) and value)

    def filter(self, record):
        if self.credentials is not None:
            SecretFilter(self.credentials).filter(record)
        text = record.getMessage()
        for value in sorted(self.secrets, key=len, reverse=True):
            text = text.replace(value, '***')
        if hasattr(self.alerter, '_redact'):
            text = self.alerter._redact(text)
        text = re.sub(r'(?i)(["\']?(?:sign|signature|ONDO-SIGN)["\']?\s*[:=]\s*["\']?)'
                      r'[0-9a-f]{64}', r'\1***', text)
        record.msg, record.args, record.exc_info, record.exc_text = text, (), None, None
        return True


def _configure_logger(settings, dependencies, *, api=None, alerter=None):
    """启动前即可配置文件和标准错误输出，组装失败同样留下脱敏原因。"""
    logger = dependencies.get('logger')
    if logger is None:
        logger = logging.getLogger('pendle.maker.' + settings.account)
        logger.setLevel(settings.log_level)
        logger.propagate = False
        Path(settings.data_dir).mkdir(parents=True, exist_ok=True)
        target = str((Path(settings.data_dir) / 'maker.log').resolve())
        formatter = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
        for handler in tuple(logger.handlers):
            if getattr(handler, '_maker_file', False) and handler.baseFilename != target:
                logger.removeHandler(handler)
                handler.close()
        if not any(isinstance(handler, logging.FileHandler) and handler.baseFilename == target
                   for handler in logger.handlers):
            handler = logging.FileHandler(target, encoding='utf-8')
            handler._maker_file = True
            handler.setFormatter(formatter)
            logger.addHandler(handler)
        stderr_handler = next((handler for handler in logger.handlers
                               if getattr(handler, '_maker_stderr', False)), None)
        if stderr_handler is None:
            stderr_handler = logging.StreamHandler(sys.stderr)
            stderr_handler._maker_stderr = True
            stderr_handler.setFormatter(formatter)
            logger.addHandler(stderr_handler)
        else:
            stderr_handler.stream = sys.stderr
    env = dependencies.get('env', os.environ)
    secrets = [env.get(key) for key in ('ONDO_API_KEY', 'ONDO_API_SECRET',
                                       'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID')]
    secrets.append(settings.healthcheck_url)
    logger.addFilter(MakerSecretFilter(getattr(api, 'credentials', None), alerter,
                                      secrets=secrets))
    return logger


def build_maker(settings, **dependencies):
    """组装依赖，不建立网络连接；实际启动由 Maker.run 执行。"""
    from .maker import Maker
    api, alerter = _build_api(settings, dependencies), _build_alerter(settings, dependencies)
    gate = api.gate if api is not None else dependencies.get('gate', SendGate())

    async def connect(url, **kwargs):
        return await websocket_connect(url, gate=gate, **kwargs)

    options = {key: dependencies[key] for key in
               ('rules', 'files', 'monotonic', 'clock', 'sleep', 'logger') if key in dependencies}
    if 'logger' not in options and 'maker_factory' not in dependencies:
        options['logger'] = _configure_logger(settings, dependencies, api=api, alerter=alerter)
    elif options.get('logger') is not None:
        _configure_logger(settings, dependencies, api=api, alerter=alerter)
    maker = dependencies.get('maker_factory', Maker)(settings, api=api, alerter=alerter,
        connect=dependencies.get('connect', connect),
        public_http=dependencies.get('public_http', dependencies.get('http', public_http)),
        **options)
    if api is None and hasattr(maker, 'gate'):
        gate = maker.gate
    return maker


def run_maker(settings, *, live=False, **dependencies):
    try:
        maker = build_maker(settings, **dependencies)
    except Exception as error:
        logger = _configure_logger(settings, dependencies)
        logger.error('启动失败：%s', error)
        return 2
    return asyncio.run(maker.run(live=live))


def _persist_emergency_halt(settings, files, api):
    try:
        if not files.exists(settings.halt_path):
            files.create_halt(settings.halt_path, {'reason': '应急命令无法确认安全状态'})
    except Exception:
        pass
    api.gate.close()


async def _cancel_confirm(settings, api, files):
    for _ in range(3):
        try:
            await api.cancel_all(settings.market, stop=True)
            rows = await api.get_open_orders(settings.market, stop=True)
            if isinstance(rows, list) and not rows:
                return 0
        except ApiError as error:
            if error.category == 'halt':
                _persist_emergency_halt(settings, files, api)
                return 76
        except Exception:
            pass
    return 1


def _remove_halt(files, path):
    if hasattr(files, 'remove_halt'):
        files.remove_halt(path)
        return
    try:
        files.fs.unlink(str(path))
    except FileNotFoundError:
        pass
    files._sync_directory(str(Path(path).parent))


def _emergency(settings, *, reset=False, **dependencies):
    if settings.mode == 'dry-run':
        raise ValueError('应急撤单和人工复位要求显式 sandbox 或 live 配置；dry-run 禁止写请求')
    files = dependencies.get('files') or DurableFiles()
    with SingleInstanceLock(settings.instance_lock_path, files=files):
        api = _build_api(settings, dependencies)
        if api is None:
            raise ValueError('应急命令必须提供 API 密钥')

        async def bounded_cancel():
            try:
                async with asyncio.timeout(settings.flatten_timeout_s):
                    return await _cancel_confirm(settings, api, files)
            except TimeoutError:
                return 1

        result = asyncio.run(bounded_cancel())
        if result != 0 or not reset:
            return result
        try:
            _remove_halt(files, settings.halt_path)
        except Exception:
            _persist_emergency_halt(settings, files, api)
            return 76
        return 0


def run_cancel_all(settings, **dependencies):
    """持有账户实例锁，最多三轮全部撤单并完整确认零挂单；不启动 WS。"""
    return _emergency(settings, **dependencies)


def reset_halt(settings, **dependencies):
    """人工确认后执行：撤单确认零挂单，才删除熔断锁并同步父目录。"""
    return _emergency(settings, reset=True, **dependencies)
