"""Sibling instance configuration keeps opt-in safety limits explicit."""

import tempfile
import unittest
from pathlib import Path

from pendle.maker_config import MakerSettings


class SiblingConfigTests(unittest.TestCase):
    def test_default_sibling_mode_is_disabled_and_heartbeat_is_per_instance(self):
        settings = MakerSettings(account='main', data_dir='data/ibm')
        self.assertEqual(getattr(settings, 'sibling_markets', None), ())
        self.assertEqual(getattr(settings, 'heartbeat_path', None),
                         Path('data/ibm/main.IBM-USD.P.heartbeat.json'))
        self.assertEqual(getattr(settings, 'peer_heartbeat_paths', None), {})
        self.assertEqual(getattr(settings, 'peer_stale_s', None), 20.0)
        self.assertEqual(getattr(settings, 'peer_cancel_timeout_s', None), 15.0)
        self.assertEqual(getattr(settings, 'stop_sweep_interval_s', None), 5.0)

    def test_sibling_list_and_peer_paths_are_normalized_and_immutable(self):
        values = ['RDDT-USD.P']
        paths = {'RDDT-USD.P': 'data/rddt/main.RDDT-USD.P.heartbeat.json'}
        settings = MakerSettings(sibling_markets=values, peer_heartbeat_paths=paths,
                                 heartbeat_path='data/ibm/heartbeat.json')
        values.clear()
        paths.clear()
        self.assertEqual(settings.sibling_markets, ('RDDT-USD.P',))
        self.assertEqual(settings.heartbeat_path, Path('data/ibm/heartbeat.json'))
        self.assertEqual(settings.peer_heartbeat_paths['RDDT-USD.P'],
                         Path('data/rddt/main.RDDT-USD.P.heartbeat.json'))
        with self.assertRaises(TypeError):
            settings.peer_heartbeat_paths['RDDT-USD.P'] = Path('other')
        tuple_settings = MakerSettings(sibling_markets=('RDDT-USD.P',),
                                      peer_heartbeat_paths=settings.peer_heartbeat_paths)
        self.assertEqual(tuple_settings.sibling_markets, settings.sibling_markets)

    def test_sibling_markets_reject_wrong_container_invalid_market_self_and_duplicates(self):
        for value in ('RDDT-USD.P', None, {'RDDT-USD.P'}, ['IBM-USD.P'],
                      ['RDDT'], ['rddt-USD.P'], [False],
                      ['RDDT-USD.P', 'RDDT-USD.P']):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'sibling_markets'):
                MakerSettings(sibling_markets=value,
                              peer_heartbeat_paths={'RDDT-USD.P': 'data/rddt/heartbeat.json'})

    def test_peer_mapping_is_required_for_every_declared_sibling(self):
        for paths in ({}, [], None, {'TSLA-USD.P': 'tsla.json'},
                      {'RDDT-USD.P': ''}, {'RDDT-USD.P': ' '},
                      {'RDDT-USD.P': None}, {'RDDT-USD.P': False},
                      {'RDDT-USD.P': 'bad\x00path'}, {'RDDT': 'rddt.json'}):
            with self.subTest(paths=paths), self.assertRaisesRegex(ValueError,
                                                                  'peer_heartbeat_paths'):
                MakerSettings(sibling_markets=['RDDT-USD.P'], peer_heartbeat_paths=paths)

    def test_explicit_heartbeat_path_must_be_valid(self):
        for path in ('', ' ', False, 1, [], 'bad\x00path'):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'heartbeat_path'):
                MakerSettings(heartbeat_path=path)

    def test_sibling_timeouts_reject_nonpositive_nonfinite_and_boolean_values(self):
        for name in ('peer_stale_s', 'peer_cancel_timeout_s', 'stop_sweep_interval_s'):
            for value in (0, -1, True, False, float('nan'), float('inf'), float('-inf')):
                if name == 'stop_sweep_interval_s' and value == 0 and value is not False:
                    continue
                with self.subTest(name=name, value=value), self.assertRaisesRegex(
                        ValueError, f'{name}.*大于零的有限数字'):
                    MakerSettings.from_dict({name: value})

    def test_zero_stop_sweep_interval_disables_throttling(self):
        self.assertEqual(MakerSettings(stop_sweep_interval_s=0).stop_sweep_interval_s, 0.0)
        self.assertEqual(MakerSettings().stop_sweep_interval_s, 5.0)

    def test_peer_staleness_exceeds_actual_watchdog_check_interval(self):
        for value in (1, 5, 10):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'peer_stale_s.*watchdog'):
                MakerSettings(peer_stale_s=value)
        self.assertEqual(MakerSettings(peer_stale_s=10.1).peer_stale_s, 10.1)

    def test_sibling_toml_and_emergency_loading_share_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'maker.toml'
            path.write_text('[maker]\nmode="live"\nmarket="RDDT-USD.P"\n'
                            'data_dir="data/maker-rddt"\nsibling_markets=["IBM-USD.P"]\n'
                            'heartbeat_path="data/maker-rddt/rddt.json"\n'
                            'peer_heartbeat_paths={"IBM-USD.P"="data/maker/ibm.json"}\n'
                            'peer_stale_s=21.5\npeer_cancel_timeout_s=16.5\n'
                            'stop_sweep_interval_s=6.5\n')
            settings = MakerSettings.from_file(path, for_emergency=True)
            self.assertEqual(settings.market, 'RDDT-USD.P')
            self.assertEqual(settings.sibling_markets, ('IBM-USD.P',))
            self.assertEqual(settings.peer_heartbeat_paths['IBM-USD.P'], Path('data/maker/ibm.json'))
            self.assertEqual((settings.peer_stale_s, settings.peer_cancel_timeout_s,
                              settings.stop_sweep_interval_s), (21.5, 16.5, 6.5))

    def test_rddt_example_is_safe_separate_and_mutually_configured(self):
        root = Path(__file__).parents[1]
        settings = MakerSettings.from_file(root / 'maker-rddt.example.toml')
        ibm = MakerSettings.from_file(root / 'maker.example.toml')
        self.assertEqual(settings.mode, 'dry-run')
        self.assertEqual(settings.market, 'RDDT-USD.P')
        self.assertEqual(settings.binance_symbol, 'RDDTUSDT')
        self.assertEqual(settings.data_dir, Path('data/maker-rddt'))
        self.assertNotEqual(settings.data_dir, ibm.data_dir)
        self.assertEqual(settings.sibling_markets, ('IBM-USD.P',))
        self.assertEqual(settings.peer_heartbeat_paths['IBM-USD.P'], ibm.heartbeat_path)


if __name__ == '__main__':
    unittest.main()
