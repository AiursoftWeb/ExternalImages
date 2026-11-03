import datetime as dt
import json
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import main


class Mock(BaseHTTPRequestHandler):
    incidents = {}
    count = 3
    tables = ['app']
    requests = []
    lost = False

    def log_message(self, *args):
        pass

    def reply(self, value, status=200):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.requests.append(('GET', self.path))
        parsed = urlparse(self.path)
        if parsed.path == '/api/v1/incidents':
            q = parse_qs(parsed.query)
            rows = [x for x in self.incidents.values() if x['Status'] != 'resolved' and x['Metadata']['source'] == q['MetadataValue'][0]]
            page = int(q['Page'][0]); size = int(q['PageSize'][0])
            self.reply({'Code': 1, 'Items': rows[(page-1)*size:page*size], 'Page': page, 'PageSize': size, 'TotalCount': len(rows)})
        else:
            self.reply({'Code': 1, 'Incident': self.incidents[parsed.path.rsplit('/', 1)[1]]})

    def do_POST(self):
        body = self.rfile.read(int(self.headers['Content-Length']))
        self.requests.append(('POST', self.path, body.decode()))
        parsed = urlparse(self.path)
        if not parsed.path.startswith('/api/'):
            sql = body.decode()
            if 'system.databases' in sql:
                rows = [{'n': '1'}]
            elif 'system.tables' in sql:
                rows = [{'name': x} for x in self.tables]
            elif 'system.columns' in sql:
                rows = [{'name': 'EventTime', 'type': 'DateTime64(3)'}, {'name': 'LogLevel', 'type': 'Int32'}]
            else:
                rows = [{'error': str(self.count - self.count // 2), 'critical': str(self.count // 2), 'total': str(self.count)}]
            self.reply({'data': rows})
            return
        data = json.loads(body)
        if parsed.path == '/api/v1/incidents':
            existing = next((x for x in self.incidents.values() if x['ReportId'] == data['ReportId']), None)
            if not existing:
                existing = {'IncidentId': str(uuid.uuid4()), 'ReportId': data['ReportId'], 'Version': str(uuid.uuid4()), 'Status': 'new', 'Level': data['Level'], 'Metadata': data['Metadata'],
                            'Context': data.get('Context'), 'OccurredAtUtc': data.get('OccurredAtUtc')}
                self.incidents[existing['IncidentId']] = existing
            if self.lost:
                type(self).lost = False
                self.connection.shutdown(2)
                self.connection.close()
                return
            self.reply({'Code': 2, 'ReportId': data['ReportId'], 'IncidentId': existing['IncidentId']})
        else:
            _, action = parsed.path.rsplit('/', 1)
            incident_id = parsed.path.split('/')[-2]
            x = self.incidents[incident_id]
            if data['Version'] != x['Version']:
                self.reply({'Code': 3}, 409); return
            if action == 'resolve': x['Status'] = 'resolved'
            elif action == 'severity': x['Level'] = data['Level']
            else: raise AssertionError(action)
            x['Version'] = str(uuid.uuid4())
            self.reply({'Code': 2})


class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Mock)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True); cls.thread.start()
        cls.url = 'http://127.0.0.1:' + str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join()

    def setUp(self):
        Mock.incidents = {}; Mock.requests = []; Mock.count = 3; Mock.tables = ['app']; Mock.lost = False
        self.tmp = tempfile.TemporaryDirectory()
        self.store = main.Store(self.tmp.name, 'node', 'AspNetCoreLogs')
        self.ch = main.ClickHouse(self.url, 'AspNetCoreLogs', 'default', '')
        self.api = main.AlertCenter(self.url, 'secret')
        self.monitor = main.Monitor(self.ch, self.api, self.store, 'node', 'AspNetCoreLogs')

    def tearDown(self):
        self.store.close(); self.tmp.cleanup()

    def scan(self, n):
        Mock.count = n
        self.assertEqual(self.monitor.scan(dt.datetime(2025, 1, 1, tzinfo=main.UTC)), 0)

    def test_thresholds(self):
        self.assertEqual([main.severity(n) for n in (0,1,2,3,4,5,14,15,16)], [None,None,None,'sev4','sev4','sev3','sev3','sev2','sev2'])

    def test_diagnostic_payload_and_mutation_reasons(self):
        self.monitor.public_url = 'https://clickhouse.example.com'
        self.ch.samples = lambda *_: [{'EventTime': '2024-12-31 23:59:59', 'LogLevel': 5,
                                      'Category': 'Example.Service', 'Message': 'Request failed', 'Exception': 'ExampleException: failure'}]
        for count, level, threshold in ((3, 'sev4', 3), (5, 'sev3', 5), (15, 'sev2', 15)):
            self.scan(count)
            posts = [json.loads(r[2]) for r in Mock.requests if r[0] == 'POST' and r[1] == '/api/v1/incidents']
            if count == 3:
                payload = posts[0]
                context = payload['Context']
                self.assertEqual(context['WindowStartUtc'], '2024-12-31T23:45:00+00:00')
                self.assertEqual(context['WindowEndUtc'], '2025-01-01T00:00:00+00:00')
                self.assertEqual(context['WindowBounds'], '[start, end)')
                self.assertEqual((context['ErrorCount'], context['CriticalCount'], context['TotalCount']), (2, 1, 3))
                self.assertEqual((context['TriggerThreshold'], context['ThresholdLevel']), (threshold, level))
                self.assertNotIn('Message', context)
                self.assertNotIn('Exception', context)
                text = payload['Content']
                self.assertEqual(payload['Title'], '[node] app: 3 errors in 15 minutes')
                self.assertIn('ExampleException: failure', text)
                self.assertIn('ClickHouse query page: https://clickhouse.example.com/play', text)
                self.assertIn('FROM `AspNetCoreLogs`.`app`', text)
            else:
                text = json.loads(Mock.requests[-1][2])['Reason']
            self.assertIn('last 15 minutes', text)
            self.assertIn(f'{count} errors', text)
            self.assertIn('countIf(toInt32(LogLevel) = 4)', text)
            self.assertIn('Error detection window', text)
            self.assertIn('minOrNull(EventTime)', text)
            self.assertIn('maxOrNull(EventTime)', text)
            self.assertIn("EventTime >= toDateTime64('2024-12-31 23:45:00.000000', 6, 'UTC')", text)
            self.assertIn("EventTime < toDateTime64('2025-01-01 00:00:00.000000', 6, 'UTC')", text)
            self.assertIn('AND toInt32(LogLevel) IN (4, 5)', text)
            self.assertIn('LIMIT 20;', text)
            self.assertLessEqual(len(text.encode()), 4000)
        self.scan(0)
        self.assertIn('0 errors in the last 15 minutes (Error: 0; Critical: 0)', json.loads(Mock.requests[-1][2])['Reason'])

    def test_resolution_separates_original_failure_and_recovery_queries_after_restart(self):
        trigger = dt.datetime(2026, 11, 3, 1, 3, 48, 520503, tzinfo=main.UTC)
        recovery = dt.datetime(2026, 11, 3, 1, 18, 48, 521823, tzinfo=main.UTC)
        Mock.count = 15125
        self.assertEqual(self.monitor.scan(trigger), 0)
        creation = json.loads(next(r[2] for r in Mock.requests if r[0] == 'POST' and r[1] == '/api/v1/incidents'))
        self.store.close()
        self.store = main.Store(self.tmp.name, 'node', 'AspNetCoreLogs')
        self.monitor = main.Monitor(self.ch, self.api, self.store, 'node', 'AspNetCoreLogs')
        Mock.count = 0
        self.assertEqual(self.monitor.scan(recovery), 0)
        reason = json.loads(Mock.requests[-1][2])['Reason']
        original, recovered = reason.split('Verify recovery (expected total: 0; first_error and last_error: NULL):')
        self.assertIn('Recovery confirmed. Zero errors are expected', original)
        self.assertIn('this is not the original failure window', original)
        self.assertIn('Original incident trigger window (creation snapshot, UTC)', original)
        self.assertIn("EventTime >= toDateTime64('2026-11-03 00:48:48.520503'", original)
        self.assertIn("EventTime < toDateTime64('2026-11-03 01:03:48.520503'", original)
        self.assertIn('LIMIT 20;', original)
        self.assertIn("EventTime >= toDateTime64('2026-11-03 01:03:48.521823'", recovered)
        self.assertIn("EventTime < toDateTime64('2026-11-03 01:18:48.521823'", recovered)
        self.assertNotIn('00:48:48.520503', recovered)
        self.assertIn('minOrNull(EventTime)', recovered)
        self.assertLessEqual(len(reason.encode()), 4000)
        self.assertEqual(creation['Context']['WindowEndUtc'], trigger.isoformat())
        self.assertEqual(next(iter(Mock.incidents.values()))['Context'], creation['Context'])

    def test_legacy_recovery_uses_occurrence_or_reports_original_window_unavailable(self):
        for context, occurred, known in (({'WindowEndUtc': '2024-12-31T21:00:00-02:00'}, '2024-12-31T22:00:00Z', True),
                                        (None, '2024-12-31T23:00:00Z', True),
                                        ({'WindowEndUtc': 'invalid'}, '2024-12-31T23:00:00+00:00', True),
                                        ({'WindowEndUtc': '2024-12-31T23:00:00'}, None, False)):
            with self.subTest(context=context):
                self.scan(3)
                incident = next(x for x in Mock.incidents.values() if x['Status'] != 'resolved')
                incident['Context'], incident['OccurredAtUtc'] = context, occurred
                self.scan(0)
                reason = json.loads(Mock.requests[-1][2])['Reason']
                if known:
                    self.assertIn("EventTime >= toDateTime64('2024-12-31 22:45:00.000000'", reason)
                    self.assertIn('Original incident trigger window', reason)
                else:
                    self.assertIn('Original trigger window unavailable', reason)
                    self.assertNotIn('Investigate the original failure:\nSELECT', reason)

    def test_samples_are_redacted_and_bounded(self):
        self.ch.samples = lambda *_: [{'EventTime': '2024-12-31 23:59:59', 'LogLevel': 5,
                                      'Message': 'password=secret123 Bearer jwt.value user@example.com https://example.com/path?token=abc',
                                      'Exception': '😀堆栈' * 3000}] * 3
        self.scan(3)
        text = json.loads(next(r[2] for r in Mock.requests if r[0] == 'POST' and r[1] == '/api/v1/incidents'))['Content']
        self.assertLessEqual(len(text.encode()), 4000)
        for value in ('secret123', 'jwt.value', 'user@example.com', 'token=abc'):
            self.assertNotIn(value, text)
        self.assertIn('LIMIT 20;', text)
        self.assertTrue(text.endswith('...'))

    def test_sample_failure_does_not_block_alert_or_resolution(self):
        def unavailable(*_): raise RuntimeError('Optional log read failed')
        self.ch.samples = unavailable
        self.scan(3)
        text = json.loads(next(r[2] for r in Mock.requests if r[0] == 'POST' and r[1] == '/api/v1/incidents'))['Content']
        self.assertIn('Samples unavailable', text)
        self.scan(0)
        self.assertEqual(next(iter(Mock.incidents.values()))['Status'], 'resolved')

    def test_samples_use_server_side_limits_and_same_window(self):
        queries = []
        def query(sql, params=None):
            queries.append(sql)
            return [{'name': k} for k in ('Message', 'Exception', 'Category')] if 'system.columns' in sql else []
        self.ch.query = query
        self.ch.samples('strange`table', dt.datetime(2025, 1, 1, tzinfo=main.UTC))
        sql = queries[-1]
        self.assertIn('`strange\\`table`', sql)
        self.assertIn('substringUTF8(toString(`Message`), 1, 500)', sql)
        self.assertIn('LIMIT 3 SETTINGS max_execution_time=5', sql)
        self.assertIn("EventTime < toDateTime64('2025-01-01 00:00:00.000000', 6, 'UTC')", sql)

    def test_public_link_rejects_embedded_credentials(self):
        for url in ('https://user:password@example.com', 'https://example.com?token=secret', 'javascript:alert(1)'):
            with self.assertRaises(ValueError): main.Monitor(self.ch, self.api, self.store, 'node', 'AspNetCoreLogs', url)

    def test_invalid_diagnostic_counts_fail_closed(self):
        for row in ({'error': '2', 'critical': '1', 'total': '4'},
                    {'error': '-1', 'critical': '1', 'total': '0'},
                    {'error': True, 'critical': 1, 'total': 2}, {'total': 0}):
            original = self.ch.query
            self.ch.query = lambda sql, params, row=row: [row] if 'countIf' in sql else original(sql, params)
            try:
                with self.assertRaises(ValueError): self.ch.count('app', dt.datetime.now(main.UTC))
            finally:
                self.ch.query = original

    def test_lifecycle(self):
        for n, expected in [(3,'sev4'),(5,'sev3'),(15,'sev2'),(2,'sev2'),(1,'sev2'),(0,'sev2')]:
            self.scan(n)
            x = next(iter(Mock.incidents.values())); self.assertEqual(x['Level'], expected)
        old = next(iter(Mock.incidents.values())); self.assertEqual(old['Status'], 'resolved')
        self.scan(3); self.assertEqual(len(Mock.incidents), 2)
        self.assertEqual(old['Status'], 'resolved')
        self.assertFalse(any('/mitigate' in r[1] for r in Mock.requests))

    def test_lost_create_restart_and_zero(self):
        Mock.lost = True
        self.assertEqual(self.monitor.scan(), 1)
        payload = self.store.pending('app'); self.assertIsNotNone(payload)
        self.store.close(); self.store = main.Store(self.tmp.name, 'node', 'AspNetCoreLogs'); self.monitor.store = self.store
        self.scan(0)
        self.assertIsNone(self.store.pending('app'))
        self.assertEqual(len(Mock.incidents), 1)
        self.assertEqual(next(iter(Mock.incidents.values()))['Status'], 'resolved')
        creates = [json.loads(x[2]) for x in Mock.requests if x[0] == 'POST' and x[1] == '/api/v1/incidents']
        self.assertEqual(creates[0], creates[1])

    def test_query_and_special_identifier(self):
        Mock.tables = ['a`b\\c']; self.scan(3)
        sql = [r[2] for r in Mock.requests if r[0] == 'POST' and 'LogLevel IN (4, 5)' in r[2]][0]
        self.assertIn('`a\\`b\\\\c`', sql)
        self.assertIn('LogLevel IN (4, 5)', sql)
        self.assertIn('EventTime >=', sql); self.assertIn('EventTime < ', sql)

    def test_extra_metadata_and_mitigated(self):
        self.scan(3)
        x = next(iter(Mock.incidents.values()))
        x['Metadata']['note'] = 'human annotation'
        x['Status'] = 'mitigated'
        self.scan(5)
        self.assertEqual(len(Mock.incidents), 1)
        self.assertEqual(x['Level'], 'sev3')
        self.scan(0)
        self.assertEqual(x['Status'], 'resolved')

    def test_pending_resolved_receipt_creates_recurrence_immediately(self):
        Mock.lost = True
        self.assertEqual(self.monitor.scan(), 1)
        old = next(iter(Mock.incidents.values()))
        old['Status'] = 'resolved'
        self.store.close()
        self.store = main.Store(self.tmp.name, 'node', 'AspNetCoreLogs')
        self.monitor.store = self.store
        self.scan(15)
        self.assertEqual(len(Mock.incidents), 2)
        new = next(x for x in Mock.incidents.values() if x['IncidentId'] != old['IncidentId'])
        self.assertEqual((new['Status'], new['Level']), ('new', 'sev2'))
        self.assertNotEqual(old['ReportId'], new['ReportId'])
        self.assertEqual(old['Status'], 'resolved')
        self.assertIsNone(self.store.pending('app'))
        self.scan(15)
        self.assertEqual(len(Mock.incidents), 2)
        self.assertFalse(any('/reopen' in r[1] for r in Mock.requests))

    def test_pending_resolved_receipt_respects_creation_threshold(self):
        for count, level in ((0, None), (1, None), (2, None), (3, 'sev4'), (5, 'sev3')):
            with self.subTest(count=count):
                Mock.incidents = {}; Mock.requests = []; Mock.count = 3; Mock.lost = True
                self.assertEqual(self.monitor.scan(), 1)
                old = next(iter(Mock.incidents.values()))
                old['Status'] = 'resolved'
                self.scan(count)
                active = [x for x in Mock.incidents.values() if x['Status'] != 'resolved']
                self.assertEqual(len(active), 1 if level else 0)
                if level:
                    self.assertEqual(active[0]['Level'], level)
                    self.assertNotEqual(active[0]['ReportId'], old['ReportId'])
                self.assertEqual(old['Status'], 'resolved')
                self.assertIsNone(self.store.pending('app'))

    def test_pending_recovers_when_table_disappears(self):
        Mock.lost = True
        self.assertEqual(self.monitor.scan(), 1)
        Mock.tables = []
        self.assertEqual(self.monitor.scan(), 1)
        self.assertIsNone(self.store.pending('app'))
        self.assertEqual(next(iter(Mock.incidents.values()))['Status'], 'new')

    def test_missing_known_table_survives_restart_and_recovers(self):
        self.scan(3)
        incident = next(iter(Mock.incidents.values()))
        self.store.close()
        self.store = main.Store(self.tmp.name, 'node', 'AspNetCoreLogs')
        self.monitor.store = self.store
        Mock.tables = ['other']
        Mock.count = 0
        self.assertEqual(self.monitor.scan(), 1)
        self.assertEqual(incident['Status'], 'new')
        self.assertEqual(self.store.known_names(), {'app', 'other'})
        Mock.tables = ['app', 'other']
        self.scan(0)
        self.assertEqual(incident['Status'], 'resolved')

    def test_missing_table_does_not_refresh_health_marker(self):
        self.scan(3)
        self.store.close()
        marker = Path(self.tmp.name) / 'healthy'
        marker.touch()
        before = marker.stat().st_mtime_ns
        Mock.tables = []
        with patch.dict(main.os.environ, {'STATE_DIR': self.tmp.name, 'NODE_NAME': 'node',
                                         'ALERTCENTER_URL': self.url, 'ALERTCENTER_API_KEY': 'secret',
                                         'CLICKHOUSE_URL': self.url, 'CLICKHOUSE_DATABASE': 'AspNetCoreLogs'}), \
             patch.object(sys, 'argv', ['main.py', '--once']), patch.object(main.signal, 'signal'):
            self.assertEqual(main.main(), 1)
        self.assertEqual(marker.stat().st_mtime_ns, before)
        self.store = main.Store(self.tmp.name, 'node', 'AspNetCoreLogs')

    def test_missing_known_table_without_incident(self):
        self.scan(0)
        self.assertFalse(Mock.incidents)
        Mock.tables = []
        self.assertEqual(self.monitor.scan(), 1)

    def test_active_identities_restore_coverage_with_scope_isolation(self):
        self.scan(3)
        incident = next(iter(Mock.incidents.values()))
        with self.store.db:
            self.store.db.execute('DELETE FROM known_tables')
        for node, database in (('other-node', 'AspNetCoreLogs'), ('node', 'other-db')):
            item = dict(incident, IncidentId=str(uuid.uuid4()), Metadata=dict(incident['Metadata'], node=node, database=database, table='foreign'))
            Mock.incidents[item['IncidentId']] = item
        Mock.tables = []
        self.assertEqual(self.monitor.scan(), 1)
        self.assertEqual(self.store.known_names(), {'app'})
        self.assertEqual(incident['Status'], 'new')
        with self.store.db:
            self.store.db.execute('DELETE FROM known_tables')
        incident['Status'] = 'resolved'
        self.assertEqual(self.monitor.scan(), 0)
        self.assertFalse(self.store.known_names())

    def test_duplicate_fail_closed(self):
        self.scan(3)
        x = dict(next(iter(Mock.incidents.values()))); x['IncidentId'] = str(uuid.uuid4()); Mock.incidents[x['IncidentId']] = x
        Mock.count = 0
        self.assertEqual(self.monitor.scan(), 1)
        self.assertTrue(all(x['Status'] == 'new' for x in Mock.incidents.values()))

    def test_isolation_multiple_tables_and_nodes(self):
        Mock.tables = ['app', 'second']
        self.scan(3)
        self.assertEqual(len(Mock.incidents), 2)
        with tempfile.TemporaryDirectory() as other:
            store = main.Store(other, 'node2', 'AspNetCoreLogs')
            try:
                monitor = main.Monitor(self.ch, self.api, store, 'node2', 'AspNetCoreLogs')
                self.assertEqual(monitor.scan(), 0)
                self.assertEqual(len(Mock.incidents), 4)
            finally:
                store.close()

    def test_table_failure_does_not_block_other(self):
        Mock.tables = ['bad', 'good']
        original = self.ch.count
        def count(name, now):
            if name == 'bad': raise ValueError('unreadable')
            return original(name, now)
        self.ch.count = count
        self.assertEqual(self.monitor.scan(), 1)
        self.assertEqual(next(iter(Mock.incidents.values()))['Metadata']['table'], 'good')

    def test_manual_close_then_new_incident(self):
        self.scan(3)
        first = next(iter(Mock.incidents.values()))
        first['Status'] = 'resolved'
        self.scan(3)
        self.assertEqual(len(Mock.incidents), 2)
        self.assertEqual(first['Status'], 'resolved')

    def test_protocol_errors_are_not_success(self):
        original = main.request
        try:
            for response in (b'garbage', b'{}', b'{"Code": 999}', b'{"Code": true}'):
                main.request = lambda *args, response=response, **kwargs: response
                with self.assertRaises(RuntimeError): self.api.call()
        finally:
            main.request = original

    def test_discovery_failure_does_not_close(self):
        self.scan(3)
        self.ch.tables = lambda: (_ for _ in ()).throw(RuntimeError('unavailable'))
        with self.assertRaises(RuntimeError): self.monitor.scan()
        self.assertEqual(next(iter(Mock.incidents.values()))['Status'], 'new')

    def test_persistence_failure_prevents_post(self):
        def failure(*args): raise OSError('disk full')
        self.store.save = failure
        self.assertEqual(self.monitor.scan(), 1)
        self.assertFalse(Mock.incidents)

    def test_schema_rejected_without_count(self):
        self.ch.query = lambda sql, params: [{'name': 'EventTime', 'type': 'DateTime'}, {'name': 'LogLevel', 'type': 'Float64'}]
        with self.assertRaises(ValueError): self.ch.count('bad', dt.datetime.now(main.UTC))

    def test_state_corruption_stops(self):
        self.store.close()
        with open(self.tmp.name + '/state.sqlite3', 'wb') as f: f.write(b'not sqlite')
        with self.assertRaises(Exception): main.Store(self.tmp.name, 'node', 'AspNetCoreLogs')
        self.store = type('Closed', (), {'close': lambda self: None})()

    def test_conflict_rechecks_and_lost_mutation_reconciles(self):
        self.scan(3)
        original = self.api.change
        calls = []
        def conflict_once(incident_id, action, payload):
            calls.append(action)
            if len(calls) == 1: raise main.Conflict()
            original(incident_id, action, payload)
        self.api.change = conflict_once
        self.scan(5)
        self.assertEqual(calls, ['severity', 'severity'])
        def lost(incident_id, action, payload):
            original(incident_id, action, payload)
            raise RuntimeError('lost')
        self.api.change = lost
        self.scan(0)
        self.assertEqual(next(iter(Mock.incidents.values()))['Status'], 'resolved')

    def test_pagination(self):
        for _ in range(101):
            incident_id = str(uuid.uuid4())
            Mock.incidents[incident_id] = {'IncidentId': incident_id, 'Metadata': {'source': main.SOURCE, 'node': 'another', 'database': 'db', 'table': incident_id}, 'Status': 'new'}
        self.assertEqual(len(self.api.active()), 101)
        self.assertEqual(len([r for r in Mock.requests if r[0] == 'GET']), 2)

    def test_lock_and_scope(self):
        with self.assertRaises(BlockingIOError): main.Store(self.tmp.name, 'node', 'AspNetCoreLogs')
        self.store.close()
        with self.assertRaises(ValueError): main.Store(self.tmp.name, 'other', 'AspNetCoreLogs')
        self.store = main.Store(self.tmp.name, 'node', 'AspNetCoreLogs')


if __name__ == '__main__': unittest.main()
