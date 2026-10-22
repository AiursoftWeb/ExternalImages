# HTTP Error AlertCenter Bridge

Monitors each configured hostname in a server's Caddy ClickHouse logs. Checks every minute using a complete fifteen-minute window, with a one-minute ingestion allowance. Daily P95/P99 thresholds use up to thirty days of history.

Sev3 requires three anomalous checks (at least 100 requests, 10 failures and max(P95, 1%) error rate). Three complete minutes at 90% failures, with at least five failures, also trigger sev3 during cold start. Sev2 additionally requires 150 failures, max(P99, 5%), three severe checks, and **at least 35 minutes since AlertCenter actually created the sev3**. Five healthy checks automatically mitigate; final resolution is manual.

Run one replica per server with:

- `SERVER_NAME`: stable server identity.
- `MONITORED_HOSTS`: comma-separated exact hostnames, matching the deployed Caddy sites.
- `CLICKHOUSE_CONNECTION_STRING`: existing read-only access to `logs.caddy_requests`.
- `ALERTCENTER_URL` and `ALERTCENTER_API_KEY`: dedicated application credentials with query, create, severity, metadata and mitigate permissions.
- A persistent writable `/data` volume for UID 1654 (override with `DATA_DIRECTORY`).

History needs seven observed UTC dates. Incident thresholds are frozen; a cold-start incident learns its escalation threshold once history becomes available. Recovery requires a fifteen-minute rate below half the incident's sev3 threshold, a five-minute rate at or below that value, and recent response evidence. Empty traffic and stale collection do not prove recovery.

The source must deliver complete logs within the ingestion allowance. The bridge rejects stale node traffic, but request timestamps alone cannot prove ingestion completeness. It reads only aggregated counts, excludes status 101, and never creates or changes ClickHouse tables. Startup loads history; later refreshes append missing days rather than rescan thirty days each minute. Keep the hostname scope current with the server's sites.
