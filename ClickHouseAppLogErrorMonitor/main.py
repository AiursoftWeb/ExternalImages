import argparse
import datetime as dt
import fcntl
import json
import logging
import os
import signal
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

SOURCE = 'clickhouse-app-log-error-monitor'
UTC = dt.timezone.utc


def severity(count):
    return 'sev2' if count >= 15 else 'sev3' if count >= 5 else 'sev4' if count >= 3 else None


def ident(value):
    return '`' + value.replace('\\', '\\\\').replace('`', '\\`') + '`'


def request(url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            if not 200 <= response.status < 300:
                raise RuntimeError('HTTP failure')
            return response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            raise Conflict() from None
        raise RuntimeError('HTTP request failed') from None
    except (OSError, ValueError):
        raise RuntimeError('HTTP request failed') from None


class Conflict(Exception):
    pass


class ClickHouse:
    def __init__(self, url, database, user, password):
        self.url, self.database = url.rstrip('/'), database
        self.headers = {'X-ClickHouse-User': user, 'X-ClickHouse-Key': password}

    def query(self, sql, params=None):
        query = urllib.parse.urlencode({'max_execution_time': '20', **{'param_' + k: v for k, v in (params or {}).items()}})
        raw = request(self.url + '/?' + query, sql.encode(), self.headers)
        try:
            result = json.loads(raw)
            if not isinstance(result, dict) or not isinstance(result.get('data'), list):
                raise ValueError()
            return result['data']
        except (ValueError, UnicodeError):
            raise RuntimeError('Invalid ClickHouse response') from None

    def tables(self):
        exists = self.query("SELECT count() AS n FROM system.databases WHERE name = {db:String} FORMAT JSON", {'db': self.database})
        if len(exists) != 1 or str(exists[0].get('n')) != '1':
            raise ValueError('Database not found or inaccessible')
        rows = self.query("SELECT name FROM system.tables WHERE database = {db:String} FORMAT JSON", {'db': self.database})
        names = [r['name'] for r in rows]
        if any(not isinstance(x, str) or not x for x in names) or len(set(names)) != len(names):
            raise ValueError('Invalid table discovery')
        return names

    def count(self, table, end):
        if end.tzinfo is None or end.utcoffset() is None:
            raise ValueError('Window end must be timezone-aware')
        end = end.astimezone(UTC)
        params = {'db': self.database, 'table': table}
        rows = self.query("SELECT name, type FROM system.columns WHERE database = {db:String} AND table = {table:String} AND name IN ('EventTime', 'LogLevel') FORMAT JSON", params)
        cols = {r['name']: r['type'] for r in rows}
        if len(rows) != 2 or cols.get('LogLevel') != 'Int32' or not (cols.get('EventTime') == 'DateTime' or cols.get('EventTime', '').startswith('DateTime64(') or cols.get('EventTime', '').startswith('DateTime(')):
            raise ValueError('Unsupported logger schema')
        params.update(start=(end - dt.timedelta(minutes=15)).strftime('%Y-%m-%d %H:%M:%S.%f'), end=end.strftime('%Y-%m-%d %H:%M:%S.%f'))
        sql = (f'SELECT countIf(LogLevel = 4) AS error, countIf(LogLevel = 5) AS critical, count() AS total FROM {ident(self.database)}.{ident(table)} '
               "WHERE EventTime >= parseDateTime64BestEffort({start:String}, 6, 'UTC') "
               "AND EventTime < parseDateTime64BestEffort({end:String}, 6, 'UTC') "
               'AND LogLevel IN (4, 5) FORMAT JSON')
        result = self.query(sql, params)
        if len(result) != 1 or any(type(result[0].get(k)) not in (str, int) or not str(result[0][k]).isdigit() for k in ('error', 'critical', 'total')):
            raise ValueError('Invalid count')
        counts = {k: int(result[0][k]) for k in ('error', 'critical', 'total')}
        if counts['error'] + counts['critical'] != counts['total']:
            raise ValueError('Inconsistent counts')
        return counts


class AlertCenter:
    def __init__(self, url, key):
        self.url = url.rstrip('/') + '/api/v1/incidents'
        self.headers = {'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'}

    def call(self, path='', payload=None, query=None):
        url = self.url + path + ('?' + urllib.parse.urlencode(query) if query else '')
        data = None if payload is None else json.dumps(payload, separators=(',', ':')).encode()
        try:
            result = json.loads(request(url, data, self.headers))
            if not isinstance(result, dict) or type(result.get('Code')) is not int or result['Code'] not in (1, 2):
                raise ValueError()
            return result
        except (ValueError, UnicodeError):
            raise RuntimeError('Invalid AlertCenter response') from None

    def active(self):
        items = []
        for page in range(1, 1000001):
            r = self.call(query={'ActiveOnly': 'true', 'MetadataKey': 'source', 'MetadataValue': SOURCE, 'Page': page, 'PageSize': 100})
            rows, total = r.get('Items'), r.get('TotalCount')
            if not isinstance(rows, list) or type(total) is not int or total < 0 or r.get('Page') != page or r.get('PageSize') != 100 or len(rows) > 100:
                raise ValueError('Invalid incident pagination')
            for item in rows:
                metadata = item.get('Metadata') if isinstance(item, dict) else None
                if not isinstance(metadata, dict) or any(not isinstance(metadata.get(k), str) or not metadata[k] for k in ('source', 'node', 'database', 'table')) or metadata['source'] != SOURCE:
                    raise ValueError('Invalid incident identity in query')
            items.extend(rows)
            if len(items) >= total:
                if len(items) != total:
                    raise ValueError('Inconsistent incident pagination')
                return items
            if not rows:
                raise ValueError('Incomplete incident pagination')
        raise ValueError('Incident pagination limit')

    def get(self, incident_id):
        return self.call('/' + incident_id)['Incident']

    def create(self, payload):
        r = self.call(payload=payload)
        if r.get('ReportId', '').lower() != payload['ReportId'].lower():
            raise ValueError('Invalid receipt')
        return str(uuid.UUID(r['IncidentId']))

    def change(self, incident_id, action, payload):
        self.call('/' + incident_id + '/' + action, payload)


class Store:
    def __init__(self, directory, node, database):
        os.makedirs(directory, exist_ok=True)
        self.lock = open(os.path.join(directory, 'monitor.lock'), 'a')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            self.lock.close()
            raise
        self.db = sqlite3.connect(os.path.join(directory, 'state.sqlite3'))
        try:
            if self.db.execute('PRAGMA quick_check').fetchone() != ('ok',):
                raise ValueError('State integrity check failed')
            self.db.execute('CREATE TABLE IF NOT EXISTS config (scope TEXT NOT NULL)')
        except Exception:
            self.db.close()
            self.lock.close()
            raise
        scope = json.dumps([node, database], ensure_ascii=True)
        row = self.db.execute('SELECT scope FROM config').fetchone()
        if row and row[0] != scope:
            self.db.close()
            self.lock.close()
            raise ValueError('State directory scope changed')
        if not row:
            self.db.execute('INSERT INTO config VALUES (?)', (scope,))
        self.db.execute('CREATE TABLE IF NOT EXISTS pending (name TEXT PRIMARY KEY, payload TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS known_tables (name TEXT PRIMARY KEY)')
        self.db.commit()

    def known_names(self):
        return {row[0] for row in self.db.execute('SELECT name FROM known_tables')}

    def remember(self, names):
        with self.db:
            self.db.executemany('INSERT OR IGNORE INTO known_tables VALUES (?)', ((name,) for name in names))

    def pending_names(self):
        return [row[0] for row in self.db.execute('SELECT name FROM pending')]

    def pending(self, name):
        row = self.db.execute('SELECT payload FROM pending WHERE name=?', (name,)).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, name, payload):
        with self.db:
            self.db.execute('INSERT INTO pending VALUES (?,?)', (name, json.dumps(payload, ensure_ascii=True, separators=(',', ':'))))

    def delete(self, name):
        with self.db:
            self.db.execute('DELETE FROM pending WHERE name=?', (name,))

    def close(self):
        self.db.close()
        self.lock.close()


class Monitor:
    def __init__(self, ch, api, store, node, database):
        self.ch, self.api, self.store, self.node, self.database = ch, api, store, node, database

    def identity(self, name):
        return {'source': SOURCE, 'node': self.node, 'database': self.database, 'table': name}

    def matches(self, incident, name):
        metadata = incident.get('Metadata') if isinstance(incident, dict) else None
        return isinstance(metadata, dict) and all(metadata.get(k) == v for k, v in self.identity(name).items())

    def validate(self, incident, name):
        if not isinstance(incident, dict) or not self.matches(incident, name):
            raise ValueError('Incident identity mismatch')
        for key in ('IncidentId', 'Version', 'ReportId'):
            if uuid.UUID(incident[key]).int == 0:
                raise ValueError('Invalid incident identifier')
        if incident.get('Status') not in ('new', 'mitigated', 'resolved') or incident.get('Level') not in ('sev0', 'sev1', 'sev2', 'sev3', 'sev4'):
            raise ValueError('Invalid incident state')
        return incident

    def diagnostics(self, counts, now):
        end = now.astimezone(UTC)
        level = severity(counts['total'])
        threshold = {'sev4': 3, 'sev3': 5, 'sev2': 15}.get(level)
        context = {'WindowStartUtc': (end - dt.timedelta(minutes=15)).isoformat(),
                   'WindowEndUtc': end.isoformat(), 'WindowBounds': '[start, end)',
                   'ErrorCount': counts['error'], 'CriticalCount': counts['critical'],
                   'TotalCount': counts['total'], 'TriggerThreshold': threshold,
                   'ThresholdLevel': level, 'Thresholds': {'sev4': 3, 'sev3': 5, 'sev2': 15}}
        text = (f"UTC window: [{context['WindowStartUtc']}, {context['WindowEndUtc']})\n"
                f"Error: {counts['error']}; Critical: {counts['critical']}; Total: {counts['total']}\n"
                f"Trigger threshold: {threshold} ({level}); rules: sev4 >=3, sev3 >=5, sev2 >=15; resolve at 0.")
        return context, text

    def process(self, name, counts, active, now):
        matches = [x for x in active if self.matches(x, name)]
        if len(matches) > 1:
            raise ValueError('Duplicate active incident identity')
        pending = self.store.pending(name)
        if pending:
            if matches and matches[0].get('ReportId', '').lower() != pending['ReportId'].lower():
                raise ValueError('Pending report conflicts with active incident')
            incident_id = self.api.create(pending)
            current = self.validate(self.api.get(incident_id), name)
            if current['IncidentId'].lower() != incident_id.lower() or current['ReportId'].lower() != pending['ReportId'].lower():
                raise ValueError('Receipt identity mismatch')
            self.store.delete(name)
        elif matches:
            current = self.validate(self.api.get(matches[0]['IncidentId']), name)
            if current['IncidentId'].lower() != matches[0]['IncidentId'].lower() or current['ReportId'].lower() != matches[0]['ReportId'].lower():
                raise ValueError('Query/detail identity mismatch')
        else:
            current = None
        if counts is None:
            return
        count = counts['total']
        context, diagnostic_text = self.diagnostics(counts, now)
        if current and current['Status'] == 'resolved':
            current = None
        if current is None and severity(count):
            payload = {'ReportId': str(uuid.uuid4()), 'Title': ('ClickHouse application errors: ' + name)[:200],
                       'Content': diagnostic_text, 'Level': severity(count), 'Context': context,
                       'OccurredAtUtc': now.astimezone(UTC).isoformat(), 'Metadata': self.identity(name)}
            self.store.save(name, payload)
            incident_id = self.api.create(payload)
            current = self.validate(self.api.get(incident_id), name)
            if current['IncidentId'].lower() != incident_id.lower() or current['ReportId'].lower() != payload['ReportId'].lower():
                raise ValueError('Receipt identity mismatch')
            self.store.delete(name)
        if not current:
            return
        incident_id, report_id = current['IncidentId'], current['ReportId']
        for attempt in range(3):
            if current['IncidentId'].lower() != incident_id.lower() or current['ReportId'].lower() != report_id.lower():
                raise ValueError('Incident changed identity')
            if current['Status'] == 'resolved':
                return
            wanted = severity(count)
            action = 'resolve' if count == 0 else 'severity' if wanted and int(wanted[-1]) < int(current['Level'][-1]) else None
            if not action:
                return
            body = {'Version': current['Version'], 'Reason': diagnostic_text}
            if action == 'severity':
                body['Level'] = wanted
            try:
                self.api.change(current['IncidentId'], action, body)
                return
            except (Conflict, RuntimeError):
                # A conflict may be represented as a protocol error rather than HTTP 409.
                # A lost response is ambiguous too: always re-read before retrying.
                current = self.validate(self.api.get(current['IncidentId']), name)
        raise RuntimeError('Repeated incident update failures')

    def scan(self, now=None):
        start = time.monotonic()
        now = now or dt.datetime.now(UTC)
        tables = self.ch.tables()
        self.store.remember(tables)
        active = self.api.active()
        active_names = {item['Metadata']['table'] for item in active
                        if item['Metadata']['node'] == self.node and item['Metadata']['database'] == self.database}
        self.store.remember(active_names | set(self.store.pending_names()))
        missing = self.store.known_names() - set(tables)
        for name in sorted(missing):
            logging.error('Known table %r missing from discovery; monitoring coverage lost', name)
        failed = 0
        pending_failed = 0
        for name in tables:
            try:
                try:
                    count = self.ch.count(name, now)
                except Exception:
                    # Confirm pending creates even when the table cannot currently be read.
                    if self.store.pending(name):
                        self.process(name, None, active, now)
                    raise
                self.process(name, count, active, now)
            except Exception as exc:
                failed += 1
                logging.error('Table %r failed (%s)', name, type(exc).__name__)
        for name in missing:
            try:
                self.process(name, None, active, now)
            except Exception as exc:
                pending_failed += 1
                logging.error('Pending incident failed (%s)', type(exc).__name__)
        elapsed = time.monotonic() - start
        logging.info('Scan tables=%d succeeded=%d failed_tables=%d missing_tables=%d pending_failed=%d seconds=%.3f', len(tables), len(tables)-failed, failed, len(missing), pending_failed, elapsed)
        if elapsed >= 300:
            logging.warning('Scan exceeded 300 seconds')
        return failed + len(missing) + pending_failed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--healthcheck', action='store_true')
    args = parser.parse_args()
    if args.healthcheck:
        try:
            return 0 if time.time() - os.stat(os.path.join(os.environ.get('STATE_DIR', '/data'), 'healthy')).st_mtime < 900 else 1
        except OSError:
            return 1
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    node = os.environ['NODE_NAME']
    database = os.environ.get('CLICKHOUSE_DATABASE', 'AspNetCoreLogs')
    if not node or not database or not os.environ['ALERTCENTER_API_KEY']:
        raise ValueError('Missing required configuration')
    store = Store(os.environ.get('STATE_DIR', '/data'), node, database)
    ch = ClickHouse(os.environ.get('CLICKHOUSE_URL', 'http://clickhouse_clickhouse:8123'), database, os.environ.get('CLICKHOUSE_USER', 'default'), os.environ.get('CLICKHOUSE_PASSWORD', ''))
    api = AlertCenter(os.environ['ALERTCENTER_URL'], os.environ['ALERTCENTER_API_KEY'])
    monitor = Monitor(ch, api, store, node, database)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    try:
        while not stop.is_set():
            started = time.monotonic()
            try:
                failed = monitor.scan()
                if not failed:
                    with open(os.path.join(os.environ.get('STATE_DIR', '/data'), 'healthy'), 'w'):
                        pass
            except Exception as exc:
                logging.error('Scan failed (%s)', type(exc).__name__)
                failed = 1
            if args.once:
                return 1 if failed else 0
            stop.wait(max(0, 300 - (time.monotonic() - started)))
    finally:
        store.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
