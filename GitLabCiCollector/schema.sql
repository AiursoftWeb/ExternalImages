CREATE DATABASE IF NOT EXISTS {database};
CREATE TABLE IF NOT EXISTS {database}.jobs
(
    source LowCardinality(String),
    job_id UInt64,
    partition_id UInt64,
    project_id UInt64,
    pipeline_id UInt64,
    name String,
    status LowCardinality(String),
    runner_id Nullable(UInt64),
    queued_at DateTime64(6, 'UTC'),
    started_at Nullable(DateTime64(6, 'UTC')),
    finished_at Nullable(DateTime64(6, 'UTC')),
    source_updated_at DateTime64(6, 'UTC')
)
ENGINE = ReplacingMergeTree(source_updated_at)
ORDER BY (source, job_id, partition_id);
CREATE VIEW IF NOT EXISTS {database}.jobs_latest AS
SELECT * FROM {database}.jobs FINAL;
CREATE TABLE IF NOT EXISTS {database}.sync_windows
(
    source LowCardinality(String),
    through DateTime64(6, 'UTC'),
    collected_at DateTime64(6, 'UTC'),
    rows UInt64
)
ENGINE = MergeTree ORDER BY (source, through);
