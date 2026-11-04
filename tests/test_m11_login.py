"""Verify login requests using real private message samples without opening network connections."""

import hashlib
import hmac
import json
from pathlib import Path
import unittest

from pendle import ondo_api as api


SAMPLES = json.loads((Path(__file__).parent / 'fixtures' / 'live_samples' /
                      'ws_private_samples.json').read_text(encoding='utf-8'))


class PrivateLoginSampleTests(unittest.TestCase):
    def test_login_matches_live_sample_string_milliseconds_and_signature(self):
        sample = SAMPLES['login_request_format']
        timestamp = sample['args']['time']
        secret = '仅用于测试的私有登录密钥'
        client = api.OndoAPI(api.Credentials('test-key', secret),
                             transport=None, clock=lambda: int(timestamp) / 1000)

        request = client.ws_login()

        self.assertEqual(request['op'], sample['op'])
        self.assertEqual(set(request['args']), set(sample['args']))
        self.assertIsInstance(request['args']['time'], str)
        self.assertEqual(request['args']['time'], timestamp)
        expected_signature = hmac.new(
            secret.encode(), (request['args']['time'] + 'ondo_perps_ws_login').encode(),
            hashlib.sha256).hexdigest()
        self.assertEqual(request['args']['sign'], expected_signature)
        self.assertIsInstance(json.loads(json.dumps(request))['args']['time'], str)


if __name__ == '__main__':
    unittest.main()
