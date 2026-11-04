"""M21 已取得 HTTP 状态后的响应解析失败必须保留真实状态。"""

import unittest

from pendle import maker_transport as transport
from pendle.ondo_api import ApiError, Credentials, HttpRequest, OndoAPI
from test_maker_transport import Opener, Response


class M21TransportFormatTests(unittest.IsolatedAsyncioTestCase):
    def request(self):
        return HttpRequest('POST', 'https://api.example/v1/perps/orders',
                           '/v1/perps/orders', {}, '{"market":"IBM-USD.P"}')

    async def test_json_and_utf8_parse_failures_keep_received_http_status(self):
        for status in (200, 401, 503):
            for body in (b'sensitive-invalid-json', b'\xff'):
                with self.subTest(status=status, body=body):
                    with self.assertRaises(transport.NetworkError) as caught:
                        await transport.rest_transport(self.request(),
                            opener=Opener(Response(body, status=status)))
                    error = caught.exception
                    self.assertEqual(getattr(error, 'status', None), status)
                    self.assertIs(getattr(error, 'response_format', None), True)
                    self.assertIn(f'HTTP {status}', str(error))
                    self.assertIn('响应格式不符', str(error))
                    self.assertNotIn('sensitive', str(error))
                    self.assertNotIn('api.example', str(error))

    async def test_api_classifies_parse_failure_using_received_http_status(self):
        for status, category in ((200, 'guard'), (401, 'halt'), (503, 'guard')):
            with self.subTest(status=status):
                opener = Opener(Response(b'sensitive-invalid-json', status=status))

                async def send(request):
                    return await transport.rest_transport(request, opener=opener)

                api = OndoAPI(Credentials('测试-key', '测试-secret'), transport=send,
                              rate=1_000_000)
                with self.assertRaises(ApiError) as caught:
                    await api.place_order({'market': 'IBM-USD.P'})
                error = caught.exception
                self.assertEqual(error.status, status)
                self.assertEqual(error.category, category)
                self.assertIs(getattr(error, 'response_format', None), True)
                self.assertIn(f'HTTP {status}', str(error))
                self.assertIn('响应格式不符', str(error))
                self.assertNotIn('sensitive', str(error))

    async def test_invalid_content_length_keeps_received_http_status(self):
        for status in (200, 401, 503):
            with self.subTest(status=status):
                opener = Opener(Response(status=status,
                    headers={'Content-Length': 'sensitive-invalid'}))
                with self.assertRaises(transport.NetworkError) as caught:
                    await transport.rest_transport(self.request(), opener=opener)
                error = caught.exception
                self.assertEqual(getattr(error, 'status', None), status)
                self.assertIs(getattr(error, 'response_format', None), True)
                self.assertIn(f'HTTP {status}', str(error))
                self.assertIn('响应格式不符', str(error))
                self.assertNotIn('sensitive', str(error))
                self.assertIsNone(error.__cause__)

    async def test_api_classifies_invalid_content_length_using_received_http_status(self):
        for status, category in ((200, 'guard'), (401, 'halt'), (503, 'guard')):
            with self.subTest(status=status):
                opener = Opener(Response(status=status,
                    headers={'Content-Length': 'sensitive-invalid'}))

                async def send(request):
                    return await transport.rest_transport(request, opener=opener)

                api = OndoAPI(Credentials('测试-key', '测试-secret'), transport=send,
                              rate=1_000_000)
                with self.assertRaises(ApiError) as caught:
                    await api.place_order({'market': 'IBM-USD.P'})
                error = caught.exception
                self.assertEqual(error.status, status)
                self.assertEqual(error.category, category)
                self.assertIs(getattr(error, 'response_format', None), True)
                self.assertIn(f'HTTP {status}', str(error))
                self.assertIn('响应格式不符', str(error))
                self.assertNotIn('sensitive', str(error))
