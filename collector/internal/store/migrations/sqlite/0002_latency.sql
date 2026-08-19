-- 0002_latency.sql — Latency-domain tables (SQLite dialect). Additive over 0001_init.sql.
-- Per docs/04 §B. Tier 3 = collector output; Tier 4 = engine output for run_type='latency'.

-- ============================================================================
-- Tier 3 — raw latency time-series (mirrors metric_samples shape).
-- Raw only; the engine does the α/i smoothing (docs/05 Stage 1).
-- ============================================================================

CREATE TABLE interaction_latency_samples (
    id               INTEGER PRIMARY KEY,
    cluster_id       INTEGER NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
    src_workload_uid TEXT NOT NULL,
    dst_workload_uid TEXT NOT NULL,
    ts               TEXT NOT NULL,
    latency_ms       REAL NOT NULL,
    unit             TEXT DEFAULT 'ms',
    collected_at     TEXT NOT NULL,
    UNIQUE (cluster_id, src_workload_uid, dst_workload_uid, ts)
);
CREATE INDEX idx_latency_lookup    ON interaction_latency_samples (cluster_id, src_workload_uid, dst_workload_uid, ts);
CREATE INDEX idx_latency_collected ON interaction_latency_samples (collected_at);

-- ============================================================================
-- Tier 4 — engine result tables for run_type='latency'.
-- ============================================================================

CREATE TABLE latency_groups (
    id             INTEGER PRIMARY KEY,
    run_id         INTEGER NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
    group_index    INTEGER NOT NULL,
    label          TEXT,
    node_names     TEXT NOT NULL,                    -- JSON array
    latency_ratio  REAL,
    workload_count INTEGER,
    total_size     REAL,
    color_hint     TEXT
);
CREATE INDEX idx_latency_groups_run ON latency_groups (run_id);

CREATE TABLE latency_group_workloads (
    id            INTEGER PRIMARY KEY,
    group_id      INTEGER NOT NULL REFERENCES latency_groups(id) ON DELETE CASCADE,
    run_id        INTEGER NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
    workload_uid  TEXT NOT NULL,
    workload_name TEXT,
    namespace     TEXT,
    kind          TEXT,
    node_name     TEXT,
    size_value    REAL,
    size_metric   TEXT DEFAULT 'interaction_volume'
);
CREATE INDEX idx_lgw_group ON latency_group_workloads (group_id);

CREATE TABLE latency_edges (
    id           INTEGER PRIMARY KEY,
    run_id       INTEGER NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
    node_a       TEXT NOT NULL,
    node_b       TEXT NOT NULL,
    weight_ms    REAL NOT NULL,
    sample_count INTEGER,
    kind         TEXT CHECK (kind IN ('internal','external')),
    group_id     INTEGER REFERENCES latency_groups(id) ON DELETE SET NULL
);
CREATE INDEX idx_latency_edges_run ON latency_edges (run_id);

CREATE TABLE latency_recommendations (
    id                  INTEGER PRIMARY KEY,
    run_id              INTEGER NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
    workload_uid        TEXT NOT NULL,
    workload_kind       TEXT,
    workload_name       TEXT,
    namespace           TEXT,
    action              TEXT CHECK (action IN ('migrate','replicate')),
    from_scope          TEXT,
    to_scope            TEXT,
    from_group_index    INTEGER,
    to_group_index      INTEGER,
    cost_saving_amount  REAL,
    savings_currency    TEXT,
    savings_period      TEXT,
    latency_delta_ms    REAL,
    suggested_mechanism TEXT,                          -- 'node_affinity'|'node_selector'|'topology_spread'
    suggested_snippet   TEXT,                          -- copy-pasteable YAML; recommend-only, tool never applies
    confidence          TEXT CHECK (confidence IN ('high','medium','low')),
    summary_text        TEXT
);
CREATE INDEX idx_latency_rec_run ON latency_recommendations (run_id);

CREATE TABLE latency_evidence (
    id                INTEGER PRIMARY KEY,
    recommendation_id INTEGER NOT NULL REFERENCES latency_recommendations(id) ON DELETE CASCADE,
    subgraph          TEXT,                            -- JSON: {nodes,edges}
    detail            TEXT                             -- JSON: {volume/latency/cost before/after}
);
CREATE INDEX idx_latency_evidence_rec ON latency_evidence (recommendation_id);

CREATE TABLE latency_peers (
    id                INTEGER PRIMARY KEY,
    recommendation_id INTEGER NOT NULL REFERENCES latency_recommendations(id) ON DELETE CASCADE,
    peer_workload_uid TEXT,
    peer_workload     TEXT,
    relation          TEXT,
    avg_count         REAL,
    latency_ms        REAL
);
CREATE INDEX idx_latency_peers_rec ON latency_peers (recommendation_id);
