-- 0001_init.sql — Kube Huddle state DB (PostgreSQL dialect).
-- Tiers 1–4 per docs/04 §B. Production backend. The metric_samples table is a
-- TimescaleDB hypertable candidate (left as a plain table here; convert with
-- SELECT create_hypertable('metric_samples','ts') where Timescale is available).
--
-- Deviation from docs/04 §B (confirmed with Yash): disc_workloads carries a
-- workload_uid column so the engine can resolve metric_samples.workload_uid ->
-- (namespace, kind, name, requests) for recommendation cards. All else matches §B.

-- ============================================================================
-- Tier 1 — Config (persistent)
-- ============================================================================

CREATE TABLE clusters (
    id                BIGSERIAL PRIMARY KEY,
    name              TEXT NOT NULL UNIQUE,
    api_url           TEXT,
    auth_method       TEXT CHECK (auth_method IN ('kubeconfig', 'token', 'client_cert', 'basic')),
    credential_ref    TEXT,                       -- points at a k8s Secret; never the raw creds
    ca_cert           TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_connected_at TIMESTAMPTZ,
    status            TEXT NOT NULL DEFAULT 'unknown'
);

CREATE TABLE data_sources (
    id              BIGSERIAL PRIMARY KEY,
    cluster_id      BIGINT REFERENCES clusters(id) ON DELETE CASCADE,   -- NULL = global fallback
    type            TEXT NOT NULL CHECK (type IN ('prometheus', 'custom_api', 'file', 'opencost', 'mesh', 'interactions')),
    name            TEXT NOT NULL,
    endpoint        TEXT,
    auth_config     JSONB,
    settings        JSONB,
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    health          TEXT,
    last_checked_at TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE settings (
    id                INT PRIMARY KEY CHECK (id = 1),
    metric_ttl_hours  INTEGER NOT NULL DEFAULT 24,
    discovery_ttl_min INTEGER NOT NULL DEFAULT 10,
    result_ttl_hours  INTEGER NOT NULL DEFAULT 24,   -- 1 day (docs/04 §A + §F.3)
    default_resources TEXT    NOT NULL DEFAULT 'cpu,memory',
    default_window    TEXT    NOT NULL DEFAULT '7d',
    thresholds        JSONB   NOT NULL DEFAULT '{"seasonality_gain":0.30,"band":0.10,"jump_min":50,"ratio_max":0.5,"min_period":3}'::jsonb
);
INSERT INTO settings (id) VALUES (1);

-- ============================================================================
-- Tier 2 — Discovery cache (short TTL, refreshable)
-- ============================================================================

CREATE TABLE disc_namespaces (
    id         BIGSERIAL PRIMARY KEY,
    cluster_id BIGINT NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    UNIQUE (cluster_id, name)
);

CREATE TABLE disc_workloads (
    id                 BIGSERIAL PRIMARY KEY,
    cluster_id         BIGINT NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
    workload_uid       TEXT NOT NULL,              -- join key for metric_samples.workload_uid
    namespace          TEXT NOT NULL,
    kind               TEXT NOT NULL,
    name               TEXT NOT NULL,
    replicas           INTEGER,
    requests_cpu_m     INTEGER,                    -- millicores
    requests_mem_bytes BIGINT,
    labels             JSONB,
    fetched_at         TIMESTAMPTZ NOT NULL,
    UNIQUE (cluster_id, workload_uid)
);

CREATE TABLE disc_pods (
    id                 BIGSERIAL PRIMARY KEY,
    cluster_id         BIGINT NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
    namespace          TEXT NOT NULL,
    workload_name      TEXT NOT NULL,
    pod_name           TEXT NOT NULL,
    node_name          TEXT,
    node_instance_type TEXT,
    fetched_at         TIMESTAMPTZ NOT NULL,
    UNIQUE (cluster_id, namespace, pod_name)
);

-- ============================================================================
-- Tier 3 — Collected data (TTL, default 1 day) — written by the collector
-- ============================================================================

CREATE TABLE metric_samples (
    id            BIGSERIAL PRIMARY KEY,
    cluster_id    BIGINT NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
    workload_uid  TEXT NOT NULL,
    resource      TEXT NOT NULL,
    resource_kind TEXT NOT NULL DEFAULT 'standard' CHECK (resource_kind IN ('standard', 'network', 'custom')),
    ts            TIMESTAMPTZ NOT NULL,
    value         DOUBLE PRECISION NOT NULL,
    unit          TEXT,
    is_rate       BOOLEAN NOT NULL DEFAULT FALSE,
    collected_at  TIMESTAMPTZ NOT NULL,
    UNIQUE (cluster_id, workload_uid, resource, ts)   -- idempotent re-ingest
);
CREATE INDEX idx_metric_samples_lookup    ON metric_samples (cluster_id, workload_uid, resource, ts);
CREATE INDEX idx_metric_samples_collected ON metric_samples (collected_at);

CREATE TABLE interactions (
    id               BIGSERIAL PRIMARY KEY,
    cluster_id       BIGINT NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
    src_workload_uid TEXT NOT NULL,
    dst_workload_uid TEXT NOT NULL,
    avg_count        DOUBLE PRECISION,
    window_start     TIMESTAMPTZ,
    window_end       TIMESTAMPTZ,
    collected_at     TIMESTAMPTZ NOT NULL,
    UNIQUE (cluster_id, src_workload_uid, dst_workload_uid, window_start)
);

CREATE TABLE collection_runs (
    id           BIGSERIAL PRIMARY KEY,
    cluster_id   BIGINT NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
    scope        JSONB,
    resources    JSONB,
    window_start TIMESTAMPTZ,
    window_end   TIMESTAMPTZ,
    sources_used JSONB,
    status       TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'running', 'success', 'failed', 'partial')),
    rows_written INTEGER NOT NULL DEFAULT 0,
    data_as_of   TIMESTAMPTZ,
    started_at   TIMESTAMPTZ,
    finished_at  TIMESTAMPTZ,
    error        TEXT
);

-- ============================================================================
-- Tier 4 — Runs + results (TTL, default 1 day) — written by the engine
-- ============================================================================

CREATE TABLE analysis_runs (
    id                BIGSERIAL PRIMARY KEY,
    name              TEXT NOT NULL UNIQUE,          -- generated slug, e.g. "brave-otter-4821"
    cluster_id        BIGINT NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
    run_type          TEXT NOT NULL DEFAULT 'latency',
    scope             JSONB,
    config            JSONB,
    collection_run_id BIGINT REFERENCES collection_runs(id) ON DELETE SET NULL,
    data_as_of        TIMESTAMPTZ,
    stale             BOOLEAN NOT NULL DEFAULT FALSE,
    status            TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'collecting', 'running', 'completed', 'failed')),
    created_at        TIMESTAMPTZ NOT NULL,
    completed_at      TIMESTAMPTZ,
    expires_at        TIMESTAMPTZ,
    error             TEXT
);

CREATE TABLE recommendations (
    id               BIGSERIAL PRIMARY KEY,
    run_id           BIGINT NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
    workload_kind    TEXT,
    workload_name    TEXT,
    namespace        TEXT,
    from_type        TEXT,
    to_target        TEXT CHECK (to_target IN ('Job', 'CronJob', 'KEDA', 'Knative')),
    cadence          TEXT,
    run_time         TEXT,
    duration         TEXT,
    savings_amount   DOUBLE PRECISION,
    savings_currency TEXT,
    savings_period   TEXT,
    confidence       TEXT CHECK (confidence IN ('high', 'medium', 'low')),
    summary_text     TEXT
);
CREATE INDEX idx_recommendations_run ON recommendations (run_id);

CREATE TABLE recommendation_evidence (
    id                  BIGSERIAL PRIMARY KEY,
    recommendation_id   BIGINT NOT NULL REFERENCES recommendations(id) ON DELETE CASCADE,
    resource            TEXT NOT NULL,
    jump_pct            DOUBLE PRECISION,
    active_idle_ratio   DOUBLE PRECISION,
    period_hours        DOUBLE PRECISION,
    active_duration_min DOUBLE PRECISION,
    overlap_pct         DOUBLE PRECISION,
    trend_value         DOUBLE PRECISION,
    eps_min             DOUBLE PRECISION,
    eps_max             DOUBLE PRECISION,
    active_windows      JSONB,
    series              JSONB
);
CREATE INDEX idx_evidence_rec ON recommendation_evidence (recommendation_id);

CREATE TABLE recommendation_peers (
    id                 BIGSERIAL PRIMARY KEY,
    recommendation_id  BIGINT NOT NULL REFERENCES recommendations(id) ON DELETE CASCADE,
    peer_workload      TEXT,
    shared_seasonality BOOLEAN,
    savings_amount     DOUBLE PRECISION,
    to_target          TEXT,
    note               TEXT
);
CREATE INDEX idx_peers_rec ON recommendation_peers (recommendation_id);
