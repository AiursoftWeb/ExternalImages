import datetime as dt
import os
import sys
import unittest
import uuid
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main


@unittest.skipUnless(os.environ.get('CH_TEST_URL'), 'set CH_TEST_URL to an isolated disposable ClickHouse instance')
class RealClickHouse(unittest.TestCase):
    def test_schema_window_levels_and_discovery(self):
        database = 'monitor_test_' + uuid.uuid4().hex
        ch = main.ClickHouse(os.environ['CH_TEST_URL'], database, os.environ.get('CH_TEST_USER', 'default'), os.environ.get('CH_TEST_PASSWORD', ''))
        table = 'logs'
        quoted = main.ident(database) + '.' + main.ident(table)
        def execute(sql):
            main.request(ch.url + '/', sql.encode(), ch.headers)
        try:
            execute(f'CREATE DATABASE {main.ident(database)}')
            execute(f'CREATE TABLE {quoted} (EventTime DateTime64(3, \'UTC\'), LogLevel Int32) ENGINE = Memory')
            self.assertIn(table, ch.tables())
            execute(f"INSERT INTO {quoted} VALUES ('2025-01-01 00:00:00',4), ('2025-01-01 00:00:01',5), ('2025-01-01 00:01:00',3), ('2025-01-01 00:15:00',4), ('2024-12-31 23:59:59',4)")
            self.assertEqual(ch.count(table, dt.datetime(2025,1,1,0,15,tzinfo=main.UTC)), 2)
        finally:
            execute(f'DROP DATABASE IF EXISTS {main.ident(database)} SYNC')
