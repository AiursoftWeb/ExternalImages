# Traffic AlertCenter Bridge

Reports sustained, concentrated HTTP downloads from Caddy ClickHouse logs, including successful responses. It only reports incidents; it never blocks addresses or changes public lists.

Every minute, it reads ten complete minutes with a one-minute ingestion allowance. A public IPv4 address or IPv6 /64 qualifies at **150,000 B/s and 60% of response bytes**, with at least eight minutes and the latest minute at 75,000 B/s. Three consecutive checks create sev3. Five fresh minutes below 75,000 B/s automatically mitigate; final resolution is manual. Ordinary downloads can qualify: this is occupancy detection, not attack attribution.

Run one replica per server with:

- `SERVER_NAME`: stable server identifier.
- `MONITORED_HOSTS`: `*` or comma-separated exact DNS hostnames.
- `CLICKHOUSE_CONNECTION_STRING`: read-only access to `logs.caddy_requests`.
- `ALERTCENTER_URL`, `ALERTCENTER_API_KEY`: dedicated application credentials with query, create, metadata and mitigate permissions.
- Persistent writable `/data` for UID 1654 (`DATA_DIRECTORY` overrides the path).
- Optional `EXCLUDED_SOURCE_CIDRS`: comma-separated probe or proxy networks; IPv6 exclusions must be /64 or broader.

The source identity is `remote_ip`; do not trust arbitrary forwarded headers. The upstream must log restored client addresses, or proxy ranges must be excluded to avoid treating shared proxies as clients. Private, loopback and reserved sources are excluded. Only aggregates and top hostnames are reported; status 101 is excluded. No ClickHouse schema changes are made.

Every minute in the observation window and the subsequent ingestion minute must have source log activity, including internal probes. Missing minutes stop occupancy evaluation and recovery. Three failed collection checks create a separate sev3; five successful checks mitigate it. This deliberately requires an active log source: quiet installations need an existing probe. Request timestamps cannot prove that individual requests were not lost or delayed.

Durable pending report IDs and payloads survive restarts and uncertain delivery. Recovery uses AlertCenter's optimistic versions; resolved incidents are never reopened. Collection and occupancy have separate incident identities. Multi-source pressure, non-HTTP traffic and live bandwidth on long-running responses need complementary network monitoring.
