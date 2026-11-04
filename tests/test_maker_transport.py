"""In-memory tests for the real transport adapters without network calls."""

import asyncio
import io
import json
import logging
import stat
import threading
import tempfile
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path
from urllib.error import HTTPError, URLError

from pendle import maker_transport as transport
from pendle.maker_config import MakerSettings
from pendle.ondo_api import ApiError, Credentials, GateClosed, HttpRequest, HttpResponse, SendGate


class Response(io.BytesIO):
    def __init__(self, data=b'{"success":true,"result":[]}', status=200, headers=None):
        super().__init__(data)
        self.status, self.headers = status, headers or {'Date': 'Wed, 04 Nov 2026 00:00:00 GMT'}
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        return super().read(size)


class Opener:
    def __init__(self, response):
        self.response, self.calls = response, []

    def open(self, request, *, timeout):
        self.calls.append((request, timeout))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class TransportTests(unittest.IsolatedAsyncioTestCase):
    def request(self, url='https://api.example/v1/perps/orders'):
        return HttpRequest('POST', url, '/v1/perps/orders',
                           {'ONDO-KEY-ID': 'sensitive', 'ONDO-SIGN': 'a' * 64}, '{"a":1}')

    async def test_rest_preserves_exact_signed_body_and_headers(self):
        opener = Opener(Response())
        result = await transport.rest_transport(self.request(), opener=opener, timeout=3)
        self.assertEqual(result.status, 200)
        self.assertEqual(result.payload, {'success': True, 'result': []})
        request, timeout = opener.calls[0]
        self.assertEqual(request.data, b'{"a":1}')
        self.assertEqual(dict((key.lower(), value) for key, value in request.header_items())
                         ['ondo-sign'], 'a' * 64)
        self.assertEqual(timeout, 3)

    async def test_http_error_body_is_returned_for_api_classification(self):
        body = io.BytesIO(b'{"error_code":"order_not_found"}')
        opener = Opener(HTTPError('https://api.example', 400, 'sensitive', {}, body))
        result = await transport.rest_transport(self.request(), opener=opener)
        self.assertEqual(result.status, 400)
        self.assertEqual(result.payload['error_code'], 'order_not_found')

    async def test_redirects_are_rejected_without_following(self):
        for response in (Response(status=302), HTTPError('https://api.example', 307,
                         'sensitive', {'Location': 'https://evil.example'}, io.BytesIO(b''))):
            with self.subTest(response=type(response)), self.assertRaises(transport.NetworkError) as exc:
                await transport.rest_transport(self.request(), opener=Opener(response))
            self.assertIn('重定向', str(exc.exception))
            self.assertNotIn('sensitive', str(exc.exception))

    async def test_default_opener_explicitly_installs_redirect_blocker(self):
        opener = Opener(Response())
        with patch.object(transport, 'build_opener', return_value=opener) as build:
            await transport.rest_transport(self.request())
        handler = build.call_args.args[0]
        self.assertIsNone(handler.redirect_request(None, None, 302, '', {}, 'https://evil.example'))

    async def test_response_body_size_is_bounded(self):
        for response in (Response(b'x' * (5 * 1024 * 1024 + 1)),
                         Response(headers={'Content-Length': str(5 * 1024 * 1024 + 1)})):
            opener = Opener(response)
            with self.subTest(headers=response.headers), self.assertRaises(transport.NetworkError):
                await transport.rest_transport(self.request(), opener=opener)
            self.assertTrue(all(0 < size <= 5 * 1024 * 1024 + 1 for size in response.read_sizes))

    async def test_transport_failures_never_expose_url_headers_or_exception(self):
        for response in (URLError('sensitive'), Response(b'sensitive'), Response(b'\xff')):
            with self.subTest(response=type(response)), self.assertRaises(transport.NetworkError) as exc:
                await transport.rest_transport(self.request(), opener=Opener(response))
            self.assertNotIn('sensitive', str(exc.exception))
            self.assertNotIn('api.example', str(exc.exception))

    async def test_http_or_credentials_in_urls_are_rejected_before_io(self):
        for url in ('http://api.example', 'https://key:secret@api.example',
                    'https://api.example/#secret'):
            opener = Opener(Response())
            with self.subTest(url=url), self.assertRaises(transport.NetworkError):
                await transport.rest_transport(self.request(url), opener=opener)
            self.assertEqual(opener.calls, [])

    async def test_alert_post_serializes_json_and_health_get_accepts_plain_text(self):
        opener = Opener(Response(b'{"ok":true}'))
        response = await transport.alert_http('POST', 'https://telegram.example/send',
                                              json={'text': '中文'}, opener=opener)
        self.assertTrue(response.payload['ok'])
        self.assertEqual(json.loads(opener.calls[0][0].data), {'text': '中文'})
        opener = Opener(Response(b'OK'))
        response = await transport.alert_http('GET', 'https://health.example/ping', opener=opener)
        self.assertEqual(response.status, 200)
        self.assertIsNone(opener.calls[0][0].data)

    async def test_public_get_returns_market_json_payload(self):
        response = await transport.alert_http('GET', 'https://api.example/v1/markets',
            opener=Opener(Response(b'{"success":true,"result":[{"market":"IBM-USD.P"}]}')))
        self.assertEqual(response.payload['result'][0]['market'], 'IBM-USD.P')

    async def test_public_http_reads_live_market_sample_with_required_headers(self):
        self.assertTrue(callable(getattr(transport, 'public_http', None)))
        sample = (Path(__file__).parent / 'fixtures/live_samples/v1_markets_ibm.json').read_bytes()
        opener = Opener(Response(sample))
        response = await transport.public_http('GET', 'https://api.example/v1/markets',
                                               opener=opener)
        self.assertEqual(response.payload['result']['perps']['tradingPairs'][0]['market'],
                         'IBM-USD.P')
        request = opener.calls[0][0]
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(headers.get('user-agent'), 'pendle-maker/0.1')
        self.assertEqual(headers.get('accept'), 'application/json')
        self.assertEqual(request.get_method(), 'GET')
        self.assertIsNone(request.data)

    async def test_public_http_rejects_writes_or_body_before_io(self):
        self.assertTrue(callable(getattr(transport, 'public_http', None)))
        for method, body in (('POST', None), ('PUT', None), ('DELETE', None),
                             ('GET', {'market': 'IBM-USD.P'})):
            opener = Opener(Response())
            with self.subTest(method=method, body=body), self.assertRaises(transport.NetworkError):
                await transport.public_http(method, 'https://api.example/v1/markets',
                                            json=body, opener=opener)
            self.assertEqual(opener.calls, [])

    async def test_public_http_rejects_redirects_and_caps_response_size(self):
        self.assertTrue(callable(getattr(transport, 'public_http', None)))
        for response in (Response(status=302),
                         HTTPError('https://api.example', 307, '敏感内容',
                                   {'Location': 'https://evil.example'}, io.BytesIO(b'')),
                         Response(b'x' * (transport.MAX_RESPONSE_BYTES + 1)),
                         Response(headers={'Content-Length': str(transport.MAX_RESPONSE_BYTES + 1)})):
            opener = Opener(response)
            with self.subTest(response=type(response)), self.assertRaises(transport.NetworkError):
                await transport.public_http('GET', 'https://api.example/v1/markets',
                                            opener=opener)
            self.assertEqual(len(opener.calls), 1)
            if isinstance(response, Response):
                self.assertTrue(all(0 < size <= transport.MAX_RESPONSE_BYTES + 1
                                    for size in response.read_sizes))

    async def test_public_http_default_opener_explicitly_blocks_redirects(self):
        self.assertTrue(callable(getattr(transport, 'public_http', None)))
        with patch.object(transport, 'build_opener', return_value=Opener(Response())) as build:
            await transport.public_http('GET', 'https://api.example/v1/markets')
        handler = build.call_args.args[0]
        self.assertIsNone(handler.redirect_request(None, None, 302, '', {}, 'https://evil.example'))

    async def test_signed_rest_adds_required_user_agent_without_changing_signature_headers(self):
        signed = self.request()
        original_headers = dict(signed.headers)
        opener = Opener(Response())
        await transport.rest_transport(signed, opener=opener)
        headers = {key.lower(): value for key, value in opener.calls[0][0].header_items()}
        self.assertEqual(headers.get('user-agent'), 'pendle-maker/0.1')
        self.assertEqual(headers.get('ondo-sign'), 'a' * 64)
        self.assertEqual(signed.headers, original_headers)

    async def test_default_ws_has_no_auto_ping_and_aborts_on_closed_gate(self):
        calls, events = [], []
        connection = SimpleNamespace(transport=SimpleNamespace(abort=lambda: events.append('abort')))

        async def connect(url, **kwargs):
            calls.append((url, kwargs))
            return connection

        gate = SendGate()
        with patch.dict('sys.modules', {'websockets': SimpleNamespace(connect=connect)}):
            result = await transport.websocket_connect('wss://sandbox.example/ws', gate=gate)
        self.assertIs(result, connection)
        self.assertIsNone(calls[0][1]['ping_interval'])
        gate.close()
        self.assertEqual(events, ['abort'])

    async def test_ws_kwargs_cannot_reenable_ping_or_exceed_response_cap(self):
        calls = []

        async def connect(url, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(transport=SimpleNamespace(abort=lambda: None))

        with patch.dict('sys.modules', {'websockets': SimpleNamespace(connect=connect)}):
            await transport.websocket_connect('wss://api.example', ping_interval=1,
                ping_timeout=1, max_size=100 * 1024 * 1024, close_timeout=0.5)
        self.assertIsNone(calls[0]['ping_interval'])
        self.assertIsNone(calls[0]['ping_timeout'])
        self.assertEqual(calls[0]['max_size'], 5 * 1024 * 1024)
        self.assertEqual(calls[0]['close_timeout'], 0.5)

    async def test_cancelled_rest_remains_in_flight_until_thread_really_finishes(self):
        entered, release = threading.Event(), threading.Event()

        class BlockingOpener:
            def open(self, request, *, timeout):
                entered.set()
                release.wait(2)
                return Response()

        task = asyncio.create_task(transport.rest_transport(self.request(), opener=BlockingOpener()))
        try:
            while not entered.is_set():
                await asyncio.sleep(0)
            task.cancel()
            for _ in range(10):
                await asyncio.sleep(0)
            self.assertFalse(task.done())
        finally:
            release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task


class FakeMaker:
    def __init__(self, settings, **kwargs):
        self.settings, self.dependencies = settings, kwargs

    async def run(self, *, live=False):
        self.live = live
        return 7


class Files:
    def __init__(self):
        self.events, self.halted = [], True

    def acquire_instance(self, path):
        self.events.append('instance-acquire')
        return 1

    def release_instance(self, handle):
        self.events.append('instance-release')

    def exists(self, path):
        return self.halted

    def create_halt(self, path, value):
        self.events.append('halt-fsync')
        self.halted = True

    def remove_halt(self, path):
        self.events.extend(('unlink-halt', 'parent-fsync'))
        self.halted = False


class API:
    def __init__(self, files):
        self.files, self.gate = files, SendGate()
        self.rows, self.error, self.cancel_count = [], None, 0

    async def cancel_all(self, market, *, stop=False):
        self.files.events.append('cancel')
        self.cancel_count += 1
        if self.error:
            raise self.error
        return []

    async def get_open_orders(self, market, *, stop=False):
        self.files.events.append('verify')
        return self.rows


class EntrypointTests(unittest.TestCase):
    def test_default_logger_writes_redacted_reason_to_file_and_stderr(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = MakerSettings(data_dir=directory, account='m3-log-test')
            stderr = io.StringIO()
            with patch('sys.stderr', stderr):
                maker = transport.build_maker(settings, env={
                    'ONDO_API_KEY': 'private-key', 'ONDO_API_SECRET': 'private-secret',
                    'TELEGRAM_BOT_TOKEN': 'private-bot', 'TELEGRAM_CHAT_ID': 'private-chat'})
                maker.logger.error('启动失败：私有登录失败 private-key private-secret '
                    'private-bot private-chat sign=' + 'a' * 64)
            text = (Path(directory) / 'maker.log').read_text(encoding='utf-8')
            for destination in (text, stderr.getvalue()):
                self.assertIn('启动失败：私有登录失败', destination)
                for secret in ('private-key', 'private-secret', 'private-bot',
                               'private-chat', 'a' * 64):
                    self.assertNotIn(secret, destination)

    def test_build_failure_reports_redacted_reason_before_maker_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = MakerSettings(data_dir=directory, account='m3-build-failure')
            stderr = io.StringIO()
            with patch('sys.stderr', stderr), patch.object(transport, '_build_api',
                    side_effect=RuntimeError('认证初始化失败 private-key private-secret')):
                result = transport.run_maker(settings, env={
                    'ONDO_API_KEY': 'private-key', 'ONDO_API_SECRET': 'private-secret'})
            self.assertEqual(result, 2)
            text = (Path(directory) / 'maker.log').read_text(encoding='utf-8')
            for destination in (text, stderr.getvalue()):
                self.assertIn('启动失败：认证初始化失败', destination)
                self.assertNotIn('private-key', destination)
                self.assertNotIn('private-secret', destination)

    def test_dry_build_without_credentials_is_public_only(self):
        maker = transport.build_maker(MakerSettings(), env={}, maker_factory=FakeMaker)
        self.assertIsNone(maker.dependencies['api'])
        self.assertIsNotNone(maker.dependencies['alerter'])

    def test_default_build_uses_dedicated_public_http(self):
        maker = transport.build_maker(MakerSettings(), env={}, maker_factory=FakeMaker)
        self.assertEqual(maker.dependencies['public_http'].__name__, 'public_http')
        self.assertIsNot(maker.dependencies['public_http'], transport.alert_http)

    def test_explicit_public_http_overrides_legacy_http_injection(self):
        async def public(*args, **kwargs):
            return HttpResponse(200, {})

        async def legacy(*args, **kwargs):
            return HttpResponse(200, {})

        maker = transport.build_maker(MakerSettings(), env={}, maker_factory=FakeMaker,
                                     public_http=public, http=legacy)
        self.assertIs(maker.dependencies['public_http'], public)
        self.assertIs(maker.dependencies['alerter']._http, legacy)

    def test_legacy_http_injection_remains_compatible(self):
        async def legacy(*args, **kwargs):
            return HttpResponse(200, {})

        maker = transport.build_maker(MakerSettings(), env={}, maker_factory=FakeMaker,
                                     http=legacy)
        self.assertIs(maker.dependencies['public_http'], legacy)
        self.assertIs(maker.dependencies['alerter']._http, legacy)

    def test_sandbox_build_selects_custom_urls_and_injected_dependencies(self):
        settings = MakerSettings(mode='sandbox', sandbox_rest_url='https://sandbox.example',
                                 sandbox_ws_url='wss://sandbox.example/custom')
        maker = transport.build_maker(settings, maker_factory=FakeMaker,
            env={'ONDO_API_KEY': 'key', 'ONDO_API_SECRET': 'secret'}, ntp_check=lambda: True,
            transport=lambda request: None, http=lambda *args, **kwargs: None)
        self.assertEqual(maker.dependencies['api'].base_url, 'https://sandbox.example')
        self.assertEqual(maker.dependencies['api'].ws_url, 'wss://sandbox.example/custom')

    def test_dry_partial_credentials_are_not_silently_ignored(self):
        with self.assertRaises(ValueError):
            transport.build_maker(MakerSettings(), env={'ONDO_API_KEY': 'key'},
                                  maker_factory=FakeMaker)

    def test_secret_file_credentials_and_telegram_are_shared(self):
        content = json.dumps({'key': 'key', 'secret': 'secret',
                              'telegram_bot_token': 'bot', 'telegram_chat_id': 'chat'})
        maker = transport.build_maker(MakerSettings(mode='sandbox', secret_file='secret.json'),
            env={}, maker_factory=FakeMaker,
            file_reader=lambda path: (stat.S_IFREG | 0o600, content))
        self.assertEqual(maker.dependencies['api'].credentials, Credentials('key', 'secret'))

    def test_run_maker_delegates_startup_and_shutdown_to_maker_run(self):
        self.assertEqual(transport.run_maker(MakerSettings(), env={}, maker_factory=FakeMaker), 7)

    def test_cancel_all_holds_instance_lock_through_empty_order_confirmation(self):
        files = Files()
        api = API(files)
        result = transport.run_cancel_all(MakerSettings(mode='sandbox'), api=api, files=files)
        self.assertEqual(result, 0)
        self.assertEqual(files.events, ['instance-acquire', 'cancel', 'verify', 'instance-release'])
        self.assertTrue(files.halted)

    def test_cancel_all_retries_only_three_times_and_does_not_clear_halt(self):
        files = Files()
        api = API(files)
        api.rows = [{'clientOrderId': 'remaining'}]
        self.assertEqual(transport.run_cancel_all(MakerSettings(mode='sandbox'),
                                                api=api, files=files), 1)
        self.assertEqual(api.cancel_count, 3)
        self.assertTrue(files.halted)

    def test_halt_error_stops_retries_and_preserves_halt(self):
        files = Files()
        api = API(files)
        api.error = ApiError(401, 'ip_not_permitted')
        self.assertEqual(transport.run_cancel_all(MakerSettings(mode='sandbox'),
                                                api=api, files=files), 76)
        self.assertEqual(api.cancel_count, 1)
        self.assertFalse(api.gate.is_open)
        self.assertTrue(files.halted)

    def test_reset_halt_removes_lock_only_after_real_empty_order_confirmation(self):
        files = Files()
        api = API(files)
        self.assertEqual(transport.reset_halt(MakerSettings(mode='sandbox'), api=api, files=files), 0)
        self.assertEqual(files.events, ['instance-acquire', 'cancel', 'verify', 'unlink-halt',
                                       'parent-fsync', 'instance-release'])
        self.assertFalse(files.halted)

    def test_failed_reset_keeps_halt_and_dry_helpers_never_write(self):
        files = Files()
        api = API(files)
        api.rows = [{}]
        self.assertEqual(transport.reset_halt(MakerSettings(mode='sandbox'), api=api, files=files), 1)
        self.assertTrue(files.halted)
        for command in (transport.reset_halt, transport.run_cancel_all):
            files.events.clear()
            with self.assertRaises(ValueError):
                command(MakerSettings(), api=api, files=files)
            self.assertEqual(files.events, [])

    def test_dry_build_binds_ws_abort_to_actual_maker_gate(self):
        class GateMaker(FakeMaker):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.gate = SendGate()

        seen = []

        async def connect(url, *, gate):
            seen.append(gate)
            return object()

        maker = transport.build_maker(MakerSettings(), env={}, maker_factory=GateMaker)
        with patch.object(transport, 'websocket_connect', side_effect=connect):
            asyncio.run(maker.dependencies['connect']('wss://api.example'))
        self.assertIs(seen[0], maker.gate)

    def test_logger_filter_redacts_ondo_telegram_and_tracebacks(self):
        from pendle.alerts import Alerter, TelegramCredentials

        filter_ = transport.MakerSecretFilter(Credentials('api-key', 'api-secret'),
            Alerter(TelegramCredentials('bot-token', 'chat-id')))
        record = logging.LogRecord('maker', logging.ERROR, '', 0,
            'api-key api-secret bot-token chat-id sign=' + 'a' * 64, (),
            (RuntimeError, RuntimeError('bot-token'), None))
        filter_.filter(record)
        for secret in ('api-key', 'api-secret', 'bot-token', 'chat-id', 'a' * 64):
            self.assertNotIn(secret, record.getMessage())
        self.assertIsNone(record.exc_info)

    def test_sandbox_build_revalidates_mutated_prod_hostname_before_transport(self):
        settings = MakerSettings(mode='sandbox')
        object.__setattr__(settings, 'sandbox_rest_url', 'https://api.ondoperps.xyz')
        with self.assertRaises(ValueError):
            transport.build_maker(settings, env={}, maker_factory=FakeMaker)

    def test_reset_fsync_failure_recreates_halt_and_returns_76(self):
        class BrokenFiles(Files):
            def remove_halt(self, path):
                self.halted = False
                raise OSError('父目录 fsync 失败')

        files = BrokenFiles()
        self.assertEqual(transport.reset_halt(MakerSettings(mode='sandbox'),
                                            api=API(files), files=files), 76)
        self.assertTrue(files.halted)
        self.assertIn('halt-fsync', files.events)

    def test_telegram_environment_works_alongside_ondo_only_secret_file(self):
        content = json.dumps({'key': 'key', 'secret': 'secret'})
        maker = transport.build_maker(MakerSettings(secret_file='ondo.json'),
            env={'TELEGRAM_BOT_TOKEN': 'bot', 'TELEGRAM_CHAT_ID': 'chat'},
            maker_factory=FakeMaker, file_reader=lambda path: (stat.S_IFREG | 0o600, content))
        self.assertEqual(maker.dependencies['alerter']._credentials.bot_token, 'bot')

    def test_dry_api_uses_read_only_gate_even_if_called_directly(self):
        async def reject_network(request):
            raise AssertionError('dry-run 不得到达传输')

        maker = transport.build_maker(MakerSettings(),
            env={'ONDO_API_KEY': 'key', 'ONDO_API_SECRET': 'secret'}, maker_factory=FakeMaker,
            transport=reject_network)
        api = maker.dependencies['api']
        for operation in (lambda: api.cancel_all('IBM-USD.P'),
                          lambda: api.place_order({'market': 'IBM-USD.P'})):
            with self.assertRaises(GateClosed):
                asyncio.run(operation())

    def test_real_maker_build_is_offline_and_default_connect_accepts_feed_kwargs(self):
        files = Files()
        logger = logging.getLogger('test.maker.build')
        maker = transport.build_maker(MakerSettings(), env={}, files=files, logger=logger)
        self.assertIsNone(maker.api)
        self.assertEqual(files.events, [])
        seen = []

        async def connect(url, *, gate, **kwargs):
            seen.append((gate, kwargs))
            return object()

        with patch.object(transport, 'websocket_connect', side_effect=connect):
            asyncio.run(maker.connect('wss://api.example', ping_interval=None,
                                     max_size=12345, close_timeout=0.5))
        self.assertIs(seen[0][0], maker.gate)
        self.assertEqual(seen[0][1]['max_size'], 12345)


if __name__ == '__main__':
    unittest.main()
