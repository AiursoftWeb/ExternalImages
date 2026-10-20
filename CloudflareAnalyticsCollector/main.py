#!/usr/bin/env python3
"""Copy Cloudflare hourly response estimates to ClickHouse using standard HTTP APIs."""
import argparse
import datetime as dt
import json
import logging
import math
import os
from pathlib import Path
import re
import signal
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

UTC = dt.timezone.utc
STOP = threading.Event()
LOG = logging.getLogger('cloudflare-analytics')


def utc(value):
    return dt.datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(UTC)


def stamp(value):
    return value.astimezone(UTC).strftime('%Y-%m-%d %H:%M:%S')


def positive_env(name, default, minimum=1, maximum=86400):
    value = int(os.environ.get(name, default))
    if not minimum <= value <= maximum:
        raise ValueError(f'{name} is outside its allowed range')
    return value


class CollectorError(RuntimeError):
    """A safe diagnostic that never contains an upstream response body."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CollectorError('HTTP redirects are not allowed')


class Collector:
    def __init__(self):
        self.zone = os.environ['CLOUDFLARE_ZONE_ID']
        self.hostname = os.environ['CLOUDFLARE_HOSTNAME']
        if not re.fullmatch(r'[a-fA-F0-9]{32}', self.zone):
            raise ValueError('CLOUDFLARE_ZONE_ID must be a zone ID')
        if not re.fullmatch(r'[a-zA-Z0-9.-]+', self.hostname):
            raise ValueError('CLOUDFLARE_HOSTNAME must be a hostname')
        self.token = os.environ['CLOUDFLARE_API_TOKEN']
        if not self.token.strip():
            raise ValueError('CLOUDFLARE_API_TOKEN must not be empty')
        self.database = os.environ.get('CLICKHOUSE_DATABASE', 'cloudflare')
        if not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_]*', self.database):
            raise ValueError('Invalid CLICKHOUSE_DATABASE')
        self.ch_url = os.environ.get('CLICKHOUSE_URL', 'http://clickhouse:8123').rstrip('/')
        parsed = urllib.parse.urlsplit(self.ch_url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('CLICKHOUSE_URL must be an HTTP(S) URL without credentials or query')
        self.ch_headers = {
            'X-ClickHouse-User': os.environ.get('CLICKHOUSE_USER', 'default'),
            'X-ClickHouse-Key': os.environ.get('CLICKHOUSE_PASSWORD', ''),
        }
        self.interval = positive_env('SYNC_INTERVAL_SECONDS', '900', maximum=3600)
        self.history = positive_env('BACKFILL_DAYS', '30', maximum=30)
        self.recheck = positive_env('RECHECK_HOURS', '48', maximum=self.history * 24)
        self.lag = positive_env('INGESTION_LAG_HOURS', '1', minimum=0, maximum=24)
        self.opener = urllib.request.build_opener(NoRedirect())

    def http(self, url, body, headers):
        request = urllib.request.Request(url, data=body, headers=headers, method='POST')
        try:
            with self.opener.open(request, timeout=60) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            # API responses and exception URLs may include secrets; never log them.
            raise CollectorError(f'HTTP request failed with status {error.code}') from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise CollectorError('HTTP request failed or timed out') from None

    def sql(self, query, rows=None, params=None):
        options = {'wait_end_of_query': '1', 'max_execution_time': '30', 'max_threads': '2', **(params or {})}
        if rows is None:
            body = query.encode()
        else:
            options['query'] = query
            body = ''.join(json.dumps(row, allow_nan=False) + '\n' for row in rows).encode()
        return self.http(self.ch_url + '/?' + urllib.parse.urlencode(options), body, self.ch_headers)

    def initialize(self):
        for statement in Path(__file__).with_name('schema.sql').read_text().format(database=self.database).split(';'):
            if statement.strip():
                self.sql(statement)

    def parameters(self):
        return {'param_zone': self.zone, 'param_host': self.hostname}

    def cursor(self):
        result = json.loads(self.sql(
            f'SELECT toString(max(window_end)) AS through FROM {self.database}.sync_windows '
            'WHERE zone_id={zone:String} AND hostname={host:String} FORMAT JSONEachRow',
            params=self.parameters()))
        return utc(result['through'] + '+00:00')

    def fetch(self, start, end):
        filters = {
            'datetime_geq': start.strftime('%Y-%m-%dT%H:%M:%SZ'),
            'datetime_lt': end.strftime('%Y-%m-%dT%H:%M:%SZ'),
            'clientRequestHTTPHost': self.hostname,
            'requestSource': 'eyeball',
        }
        where = ','.join(key + ':' + json.dumps(value) for key, value in filters.items())
        query = ('query {viewer {zones(filter:{zoneTag:' + json.dumps(self.zone) + '}) {'
                 'httpRequestsAdaptiveGroups(limit:25,filter:{' + where + '},orderBy:[datetimeHour_ASC]) '
                 '{count sum {edgeResponseBytes} avg {sampleInterval} dimensions {datetimeHour}}}}}')
        payload = self.http('https://api.cloudflare.com/client/v4/graphql', json.dumps({'query': query}).encode(),
                            {'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'})
        data = json.loads(payload)
        if data.get('errors'):
            raise CollectorError('Cloudflare GraphQL returned errors')
        zones = data.get('data', {}).get('viewer', {}).get('zones', [])
        if len(zones) != 1:
            raise CollectorError('Cloudflare did not return the requested zone')
        groups = zones[0]['httpRequestsAdaptiveGroups']
        if not isinstance(groups, list) or len(groups) >= 25:
            raise CollectorError('Cloudflare result may be truncated')
        rows = []
        seen = set()
        for group in groups:
            hour = utc(group['dimensions']['datetimeHour'])
            response_bytes = group['sum']['edgeResponseBytes']
            requests = group['count']
            sample = group['avg']['sampleInterval']
            if hour < start or hour >= end or hour.minute or hour.second or hour.microsecond or hour in seen:
                raise CollectorError('Cloudflare returned invalid or duplicate hourly buckets')
            if any(type(n) is not int or not 0 <= n < 2**64 for n in (response_bytes, requests)):
                raise CollectorError('Cloudflare returned invalid counters')
            if not isinstance(sample, (float, int)) or not math.isfinite(sample) or sample < 1:
                raise CollectorError('Cloudflare returned invalid sampling metadata')
            seen.add(hour)
            rows.append({'zone_id': self.zone, 'hostname': self.hostname, 'hour': stamp(hour),
                         'response_bytes': response_bytes, 'requests': requests, 'sample_interval': sample})
        return rows

    def sync(self, now=None):
        now = now or dt.datetime.now(UTC)
        end = now.replace(minute=0, second=0, microsecond=0) - dt.timedelta(hours=self.lag)
        earliest = end - dt.timedelta(days=self.history)
        cursor = self.cursor()
        if cursor > end:
            raise CollectorError('Stored progress is ahead of the current collection boundary')
        if dt.datetime(1970, 1, 2, tzinfo=UTC) < cursor < earliest:
            LOG.warning('Unrecoverable history gap: %s to %s; resuming available history', stamp(cursor), stamp(earliest))
        start = max(earliest, min(cursor, end - dt.timedelta(hours=self.recheck)))
        while start < end and not STOP.is_set():
            stop = min(start + dt.timedelta(days=1), end)
            rows = self.fetch(start, stop)
            observed_count = len(rows)
            observed_hours = {row['hour'] for row in rows}
            for row in rows:
                row['is_present'] = 1
            # A refresh is a complete window snapshot. Tombstones remove stale
            # observations without presenting a missing bucket as zero traffic.
            for offset in range(int((stop - start).total_seconds() // 3600)):
                hour = stamp(start + dt.timedelta(hours=offset))
                if hour not in observed_hours:
                    rows.append({'zone_id': self.zone, 'hostname': self.hostname,
                                 'hour': hour, 'response_bytes': 0, 'requests': 0,
                                 'sample_interval': 1.0, 'is_present': 0})
            version = time.time_ns()
            collected = dt.datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S.%f')
            for row in rows:
                row.update(version=version, collected_at=collected)
            if rows:
                self.sql(f'INSERT INTO {self.database}.http_hourly FORMAT JSONEachRow', rows)
            # Progress is committed only after every metric row was accepted.
            self.sql(f'INSERT INTO {self.database}.sync_windows FORMAT JSONEachRow', [{
                'zone_id': self.zone, 'hostname': self.hostname, 'window_start': stamp(start),
                'window_end': stamp(stop), 'collected_at': collected, 'version': version,
            }])
            LOG.info('Synced %s to %s: %d hourly buckets', stamp(start), stamp(stop), observed_count)
            start = stop
        if start == end:
            health = Path('/tmp/cloudflare-analytics-health')
            health.write_text(str(time.time()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true', help='Run one synchronization cycle')
    parser.add_argument('--init-schema', action='store_true', help='Create schema, then exit')
    parser.add_argument('--healthcheck', action='store_true', help='Check last successful full cycle')
    args = parser.parse_args()
    if args.healthcheck:
        try:
            age = time.time() - float(Path('/tmp/cloudflare-analytics-health').read_text())
            return 0 if 0 <= age < int(os.environ.get('SYNC_INTERVAL_SECONDS', '900')) * 3 + 300 else 1
        except (OSError, ValueError):
            return 1
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    try:
        collector = Collector()
        if args.init_schema:
            collector.initialize()
            return 0
        while not STOP.is_set():
            try:
                collector.sync()
            except Exception as error:
                # Keep unexpected responses and credentials out of logs, too.
                detail = str(error) if isinstance(error, CollectorError) else type(error).__name__
                LOG.error('Synchronization failed: %s; retrying next cycle', detail)
                if args.once:
                    return 1
            if args.once:
                return 0 if not STOP.is_set() else 1
            STOP.wait(collector.interval)
        return 0
    except (KeyError, ValueError, RuntimeError):
        LOG.error('Collector initialization failed; check configuration and schema permissions')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
