"""命令行模式必须双重确认实盘，并能进行人工应急操作。"""

import contextlib
import io
import tempfile
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from pendle.cli import main
from pendle.maker_risk import AlreadyRunning


class MakerCliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = Path(self.directory.name) / 'maker.toml'
        self.config.write_text('[maker]\nmode="dry-run"\n')
        self.runner = SimpleNamespace(run_maker=Mock(return_value=0),
                                      run_cancel_all=Mock(return_value=0),
                                      reset_halt=Mock(return_value=0))
        original = sys.modules.get('pendle.maker')
        sys.modules['pendle.maker'] = self.runner

        def restore():
            if original is None:
                sys.modules.pop('pendle.maker', None)
            else:
                sys.modules['pendle.maker'] = original
        self.addCleanup(restore)

    def invoke(self, *args):
        output, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            result = main(list(args))
        return result, output.getvalue(), error.getvalue()

    def test_default_mode_is_dry_run_and_does_not_opt_into_live(self):
        result, _, error = self.invoke('maker', '--config', str(self.config))
        self.assertEqual(result, 0, error)
        settings = self.runner.run_maker.call_args.args[0]
        self.assertEqual(settings.mode, 'dry-run')
        self.assertEqual(self.runner.run_maker.call_args.kwargs, {'live': False})
        self.runner.run_cancel_all.assert_not_called()

    def test_live_cli_flag_cannot_upgrade_dry_run_or_sandbox(self):
        for mode in ('dry-run', 'sandbox'):
            self.config.write_text(f'[maker]\nmode="{mode}"\n')
            result, _, error = self.invoke('maker', '--config', str(self.config), '--live')
            self.assertEqual(result, 2)
            self.assertIn('live', error)
        self.runner.run_maker.assert_not_called()

    def test_live_config_requires_cli_flag(self):
        self.config.write_text('[maker]\nmode="live"\nguards_calibrated=true\n'
                               'sandbox_verified=true\nhealthcheck_url="https://example.com/check"\n')
        result, _, error = self.invoke('maker', '--config', str(self.config))
        self.assertEqual(result, 2)
        self.assertIn('--live', error)
        self.runner.run_maker.assert_not_called()
        result, _, error = self.invoke('maker', '--config', str(self.config), '--live')
        self.assertEqual(result, 0, error)
        self.assertEqual(self.runner.run_maker.call_args.kwargs, {'live': True})

    def test_regular_start_requires_explicit_config(self):
        result, _, error = self.invoke('maker')
        self.assertEqual(result, 2)
        self.assertIn('--config', error)
        self.runner.run_maker.assert_not_called()

    def test_cancel_all_routes_only_to_emergency_entry(self):
        result, _, error = self.invoke('maker-cancel-all', '--config', str(self.config))
        self.assertEqual(result, 0, error)
        self.assertEqual(self.runner.run_cancel_all.call_count, 1)
        self.runner.run_maker.assert_not_called()

    def test_reset_halt_without_config_is_rejected(self):
        result, _, error = self.invoke('maker', '--reset-halt')
        self.assertEqual(result, 2, error)
        self.assertIn('--config', error)
        self.runner.reset_halt.assert_not_called()
        self.runner.run_maker.assert_not_called()

    def test_reset_halt_keeps_explicit_account_and_rejects_live_switch(self):
        self.config.write_text('[maker]\naccount="server-main"\n')
        result, _, error = self.invoke('maker', '--config', str(self.config), '--reset-halt')
        self.assertEqual(result, 0, error)
        self.assertEqual(self.runner.reset_halt.call_args.args[0].account, 'server-main')
        result, _, error = self.invoke('maker', '--reset-halt', '--live')
        self.assertEqual(result, 2)
        self.assertIn('复位', error)

    def test_instance_conflict_reports_chinese_error(self):
        self.runner.run_cancel_all.side_effect = AlreadyRunning('同一账户已有机器人实例')
        result, _, error = self.invoke('maker-cancel-all', '--config', str(self.config))
        self.assertEqual(result, 2)
        self.assertIn('已有机器人实例', error)
        self.assertNotIn('Traceback', error)

    def test_emergency_cancel_and_reset_do_not_require_live_startup_flags(self):
        self.config.write_text('[maker]\nmode="live"\nguards_calibrated=false\n')
        result, _, error = self.invoke('maker-cancel-all', '--config', str(self.config))
        self.assertEqual(result, 0, error)
        settings = self.runner.run_cancel_all.call_args.args[0]
        self.assertEqual(settings.mode, 'live')
        self.assertEqual(settings.rest_url, 'https://api.ondoperps.xyz')
        result, _, error = self.invoke('maker', '--config', str(self.config), '--reset-halt')
        self.assertEqual(result, 0, error)
        self.assertEqual(self.runner.reset_halt.call_args.args[0].mode, 'live')
        result, _, error = self.invoke('maker', '--config', str(self.config), '--live')
        self.assertEqual(result, 2, error)
        self.runner.run_maker.assert_not_called()

    def test_runner_halt_exit_status_is_preserved(self):
        self.runner.run_maker.return_value = 76
        result, _, _ = self.invoke('maker', '--config', str(self.config))
        self.assertEqual(result, 76)

    def test_help_discloses_default_readonly_mode_and_emergency_commands(self):
        with self.assertRaises(SystemExit) as stopped:
            _, output, _ = self.invoke('--help')
        self.assertEqual(stopped.exception.code, 0)
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream), self.assertRaises(SystemExit):
            main(['maker', '--help'])
        self.assertIn('dry-run', stream.getvalue())
        self.assertIn('--reset-halt', stream.getvalue())


if __name__ == '__main__':
    unittest.main()
