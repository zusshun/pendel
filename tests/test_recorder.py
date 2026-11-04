import tempfile
import unittest
from pathlib import Path

from pendle.config import Settings
from pendle.market import Book, MarketError, RateLimited
from pendle.recorder import Recorder
from pendle.storage import Sample, Store
from test_market import fixture


def metadata():
    return {'symbol': 'INTC', 'ondo_market': 'INTC-USD.P',
            'ondo_contract': fixture('ondo_contracts.json')['result'][0],
            'lighter_market': fixture('lighter_markets.json')['order_books'][0]}


def book(venue, at=1000, bid='99', ask='101', size='10'):
    return Book.make(venue, 'INTC', at, None, [(bid, size)], [(ask, size)])


class StoreTests(unittest.TestCase):
    def test_committed_samples_survive_reopening_with_raw_and_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sample.sqlite'
            sample = Sample(1100, book('ondo', 1000), None,
                            {'lighter': '接口失败'}, {'ondo': {'raw': 'preserved'}})
            with Store(path) as store:
                run_id = store.create_run(Settings(), metadata(), 'test')
                store.append(run_id, sample)
            with Store(path, create=False) as store:
                run, rows = store.load_run(run_id)
                self.assertEqual(rows, [sample])
                self.assertEqual(run['metadata'], metadata())
                self.assertEqual(run['schema_version'], 1)
                self.assertEqual(store.load_run()[0]['id'], run_id)

    def test_read_only_open_does_not_create_missing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'missing.sqlite'
            with self.assertRaises(ValueError):
                Store(path, create=False)
            self.assertFalse(path.exists())


class FakeTime:
    def __init__(self):
        self.value = 1000.0
        self.sleeps = []
    def now(self):
        return self.value
    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += seconds


class RecorderTests(unittest.TestCase):
    def test_failed_sample_is_saved_and_old_book_is_not_reused(self):
        clock = FakeTime()
        class Sources:
            def setup(self):
                return metadata()
            def fetch(self, venue):
                if venue == 'lighter':
                    raise RateLimited(7)
                return book('ondo', int(clock.now() * 1000)), {'raw': 1}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'record.sqlite'
            result = Recorder(Settings(), Sources(), now=clock.now, sleep=clock.sleep).record(path, 10)
            with Store(path, create=False) as store:
                _, rows = store.load_run(result['run_id'])
            self.assertGreaterEqual(len(rows), 1)
            self.assertTrue(all(row.lighter is None and 'lighter' in row.errors for row in rows))
            self.assertTrue(all(row.raw['ondo'] == {'raw': 1} for row in rows))
            self.assertIn(7, clock.sleeps)

    def test_setup_failure_leaves_session_error(self):
        class Sources:
            def setup(self):
                raise MarketError('元数据不可用')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'record.sqlite'
            with self.assertRaises(MarketError):
                Recorder(Settings(), Sources()).record(path, 1)
            with Store(path, create=False) as store:
                _, rows = store.load_run()
            self.assertIn('setup', rows[0].errors)

    def test_ctrl_c_preserves_completed_data(self):
        clock = FakeTime()
        class Sources:
            def setup(self):
                return metadata()
            def fetch(self, venue):
                return book(venue, int(clock.now() * 1000)), {}
        def stop(_):
            raise KeyboardInterrupt
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'record.sqlite'
            result = Recorder(Settings(), Sources(), now=clock.now, sleep=stop).record(path, 10)
            with Store(path, create=False) as store:
                _, rows = store.load_run()
            self.assertEqual(result['status'], 'interrupted')
            self.assertEqual(len(rows), 1)
