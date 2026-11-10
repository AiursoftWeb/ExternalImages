# HTTP Error AlertCenter Bridge

Monitors hostnames in a server's Caddy ClickHouse logs. Checks every minute using a complete fifteen-minute window, with a one-minute ingestion allowance. Daily P95/P99 thresholds use up to thirty days of history.

Sev3 requires three anomalous checks (at least 100 requests, 10 failures and max(P95, 1%) error rate). Three complete minutes at 90% failures, with at least five failures, also trigger sev3 during cold start. Sev2 additionally requires 150 failures, max(P99, 5%), three severe checks, and **at least 35 minutes since AlertCenter actually created the sev3**. Five healthy checks automatically mitigate; final resolution is manual.

Run one replica per server with:

- `SERVER_NAME`: stable server identity.
- `MONITORED_HOSTS`: `*` discovers all DNS hostnames observed in logs, including newly added domains; alternatively supply comma-separated exact hostnames. Wildcards cannot be mixed with a list. IP addresses and malformed hosts are excluded.
- `CLICKHOUSE_CONNECTION_STRING`: existing read-only access to `logs.caddy_requests`.
- `ALERTCENTER_URL` and `ALERTCENTER_API_KEY`: dedicated application credentials with query, create, severity, metadata and mitigate permissions.
- A persistent writable `/data` volume for UID 1654 (override with `DATA_DIRECTORY`).

History needs seven observed UTC dates. Incident thresholds are frozen; a cold-start incident learns its escalation threshold once history becomes available. Recovery requires a fifteen-minute rate below half the incident's sev3 threshold, a five-minute rate at or below that value, and recent response evidence. Empty traffic and stale collection do not prove recovery.

The source must deliver complete logs within the ingestion allowance. The bridge rejects stale node traffic, but request timestamps alone cannot prove ingestion completeness. It reads only aggregated counts, excludes status 101, and never creates or changes ClickHouse tables. Startup loads history; later refreshes append missing days rather than rescan thirty days each minute. Wildcard mode also includes valid DNS names supplied by clients, rather than a Caddy configuration allowlist. Use an explicit list to restrict scope. History stores only nonempty five-minute buckets; quiet hosts do not imply recovery.

Optional request-abuse detection uses the same application, credentials and volume. Set `HTTP_ABUSE_ENABLED=true` only after verifying trusted client IP attribution and timely, complete access logs. `HTTP_ABUSE_EXCLUDED_SOURCE_CIDRS` accepts comma-separated approved source networks (IPv6 /64 or broader); private/reserved sources are excluded automatically. IPv4 hosts and IPv6 /64 groups are evaluated symmetrically across servers.

The detector confirms sustained repeated downloads over 20/60 minutes or high request volume over five minutes. Suspected activity creates sev3; attributable server failures or measured latency degradation for other visitors can create or upgrade to sev2. One active abuse incident per server collects the evidence. Ten fresh complete minutes of low activity for every implicated source automatically mitigate, including after blocking. It never blocks traffic, resolves or reopens incidents. Confirmed reports persist through API outages and restarts; manual closure suppresses the same continuing activity until recovery. This feature is disabled by default and does not change the hostname error rules above.
