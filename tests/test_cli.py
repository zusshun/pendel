import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]


def command(*args):
    return subprocess.run([sys.executable, '-m', 'pendle', *map(str, args)],
                          cwd=ROOT, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'},
                          text=True, capture_output=True)


class CliTests(unittest.TestCase):
    def test_demo_then_read_only_replay_matches_report(self):
        with tempfile.TemporaryDirectory() as directory:
            db, report = Path(directory) / 'demo.sqlite', Path(directory) / 'report.json'
            completed = command('demo', '--db', db, '--report', report)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            saved = json.loads(report.read_text())
            self.assertGreater(saved['sample_count'], 1)
            self.assertTrue(any(e['type'] == 'hedge_fill' for e in saved['events']))
            replayed = command('replay', '--db', db)
            self.assertEqual(replayed.returncode, 0, replayed.stderr)
            self.assertEqual(json.loads(replayed.stdout), saved)

    def test_bad_config_and_missing_db_have_chinese_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'bad.toml'
            path.write_text('quote_notional = "NaN"\n')
            result = command('demo', '--config', path, '--db', Path(directory) / 'out.sqlite')
            self.assertEqual(result.returncode, 2)
            self.assertIn('有限数字', result.stderr)
            self.assertNotIn('Traceback', result.stderr)
            result = command('replay', '--db', Path(directory) / 'missing.sqlite')
            self.assertEqual(result.returncode, 2)
            self.assertIn('不存在', result.stderr)

    def test_report_must_not_overwrite_recording_or_config(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / 'demo.sqlite'
            self.assertEqual(command('demo', '--db', db).returncode, 0)
            before = db.read_bytes()
            result = command('replay', '--db', db, '--report', db)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(db.read_bytes(), before)
            config = Path(directory) / 'settings.toml'
            config.write_text('quote_notional = "50"\n')
            result = command('demo', '--db', db, '--config', config, '--report', config)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(config.read_text(), 'quote_notional = "50"\n')

    def test_help_and_example_config(self):
        result = command('--help')
        self.assertEqual(result.returncode, 0)
        self.assertIn('record', result.stdout)
        self.assertIn('回放', result.stdout)
        with tempfile.TemporaryDirectory() as directory:
            result = command('demo', '--config', ROOT / 'config.example.toml',
                             '--db', Path(directory) / 'demo.sqlite')
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_reward_only_override_keeps_recorded_nondefault_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            recorded = path / 'recorded.toml'
            recorded.write_text('quote_notional="50"\nondo_maker_fee_bps="4"\npoll_interval_s=3.0\n')
            override = path / 'reward.toml'
            override.write_text('reward_usdc="5"\n')
            db = path / 'demo.sqlite'
            self.assertEqual(command('demo', '--config', recorded, '--db', db).returncode, 0)
            result = command('replay', '--db', db, '--config', override)
            self.assertEqual(result.returncode, 0, result.stderr)
            settings = json.loads(result.stdout)['settings']
            self.assertEqual(settings['quote_notional'], '50')
            self.assertEqual(settings['ondo_maker_fee_bps'], '4')
            self.assertEqual(settings['poll_interval_s'], 3.0)
