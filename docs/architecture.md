# Architecture

Kube Huddle is three independently deployable modules that meet at a state
database. The engine never calls the collector directly; the collector never
calls the engine directly. All coordination flows through the DB schema.
That's the only cross-module contract worth being religious about.

```
   ┌─────────────┐    read-only     ┌──────────────────────────┐
   │  Kubernetes │  ← ─ ─ ─ ─ ─ ─   │  collector (Go)          │
   │  API server │                   │  ─────────────────────   │
   │             │                   │  MetricsConnector        │
   │  Prometheus │  ← ─ ─ ─ ─ ─ ─   │  InteractionConnector    │
   │  (Hubble /  │                   │  LatencyConnector        │
   │   Istio /   │                   │  DiscoverConnector       │
   │   OTel)     │                   └──────────────────────────┘
   └─────────────┘                             │
                                               ▼
                                        ┌───────────────────────┐
                                        │  State DB             │
                                        │  (Postgres / SQLite)  │
                                        │                       │
                                        │  Tier 1: config       │
                                        │  Tier 2: discovery    │
                                        │  Tier 3: collected    │
                                        │  Tier 4: analysis     │
                                        └───────────────────────┘
                                               ▲              ▲
                                               │              │
                                        writes │              │ reads
                              ┌───────────────────────┐     ┌──────────────┐
                              │  engine (Python)      │     │  UI (static) │
                              │  ─────────────────    │     │  ────────    │
                              │  analysis_core        │     │  vanilla JS  │
                              │  recommenders/latency │     │  self-       │
                              │    smooth → weigh →   │     │  contained   │
                              │    group → whatif     │     │  SVG orb +   │
                              │  FastAPI /api/v1      │◄────│  force-graph │
                              └───────────────────────┘  HTTP
```

## The three modules

### Collector (`collector/`, Go)

**Job:** pull data from external sources; write normalized rows to the state DB.

**Extension points** — all interface-based, self-registered in `init()`:

- **`MetricsConnector`** — pulls per-workload CPU/mem/network series. Reference impl: Prometheus. Any source that emits time-series can implement this.
- **`InteractionConnector`** — pulls src→dst call counts (`interactions.avg_count`). Cilium/Hubble, Istio, OTel service-graph, or plain Prometheus mesh histograms.
- **`LatencyConnector`** — pulls src→dst per-pair latency samples (`interaction_latency_samples`). Raw only; the engine does α/i smoothing so it stays re-tunable.
- **`DiscoverConnector`** — reads k8s API for namespaces, workloads (with `Kind` so DaemonSets can be excluded), pods (with `nodeName`).

**Steps** (`internal/steps/`) wire connectors to the store — `metrics`,
`interactions`, `latency`, `discover`. Each step is independent; a failing
step doesn't abort the others.

**Runtime shape**:
- **CronJob** — scheduled ingest.
- **Long-running trigger service** — `collector serve`, listens on `:8081`; the engine's `POST /collections` reaches it for on-demand ingest.
- **CLI** — `collector discover`, `collector db migrate`, `collector connectors list`.

### Engine (`engine/`, Python + FastAPI)

**Job:** analyse. Read from the state DB, produce recommendations, write them back.

**Two layers**:

- **`analysis_core/`** — shared utilities: state-DB access (`io/statestore.py`), interaction graph helpers, prepare/probe/smooth primitives. Reusable across recommender heads.
- **`recommenders/latency/`** — the latency head: four pure-function stages the runner glues together.

The four stages are **pure functions** over data — no I/O in any of them, so
every stage is unit-tested against fixtures in `engine/synth/`:

| Stage | File | What it does |
|---|---|---|
| Smooth | `smooth.py` | `α·latest + (1−α)·mean(last i)` per pair. `pick_alpha` optional. |
| Weigh | `weigh.py` | For each pair `(x,y)` with smoothed latency `∂` and placement `x@i / y@j`: accumulate `W_ij = mean(∂ across all pairs crossing i,j)`. DaemonSet edges dropped when `exclude_daemonset_edges=True`. Returns a NetworkX graph + no-data-node list. |
| Group | `group.py` | Greedy growth from each start node (grow → repeat sequentially → collect all groups); pick the **exact cover** minimizing `Σ latency_ratio(group)`. Greedy fallback available via `cover_mode='greedy'`. |
| Whatif | `whatif.py` | Per candidate workload (DaemonSets excluded): replicate-per-group is the default; migrate when cross-group volume is small AND latency doesn't worsen AND (if a `cost_provider` is set) cost drops. Emits `latency_delta_ms`, `cost_saving`, and a copy-pasteable `nodeAffinity` YAML snippet. |

**FastAPI (`api/`)** — the `/api/v1` surface. `POST /runs` dispatches on
`run_type` (currently just `"latency"`, structured for future heads). The
four latency-specific reads (`groups`, `graph`, `recommendations`,
`evidence`) return the exact DTOs the UI renders.

**Runner (`recommenders/latency/runner.py`)** — the only place stages meet
I/O. Reads collected data, orchestrates the pipeline, persists
`latency_groups`, `latency_group_workloads`, `latency_edges`,
`latency_recommendations`, and per-rec `latency_evidence` + `latency_peers`.

### UI (`ui/`, static HTML + vanilla JS)

**Job:** wizard + visualization. No build step, no framework, no external
libraries. Nginx serves the static bundle and proxies `/api/` to the engine
(single-origin, no CORS).

**Four screens**:

1. **Connect cluster** — grid of cluster cards; add-cluster modal with four
   auth tabs (kubeconfig / SA token / client cert / basic).
2. **Select pods** — namespace tree from `disc_workloads`. DaemonSets are
   disabled + labelled "auto-excluded" (they already run on every node).
3. **Sources & run** — source cards (Hubble / Istio / OTel / Prometheus
   mesh), α slider, `i` + unit selector, migration + cost toggles, cover mode.
4. **Results** — the **orb view** (island → node → click drill-down to
   workload list) + wide **recommendation rows** + a **Why force-graph modal**
   with the copy-pasteable snippet. **APPLY/DROP are strictly client-side**
   preview — they mutate JS state only.

The orb and force-graph are self-contained SVG: a small force relaxation for
circle packing, no D3. Works offline / air-gapped.

## The state-DB contract

The database is the only cross-module contract. Four tiers, per the schema
migrations in `collector/internal/store/migrations/{sqlite,postgres}/`:

### Tier 1 — Config (persistent)

`clusters`, `data_sources`, `settings`. Written via the API when the user adds
clusters / sources; never expires.

### Tier 2 — Discovery cache (short TTL, refreshable)

`disc_namespaces`, `disc_workloads` (with `kind`), `disc_pods` (with `node_name`).
Populated by the collector's discover step. Default TTL 10 minutes; the engine
uses it to serve `/clusters/{id}/namespaces` and `/clusters/{id}/namespaces/{ns}/workloads`.

### Tier 3 — Collected data (1-day TTL)

`metric_samples`, `interactions`, `interaction_latency_samples`, `collection_runs`.
Written by the collector's steps. The engine reads it to run the pipeline.

### Tier 4 — Analysis results (1-day TTL)

`analysis_runs` (polymorphic on `run_type`), plus the latency-specific
result tables: `latency_groups`, `latency_group_workloads`, `latency_edges`,
`latency_recommendations`, `latency_evidence`, `latency_peers`. Written by
the engine's runner; read by the API to serve `/runs/{id}/*` endpoints.

Full schema: [`collector/internal/store/migrations/sqlite/0001_init.sql`](../collector/internal/store/migrations/sqlite/0001_init.sql) + `0002_latency.sql`. The Postgres migrations are the same logical schema in Postgres syntax.

## Data flow, end to end

1. **Discovery** — collector queries the k8s API → writes `disc_namespaces`, `disc_workloads` (with `kind`), `disc_pods` (with `nodeName`) to the state DB.
2. **Collection** — collector queries Prometheus/Hubble for interactions + latency samples → writes `interactions` + `interaction_latency_samples`.
3. **Run** — engine's runner:
   - Reads pair latency series from `interaction_latency_samples`.
   - Reads placement from `disc_pods` × `disc_workloads`.
   - Reads DaemonSet UIDs (workloads where `kind='DaemonSet'`).
   - Executes smooth → weigh → group → whatif.
   - Writes `latency_groups` + `latency_group_workloads` + `latency_edges` + `latency_recommendations` + `latency_evidence` + `latency_peers`.
4. **UI render** — engine's API reads Tier-4 tables and returns DTOs. The UI renders the orb from `/groups`, cards from `/recommendations`, force-graph from `/evidence`.

## Adding a second recommender head

`analysis_runs.run_type` is the polymorphic discriminator. The dispatch is in
`engine/runner.py`:

```python
def run_analysis(store, *, cluster, ..., run_type="latency", **kwargs):
    if run_type == "latency":
        return run_latency_analysis(store, cluster=cluster, ..., **kwargs)
    raise ValueError(f"unknown run_type: {run_type!r}")
```

To add e.g. a `qos` head:

1. Add a new schema migration `0003_qos.sql` with its result tables.
2. Add `recommenders/qos/` with pure-function stages (`prepare/rank/label`).
3. Add `runner.py::run_qos_analysis` that reads Tier-3 and writes Tier-4-QoS.
4. Wire it into the dispatch in `engine/runner.py`.
5. Add a `POST /runs {run_type:"qos", ...}` test; add read-side DTOs; add UI mode toggle.

The `analysis_core` primitives (interaction-graph traversal, series prepare,
seasonality/aggregate helpers) are reusable — that's the whole point of the
split.

## Determinism and testability

- **Every algorithm stage is a pure function.** No wall-clock, no `random` calls (except deterministically for run-name slugs), no environment lookups.
- **Synthetic fixtures cover every code path.** `engine/synth/` provides `fig2_cluster` (the disclosure's canonical 8-node cover), `sparse_migration_cluster` (3 clean islands + 1 low-volume straddler → triggers a migrate rec), and `no_data_cluster` (isolated node → low-confidence replicate).
- **46 unit + contract tests on the engine**, **6 on the collector**. Every DTO shape is asserted against docs/04 §E. Every stage is asserted against known-answer fixtures. The Fig.2 known-answer test locks in the exact 5-group cover at total ratio ≈ 0.35.

## References

- **`design-docs/01-understanding.md`** — the problem statement + exact algorithm (the anchor).
- **`design-docs/03-collector-design.md`** — Module 1 (collector) design notes.
- **`design-docs/05-engine-design.md`** — Module 2 (engine) design notes.
- **`design-docs/07-ui-design.md`** — Module 3 (UI) design notes.
- **`design-docs/04-schema-and-api.md`** — the cross-module contract in full detail.
