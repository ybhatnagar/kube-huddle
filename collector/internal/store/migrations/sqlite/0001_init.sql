-- 0001_init.sql — Kube Huddle state DB (SQLite dialect).
-- Tiers 1–4 per docs/04 §B. SQLite is the dev/single-node fallback; Postgres is
-- the production backend (see ../postgres/0001_init.sql — same logical schema).
--
-- Deviation from docs/04 §B (confirmed with Yash): disc_workloads carries a
-- workload_uid column so the engine can resolve metric_samples.workload_uid ->
-- (namespace, kind, name, requests) for recommendation cards. All else matches §B.
--
-- Timestamps are stored as ISO-8601 UTC text ("2006-01-02T15:04:05Z"); this sorts
-- lexicographically, so range/TTL comparisons work directly. JSON columns are TEXT.

-- ============================================================================
-- Tier 1 — Config (persistent)
-- ============================================================================

CREATE TABLE clusters (
    id                INTEGER PRIMARY KEY,
    name              TEXT NOT NULL UNIQUE,
    api_url           TEXT,
    auth_method       TEXT CHECK (auth_method IN ('kubeconfig', 'token', 'client_cert', 'basic')),
    credential_ref    TEXT,                       -- points at a k8s Secret; never the raw creds
    ca_cert           TEXT,
    created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    last_connected_at TEXT,
    status            TEXT NOT NULL DEFAULT 'unknown'
);

CREATE TABLE data_sources (
    id              INTEGER PRIMARY KEY,
    cluster_id      INTEGER REFERENCES clusters(id) ON DELETE CASCADE,  -- NULL = global fallback
    type            TEXT NOT NULL CHECK (type IN ('prometheus', 'custom_api', 'file', 'opencost', 'mesh', 'interactions')),
    name            TEXT NOT NULL,
    endpoint        TEXT,
    auth_config     TEXT,                          -- JSON
    settings        TEXT,                          -- JSON
    enabled         INTEGER NOT NULL DEFAULT 1,
    health          TEXT,
    last_checked_at TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- Single-row global defaults.
CREATE TABLE settings (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    metric_ttl_hours  INTEGER NOT NULL DEFAULT 24,
    discovery_ttl_min INTEGER NOT NULL DEFAULT 10,
    result_ttl_hours  INTEGER NOT NULL DEFAULT 24,   -- 1 day (docs/04 §A + §F.3)
    default_resources TEXT    NOT NULL DEFAULT 'cpu,memory',
    default_window    TEXT    NOT NULL DEFAULT '7d',
    thresholds        TEXT    NOT NULL DEFAULT '{"seasonality_gain":0.30,"band":0.10,"jump_min":50,"ratio_max":0.5,"min_period":3}'
);
INSERT INTO settings (id) VALUES (1);

-- ============================================================================
-- Tier 2 — Discovery cache (short TTL, refreshable)
-- ============================================================================

CREATE TABLE disc_namespaces (
    id         INTEGER PRIMARY KEY,
    cluster_id INTEGER NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    UNIQUE (cluster_id, name)
);

CREATE TABLE disc_workloads (
    id                 INTEGER PRIMARY KEY,
    cluster_id         INTEGER NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
    workload_uid       TEXT NOT NULL,              -- join key for metric_samples.workload_uid
    namespace          TEXT NOT NULL,
    kind               TEXT NOT NULL,
    name               TEXT NOT NULL,
    replicas           INTEGER,
    requests_cpu_m     INTEGER,                    -- millicores
    requests_mem_bytes INTEGER,
    labels             TEXT,                        -- JSON
    fetched_at         TEXT NOT NULL,
    UNIQUE (cluster_id, workload_uid)
);

CREATE TABLE disc_pods (
    id                 INTEGER PRIMARY KEY,
    cluster_id         INTEGER NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
    namespace          TEXT NOT NULL,
    workload_name      TEXT NOT NULL,
    pod_name           TEXT NOT NULL,
    node_name          TEXT,
    node_instance_type TEXT,
    fetched_at         TEXT NOT NULL,
    UNIQUE (cluster_id, namespace, pod_name)
);

-- ============================================================================
-- Tier 3 — Collected data (TTL, default 1 day) — written by the collector
-- ============================================================================

CREATE TABLE metric_samples (
    id            INTEGER PRIMARY KEY,
    cluster_id    INTEGER NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
    workload_uid  TEXT NOT NULL,
    resource      TEXT NOT NULL,
    resource_kind TEXT NOT NULL DEFAULT 'standard' CHECK (resource_kind IN ('standard', 'network', 'custom')),
    ts            TEXT NOT NULL,
    value         REAL NOT NULL,
    unit          TEXT,
    is_rate       INTEGER NOT NULL DEFAULT 0,
    collected_at  TEXT NOT NULL,
    UNIQUE (cluster_id, workload_uid, resource, ts)   -- idempotent re-ingest
);
CREATE INDEX idx_metric_samples_lookup    ON metric_samples (cluster_id, workload_uid, resource, ts);
CREATE INDEX idx_metric_samples_collected ON metric_samples (collected_at);

CREATE TABLE interactions (
    id               INTEGER PRIMARY KEY,
    cluster_id       INTEGER NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
    src_workload_uid TEXT NOT NULL,
    dst_workload_uid TEXT NOT NULL,
    avg_count        REAL,
    window_start     TEXT,
    window_end       TEXT,
    collected_at     TEXT NOT NULL,
    UNIQUE (cluster_id, src_workload_uid, dst_workload_uid, window_start)
);

CREATE TABLE collection_runs (
    id           INTEGER PRIMARY KEY,
    cluster_id   INTEGER NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
    scope        TEXT,                              -- JSON: "all" | {workload_uids:[...]}
    resources    TEXT,                              -- JSON
    window_start TEXT,
    window_end   TEXT,
    sources_used TEXT,                              -- JSON
    status       TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'running', 'success', 'failed', 'partial')),
    rows_written INTEGER NOT NULL DEFAULT 0,
    data_as_of   TEXT,
    started_at   TEXT,
    finished_at  TEXT,
    error        TEXT
);

-- ============================================================================
-- Tier 4 — Runs + results (TTL, default 1 day) — written by the engine
-- ============================================================================

CREATE TABLE analysis_runs (
    id                INTEGER PRIMARY KEY,
    name              TEXT NOT NULL UNIQUE,          -- generated slug, e.g. "brave-otter-4821"
    cluster_id        INTEGER NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
    run_type          TEXT NOT NULL DEFAULT 'latency',
    scope             TEXT,                          -- JSON
    config            TEXT,                          -- JSON: resources, window, thresholds, min_period
    collection_run_id INTEGER REFERENCES collection_runs(id) ON DELETE SET NULL,
    data_as_of        TEXT,
    stale             INTEGER NOT NULL DEFAULT 0,
    status            TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'collecting', 'running', 'completed', 'failed')),
    created_at        TEXT NOT NULL,
    completed_at      TEXT,
    expires_at        TEXT,
    error             TEXT
);

CREATE TABLE recommendations (
    id               INTEGER PRIMARY KEY,
    run_id           INTEGER NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
    workload_kind    TEXT,
    workload_name    TEXT,
    namespace        TEXT,
    from_type        TEXT,
    to_target        TEXT CHECK (to_target IN ('Job', 'CronJob', 'KEDA', 'Knative')),
    cadence          TEXT,
    run_time         TEXT,
    duration         TEXT,
    savings_amount   REAL,
    savings_currency TEXT,
    savings_period   TEXT,
    confidence       TEXT CHECK (confidence IN ('high', 'medium', 'low')),
    summary_text     TEXT
);
CREATE INDEX idx_recommendations_run ON recommendations (run_id);

CREATE TABLE recommendation_evidence (
    id                  INTEGER PRIMARY KEY,
    recommendation_id   INTEGER NOT NULL REFERENCES recommendations(id) ON DELETE CASCADE,
    resource            TEXT NOT NULL,
    jump_pct            REAL,
    active_idle_ratio   REAL,
    period_hours        REAL,
    active_duration_min REAL,
    overlap_pct         REAL,
    trend_value         REAL,
    eps_min             REAL,
    eps_max             REAL,
    active_windows      TEXT,                        -- JSON: [{start,end}]
    series              TEXT                         -- JSON: downsampled points + overlay
);
CREATE INDEX idx_evidence_rec ON recommendation_evidence (recommendation_id);

CREATE TABLE recommendation_peers (
    id                 INTEGER PRIMARY KEY,
    recommendation_id  INTEGER NOT NULL REFERENCES recommendations(id) ON DELETE CASCADE,
    peer_workload      TEXT,
    shared_seasonality INTEGER,
    savings_amount     REAL,
    to_target          TEXT,
    note               TEXT
);
CREATE INDEX idx_peers_rec ON recommendation_peers (recommendation_id);
