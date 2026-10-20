# Cloudflare Analytics Collector

Copies hourly Cloudflare CDN response estimates into ClickHouse every 15 minutes for Grafana. Uses Python's standard library; no public HTTP endpoint.

Set `CLOUDFLARE_API_TOKEN` (zone-scoped Analytics Read), `CLOUDFLARE_ZONE_ID`, `CLOUDFLARE_HOSTNAME`, `CLICKHOUSE_URL`, `CLICKHOUSE_USER` and `CLICKHOUSE_PASSWORD`. Keep credentials in the deployment secret configuration. `CLICKHOUSE_DATABASE` defaults to `cloudflare`.

Initialize once with `--init-schema` using schema-management credentials. Run the service with an account granted SELECT/INSERT on `http_hourly` and `sync_windows`; Grafana needs SELECT on `http_hourly_latest` and `sync_windows`. The normal service does not create or alter schema. `--once` synchronizes once and exits with a nonzero status on failure.

Defaults: `SYNC_INTERVAL_SECONDS=900`, `BACKFILL_DAYS=30`, `RECHECK_HOURS=48`, `INGESTION_LAG_HOURS=1`. Startup backfills available history; later cycles refresh the recent 48 hours and resume interrupted backfills using ClickHouse progress. Queries cover at most 24 hours each. History beyond the configured window is reported as a gap and cannot be recovered from this service. Run one replica. Retained rows have no automatic TTL.

Query `http_hourly_latest`, **not the underlying versioned table**, to avoid counting repeated snapshots. Daily bytes are `sum(response_bytes)` grouped by UTC date; hourly average Mbps is `response_bytes * 8 / 3600 / 1000000`. Daily TB divides bytes by `1000000000000`. Cloudflare already scales its sampled estimates; do not multiply by `sample_interval` again. Requests, bytes and sampling metadata describe this hostname's edge responses, not completed downloads or people.

A successful empty API window records progress but does not synthesize zero traffic in the query view. Missing buckets are stored as tombstones so an upstream revision can remove an earlier observation. API errors or failed inserts do not advance progress. Check `max(window_end)` and `max(collected_at)` in `sync_windows` for freshness. The health check becomes unhealthy after three missed cycles plus five minutes. Token expiry or upstream schema changes require operator attention; failures preserve previously collected data. Revisions outside the recheck window require an explicit backfill. Website PV and unique IPs remain separate queries against the existing Caddy logs.
