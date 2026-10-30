CREATE DATABASE IF NOT EXISTS {database};

CREATE TABLE IF NOT EXISTS {database}.http_hourly
(
    zone_id String,
    hostname String,
    hour DateTime('UTC'),
    response_bytes UInt64,
    requests UInt64,
    sample_interval Float64,
    is_present UInt8,
    collected_at DateTime64(6, 'UTC'),
    version UInt64
)
ENGINE = ReplacingMergeTree(version)
PARTITION BY toYYYYMM(hour)
ORDER BY (zone_id, hostname, hour);

CREATE TABLE IF NOT EXISTS {database}.http_daily_uniques
(
    zone_id String,
    hostname String,
    date Date,
    unique_ips UInt64,
    collected_at DateTime64(6, 'UTC'),
    version UInt64
)
ENGINE = ReplacingMergeTree(version)
PARTITION BY toYYYYMM(date)
ORDER BY (zone_id, hostname, date);

CREATE TABLE IF NOT EXISTS {database}.sync_windows
(
    zone_id String,
    hostname String,
    window_start DateTime('UTC'),
    window_end DateTime('UTC'),
    collected_at DateTime64(6, 'UTC'),
    version UInt64
)
ENGINE = ReplacingMergeTree(version)
ORDER BY (zone_id, hostname, window_start, window_end);

CREATE VIEW IF NOT EXISTS {database}.http_hourly_latest AS
SELECT zone_id, hostname, hour,
    tupleElement(latest, 1) AS response_bytes,
    tupleElement(latest, 2) AS requests,
    tupleElement(latest, 3) AS sample_interval,
    tupleElement(latest, 4) AS collected_at
FROM
(
    SELECT zone_id, hostname, hour,
        argMax(tuple(response_bytes, requests, sample_interval, collected_at, is_present), version) AS latest
    FROM {database}.http_hourly
    GROUP BY zone_id, hostname, hour
)
WHERE tupleElement(latest, 5) = 1;

CREATE VIEW IF NOT EXISTS {database}.http_daily_uniques_latest AS
SELECT zone_id, hostname, date,
    tupleElement(latest, 1) AS unique_ips,
    tupleElement(latest, 2) AS collected_at
FROM
(
    SELECT zone_id, hostname, date,
        argMax(tuple(unique_ips, collected_at), version) AS latest
    FROM {database}.http_daily_uniques
    GROUP BY zone_id, hostname, date
);
