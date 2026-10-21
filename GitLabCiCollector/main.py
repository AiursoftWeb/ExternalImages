#!/usr/bin/env python3
"""Copy GitLab job lifecycle metadata from read-only PostgreSQL to ClickHouse."""
import argparse
import datetime as dt
import json
import logging
import os
from pathlib import Path
import re
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

UTC = dt.timezone.utc
STOP = threading.Event()
LOG = logging.getLogger('gitlab-ci-collector')


class CollectorError(RuntimeError):
    """A diagnostic without credentials or upstream response bodies."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CollectorError('HTTP redirects are not allowed')


def utc(value):
    parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def stamp(value):
    return value.astimezone(UTC).strftime('%Y-%m-%d %H:%M:%S.%f')


def setting(name, default, maximum):
    value = int(os.environ.get(name, default))
    if not 1 <= value <= maximum:
        raise ValueError(f'{name} is outside its allowed range')
    return value


class Collector:
    def __init__(self):
        self.source = os.environ.get('GITLAB_SOURCE', 'gitlab')
        self.database = os.environ.get('CLICKHOUSE_DATABASE', 'GitLabMetrics')
        if any(not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', x) for x in (self.source, self.database)):
            raise ValueError('Invalid source or database identifier')
        self.url = os.environ.get('CLICKHOUSE_URL', 'http://clickhouse:8123').rstrip('/')
        parts = urllib.parse.urlsplit(self.url)
        if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError('CLICKHOUSE_URL must be HTTP(S) without credentials or query')
        self.headers = {'X-ClickHouse-User': os.environ.get('CLICKHOUSE_USER', 'default'),
                        'X-ClickHouse-Key': os.environ.get('CLICKHOUSE_PASSWORD', '')}
        self.interval = setting('SYNC_INTERVAL_SECONDS', '60', 3600)
        self.days = setting('BACKFILL_DAYS', '30', 90)
        self.overlap = setting('RECHECK_SECONDS', '300', 86400)
        self.batch = setting('BATCH_SIZE', '2000', 10000)
        self.opener = urllib.request.build_opener(NoRedirect())

    def sql(self, query, rows=None):
        options = {'wait_end_of_query': '1', 'max_execution_time': '30', 'max_threads': '2'}
        if rows is None:
            body = query.encode()
        else:
            options['query'] = query
            body = ''.join(json.dumps(x, allow_nan=False) + '\n' for x in rows).encode()
        request = urllib.request.Request(self.url + '/?' + urllib.parse.urlencode(options), data=body,
                                         headers=self.headers, method='POST')
        try:
            with self.opener.open(request, timeout=45) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            raise CollectorError(f'ClickHouse HTTP status {error.code}') from None
        except (urllib.error.URLError, OSError, TimeoutError):
            raise CollectorError('ClickHouse request failed or timed out') from None

    def initialize(self):
        for query in Path(__file__).with_name('schema.sql').read_text().format(database=self.database).split(';'):
            if query.strip():
                self.sql(query)

    def progress(self):
        raw = self.sql(f"SELECT maxOrNull(through) AS through FROM {self.database}.sync_windows "
                       f"WHERE source='{self.source}' FORMAT JSONEachRow")
        value = json.loads(raw)['through']
        return utc(value) if value else None

    def postgres(self, query):
        env = dict(os.environ, PGOPTIONS='-c default_transaction_read_only=on -c statement_timeout=15000 '
                   '-c lock_timeout=1000 -c timezone=UTC', PGCONNECT_TIMEOUT='10')
        try:
            result = subprocess.run(['psql', '-X', '-q', '-t', '-A', '-v', 'ON_ERROR_STOP=1'], input=query,
                                    text=True, capture_output=True, timeout=30, env=env, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise CollectorError('PostgreSQL request failed or timed out') from None
        if result.returncode:
            raise CollectorError('PostgreSQL query failed')
        try:
            return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        except (ValueError, TypeError):
            raise CollectorError('Invalid PostgreSQL result') from None

    def fetch(self, start, end, after=None, active=False):
        # All interpolated values are validated identifiers, integers, or formatted UTC timestamps.
        where = "b.type='Ci::Build' AND b.queued_at IS NOT NULL"
        if active:
            where += " AND b.status IN ('pending','running','preparing','canceling') AND b.finished_at IS NULL"
            if after:
                where += f' AND (b.id,b.partition_id)>({after[1]},{after[2]})'
            order = 'b.id,b.partition_id'
        else:
            where += f" AND b.updated_at>='{stamp(start)}' AND b.updated_at<'{stamp(end)}'"
            if after:
                where += (f" AND (b.updated_at,b.id,b.partition_id)>"
                          f"('{stamp(utc(after[0]))}',{after[1]},{after[2]})")
            order = 'b.updated_at,b.id,b.partition_id'
        query = f"""SELECT row_to_json(j) FROM (
SELECT b.id AS job_id,b.partition_id,b.project_id,b.commit_id AS pipeline_id,
b.name,b.status,b.runner_id,b.queued_at,b.started_at,b.finished_at,b.updated_at AS source_updated_at
FROM p_ci_builds b WHERE {where} ORDER BY {order} LIMIT {self.batch}) j;"""
        return self.postgres(query)

    def write(self, rows):
        if not rows:
            return
        for row in rows:
            q, s, f = [utc(row[k]) if row[k] else None for k in ('queued_at', 'started_at', 'finished_at')]
            if q is None or (s and s < q) or (f and f < (s or q)):
                raise CollectorError('Invalid source lifecycle timestamps')
            if f is None and row['status'] in ('success', 'failed', 'canceled', 'skipped'):
                raise CollectorError('Queued terminal job has no finish time')
            row['source'] = self.source
            for key in ('queued_at', 'started_at', 'finished_at', 'source_updated_at'):
                row[key] = stamp(utc(row[key])) if row[key] else None
        self.sql(f'INSERT INTO {self.database}.jobs FORMAT JSONEachRow', rows)

    def copy_window(self, start, end, active=False):
        after = None
        count = 0
        while not STOP.is_set():
            rows = self.fetch(start, end, after, active)
            if not rows:
                return count
            last = rows[-1]
            after = (last['source_updated_at'], int(last['job_id']), int(last['partition_id']))
            self.write(rows)
            count += len(rows)
            if len(rows) < self.batch:
                return count
        raise CollectorError('Collection interrupted; progress not advanced')

    def sync(self, now=None):
        # A short lag avoids racing ordinary source transactions; the overlap rechecks late commits.
        end = (now or dt.datetime.now(UTC)) - dt.timedelta(seconds=10)
        cursor = self.progress()
        earliest = end - dt.timedelta(days=self.days)
        if cursor and cursor > end:
            raise CollectorError('Stored progress is ahead of the collection boundary')
        if cursor and cursor < earliest:
            LOG.warning('Source history gap exceeds configured backfill window')
        start = max(earliest, cursor - dt.timedelta(seconds=self.overlap)) if cursor else earliest
        # Refresh old active jobs too: their original queue/update time may precede the backfill window.
        active_count = self.copy_window(start, end, active=True)
        while start < end and not STOP.is_set():
            stop = min(start + dt.timedelta(hours=1), end)
            count = self.copy_window(start, stop)
            if STOP.is_set():
                raise CollectorError('Collection interrupted; progress not advanced')
            self.sql(f'INSERT INTO {self.database}.sync_windows FORMAT JSONEachRow',
                     [{'source': self.source, 'through': stamp(stop), 'collected_at': stamp(dt.datetime.now(UTC)),
                       'rows': count}])
            LOG.info('Copied %s jobs through %s; active refresh %s', count, stamp(stop), active_count)
            start = stop
            if start < end and STOP.wait(0.1):
                raise CollectorError('Collection interrupted')

    def health(self):
        raw = self.sql(f"SELECT maxOrNull(collected_at) AS collected_at, maxOrNull(through) AS through FROM {self.database}.sync_windows "
                       f"WHERE source='{self.source}' FORMAT JSONEachRow")
        record = json.loads(raw)
        limit = self.interval * 3 + 300
        return all(record[key] and 0 <= (dt.datetime.now(UTC) - utc(record[key])).total_seconds() <= limit
                   for key in ('collected_at', 'through'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--init-schema', action='store_true')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--healthcheck', action='store_true')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    collector = Collector()
    if args.init_schema:
        collector.initialize()
        return 0
    if args.healthcheck:
        return 0 if collector.health() else 1
    while not STOP.is_set():
        try:
            collector.sync()
        except (CollectorError, ValueError, KeyError, TypeError):
            LOG.error('Collection failed; unconfirmed progress is retained')
            if args.once:
                return 1
        if args.once or STOP.wait(collector.interval):
            break
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (CollectorError, ValueError, KeyError, TypeError):
        LOG.error('Collector configuration or request failed')
        raise SystemExit(1)
