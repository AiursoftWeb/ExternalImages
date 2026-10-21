# GitLab CI Collector

Copies GitLab job lifecycle metadata into ClickHouse for Runner backlog and slow-job dashboards. One replica, no public endpoint, no job logs or artifacts.

Set PostgreSQL's standard `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER` and authentication settings for a read-only account allowed to select the required `p_ci_builds` columns. Set `CLICKHOUSE_URL`, `CLICKHOUSE_USER`, `CLICKHOUSE_PASSWORD`; `CLICKHOUSE_DATABASE` defaults to `GitLabMetrics`, and `GITLAB_SOURCE` identifies the source instance. Credentials belong in deployment secrets. The container does not configure GitLab database access or require Docker/SSH administration privileges.

Run `--init-schema` once with schema-management credentials. Normal operation needs SELECT/INSERT on `jobs` and `sync_windows`; Grafana needs SELECT on `jobs_latest` and its underlying `jobs` table (view invoker permissions), plus `sync_windows`. Queries must use `jobs_latest` to immediately deduplicate repeated observations.

Defaults: `SYNC_INTERVAL_SECONDS=60`, `BACKFILL_DAYS=30`, `RECHECK_SECONDS=300`, `BATCH_SIZE=2000`. Collection uses indexed `updated_at` ranges, hourly backfill windows, keyset pagination and batch inserts. Active jobs are also refreshed regardless of age. PostgreSQL queries are read-only with 15-second statement and one-second lock timeouts. Progress advances only after a complete window is confirmed written. Failed or uncertain writes can be replayed without counting jobs twice. Run `--once` for one synchronization; it returns nonzero on failure.

The ten-second collection lag and five-minute overlap cover ordinary transaction delays, not arbitrary historical edits or transactions committing outside that overlap. Source history beyond the configured backfill window cannot be recovered automatically after a prolonged outage. Removed source jobs are retained as historical observations. Tables have no automatic retention policy. Health checks require both a recent successful collection and a recent fully copied boundary; missing or stale collection is not zero backlog.

Reconstruct backlog from `queued_at <= time < finished_at`, with unfinished jobs extending only through the successfully collected boundary. Running intervals begin at `started_at`. Exclude jobs not yet queued, count retries by their distinct job IDs, and include jobs crossing the selected time boundary. Timestamps are UTC; `source_updated_at` orders revisions, not the chart's time axis. Configured Runner capacity is a reference, not a cap on overlapping historical running records.
