"""保存研究会话及逐次采样，回放以只读方式打开。"""

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__
from .market import Book

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class Sample:
    timestamp_ms: int
    ondo: Book | None
    lighter: Book | None
    errors: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)

    def to_dict(self):
        return {'timestamp_ms': self.timestamp_ms,
                'ondo': self.ondo.to_dict() if self.ondo else None,
                'lighter': self.lighter.to_dict() if self.lighter else None,
                'errors': self.errors, 'raw': self.raw}

    @classmethod
    def from_dict(cls, value):
        return cls(value['timestamp_ms'],
                   Book.from_dict(value['ondo']) if value['ondo'] else None,
                   Book.from_dict(value['lighter']) if value['lighter'] else None,
                   value.get('errors', {}), value.get('raw', {}))


class Store:
    def __init__(self, path, create=True):
        path = Path(path)
        if not create and not path.is_file():
            raise ValueError('录制数据库不存在：' + str(path))
        if create:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(path)
            self.connection.executescript('''
                CREATE TABLE IF NOT EXISTS runs (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL UNIQUE,
                    schema_version INTEGER NOT NULL,
                    engine_version TEXT NOT NULL,
                    created_ms INTEGER NOT NULL,
                    label TEXT NOT NULL,
                    settings TEXT NOT NULL,
                    metadata TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS samples_run ON samples(run_id, id);
            ''')
            self.connection.commit()
        else:
            self.connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
        self.connection.row_factory = sqlite3.Row

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.connection.close()

    def create_run(self, settings, metadata, label='record'):
        run_id = uuid.uuid4().hex
        self.connection.execute('''INSERT INTO runs
            (id,schema_version,engine_version,created_ms,label,settings,metadata)
            VALUES (?,?,?,?,?,?,?)''',
            (run_id, SCHEMA_VERSION, __version__, int(time.time() * 1000), label,
             json.dumps(settings.to_dict()), json.dumps(metadata)))
        self.connection.commit()
        return run_id

    def set_metadata(self, run_id, metadata):
        self.connection.execute('UPDATE runs SET metadata=? WHERE id=?',
                                (json.dumps(metadata), run_id))
        self.connection.commit()

    def append(self, run_id, sample):
        self.connection.execute('INSERT INTO samples(run_id,payload) VALUES (?,?)',
                                (run_id, json.dumps(sample.to_dict())))
        self.connection.commit()

    def get_run(self, run_id=None):
        if run_id is None:
            row = self.connection.execute('SELECT * FROM runs ORDER BY seq DESC LIMIT 1').fetchone()
        else:
            row = self.connection.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
        if row is None:
            raise ValueError('找不到录制会话')
        run = dict(row)
        run['settings'] = json.loads(run['settings'])
        run['metadata'] = json.loads(run['metadata'])
        return run

    def iter_samples(self, run_id):
        for sample in self.connection.execute(
                'SELECT payload FROM samples WHERE run_id=? ORDER BY id', (run_id,)):
            yield Sample.from_dict(json.loads(sample['payload']))

    def load_run(self, run_id=None):
        """小型测试／交互读取；命令行回放使用迭代接口。"""
        run = self.get_run(run_id)
        return run, list(self.iter_samples(run['id']))
