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
        def execute(sql):
            main.request(ch.url + '/', sql.encode(), ch.headers)
        try:
            execute(f'CREATE DATABASE {main.ident(database)}')
            for level_type, time_type in (('Int32', "DateTime('UTC')"), ('Int32', "DateTime64(3, 'UTC')"), ('String', "DateTime64(3, 'UTC')")):
                with self.subTest(level_type=level_type, time_type=time_type):
                    table = 'logs_' + level_type.lower() + ('_millis' if 'DateTime64' in time_type else '_seconds')
                    quoted = main.ident(database) + '.' + main.ident(table)
                    execute(f'CREATE TABLE {quoted} (EventTime {time_type}, LogLevel {level_type}) ENGINE = Memory')
                    self.assertIn(table, ch.tables())
                    execute(f"INSERT INTO {quoted} VALUES ('2025-01-01 00:00:00',4), ('2025-01-01 00:00:01',5), ('2025-01-01 00:01:00',3), ('2025-01-01 00:15:00',4), ('2024-12-31 23:59:59',4)")
                    self.assertEqual(ch.count(table, dt.datetime(2025,1,1,0,15,tzinfo=main.UTC)), {'error': 1, 'critical': 1, 'total': 2})
                    count_sql, _, _ = main.investigation_queries(database, table, dt.datetime(2025,1,1,0,15,tzinfo=main.UTC))
                    result = ch.query(count_sql.rstrip(';') + ' FORMAT JSON')[0]
                    self.assertEqual((int(result['errors']), int(result['critical']), int(result['total'])), (1, 1, 2))
                    self.assertEqual(result['first_error'], '2025-01-01 00:00:00' + ('.000' if 'DateTime64' in time_type else ''))
                    empty_sql, _, _ = main.investigation_queries(database, table, dt.datetime(2025,1,1,1,0,tzinfo=main.UTC))
                    empty = ch.query(empty_sql.rstrip(';') + ' FORMAT JSON')[0]
                    self.assertEqual(int(empty['total']), 0)
                    self.assertIsNone(empty['first_error'])
                    self.assertIsNone(empty['last_error'])
                    if level_type == 'String':
                        execute(f"INSERT INTO {quoted} VALUES ('2025-01-01 00:02:00','Error')")
                        with self.assertRaises(RuntimeError):
                            ch.count(table, dt.datetime(2025,1,1,0,15,tzinfo=main.UTC))
        finally:
            execute(f'DROP DATABASE IF EXISTS {main.ident(database)} SYNC')
