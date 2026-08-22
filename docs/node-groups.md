# Node groups — the algorithm

This doc explains the four-stage engine in one place — how each stage
works, why it's shaped the way it is, and how it produces the two things
users see: the **island orb** (showback) and the **replicate/migrate
recommendations** (actionable output).

If you want the code, it's in `engine/engine/recommenders/latency/`. The
four stages there — `smooth.py`, `weigh.py`, `group.py`, `whatif.py` — are
pure functions; every rule below maps to a specific one.

The mechanic is inspired by the KubeCon NA 2023 *"Huddle — Insightful latency
optimizer for complex and sparse app flows"* talk (Yash Bhatnagar), and by
the VMware invention disclosure that preceded it. The engine here is the
open-source reimplementation of that idea on a CNCF stack (Cilium/Hubble as
the default latency source, OpenCost for the cost side, NetworkX for graph
math, everything read-only).

## Inputs

For each cluster, the engine reads:

- **`interaction_latency_samples`** — raw `(src_uid, dst_uid, ts, latency_ms)` rows the collector writes. Raw so α + `i` stay re-tunable per run.
- **`interactions.avg_count`** — per-pair call volume (calls/hr).
- **`disc_pods`** joined to **`disc_workloads`** — resolves `workload_uid → node_name`.
- **`disc_workloads.kind`** — flags which UIDs are DaemonSets (excluded from candidacy).

## Stage 1 — Smooth (`smooth.py`)

For each pair `(src, dst)`, collapse its ordered latency series to a single
smoothed value:

```
smoothed = α · latest + (1 − α) · mean(last i values before latest)
```

Guards:

- Empty series → `0.0`.
- Single point → that point.
- Fewer than `i` prior points → mean of what exists.

**Config knobs**:
- `alpha` — float in `[0,1]`, or the string `"auto"` to derive it from the series' coefficient of variation (`pick_alpha`: steady series → higher α, noisy series → lower α, clamped to `[0.2, 0.8]`).
- `i_window` — moving-average length.
- `i_unit` ∈ `samples | minutes | hours | days` — time units are converted to sample counts using the collection step (`i_window_from_unit`).

Pure function; no I/O.

## Stage 2 — Weigh (`weigh.py`)

Turn per-pair smoothed latencies into an undirected node graph:

```
W_ij = mean( ∂_xy across all pairs (x, y) with x@i, y@j and i ≠ j )
```

- **Same-node pairs** are skipped (they contribute nothing to cross-node latency).
- **Placement lookups** that miss (workload UIDs not in `placement`) are skipped.
- **DaemonSet-touching pairs** are skipped when `exclude_daemonset_edges=True` (the default). DaemonSets already run on every node — their "cross-node" calls don't represent anything worth optimizing.

Returns:
- A NetworkX `Graph` with edge attributes `weight_ms` (float) and `count` (int) per node-pair.
- A tuple of `no_data_nodes` — nodes that host workloads but have no cross-node interactions. Downstream stages don't add these to the graph; the runner emits an "insufficient data — replicate here" rec for their workloads.

Pure function.

## Stage 3 — Group (`group.py`) — the invention core

The disclosure's core claim is that you can partition nodes into
"low-latency islands" from just this weighted graph, without knowing anything
about your topology labels or your network.

Three steps:

### 3a. Grow candidate groups

For each start node, sequentially cover the graph:

1. `group = {start}`, `max_edge = −∞`.
2. Repeatedly consider edges from the group to unvisited neighbours.
3. Admit the neighbour reachable by the **least** edge weight (constraint a).
4. **Only if** that edge weight is strictly less than `max_edge` (constraint b), except when the group is a singleton — the first edge is always admissible.
5. Update `max_edge = max(max_edge, admitted_weight)`.
6. Stop when no neighbour qualifies. Continue with a fresh `grow_group` from the next unvisited node in sorted order.

Each start node produces one full **sequential cover** of the graph. `candidate_groups(g)` is the union of every group that appears in any of those covers, deduplicated as frozensets.

### 3b. Score every candidate

```
latency_ratio(g, group) = Σ (internal edge weights) / Σ (external edge weights)
```

- **Internal** = both endpoints inside the group.
- **External** = exactly one endpoint inside the group.
- Singleton or zero-external → `0.0`.

Lower is better: low internal latency + high external latency = a clean island (tight inside, isolated outside).

### 3c. Pick the minimum-ratio exact cover

Choose a set of candidates such that **every node appears in exactly one group**, minimizing the **sum** of the groups' ratios.

- **`cover_mode='exact'`** (default) — backtracking with branch-and-bound pruning. Ordered by rounded totals so ties can be broken. Feasibility is guaranteed by singletons emerging naturally from step 3a (any node the sequential cover leaves alone becomes a singleton in its cover).
- **`cover_mode='greedy'`** — repeatedly pick the still-fitting candidate with the lowest ratio-per-new-node score, break ties by size (larger wins → fewer groups). O(|cand| · |nodes|). Not guaranteed optimal; use for large graphs where the exact solver blows up.
- **Tie-break**: after rounding total ratios to `round_sig_digits` (default 2), prefer covers with **fewer groups**.

### The disclosure's Fig.2 known-answer

The canonical test case: 8 nodes, 11 edges. Applying the formula above, the
minimizing cover is the **5-group** `(N1,N3)(N2,N4)(N5,N6)(N7)(N8)` at total
`1/9 + 5/27 + 1/20 + 0 + 0 ≈ 0.35`. Locked in as the primary unit test in
`engine/tests/test_latency_group.py`.

## Stage 4 — What-if (`whatif.py`)

For each candidate workload (DaemonSets excluded when `exclude_daemonsets=True`):

### 4a. Account for the workload's interaction pattern

Sum call volume by peer group; total the "network time" as `Σ (volume × latency)` over all peer pairs. The result is a `vols_by_group[gi] → volume` map plus a scalar `latency_before`.

### 4b. Try migration

If `enable_migration=True` and the workload has interactions:

1. Find the **dominant** peer group (highest volume). Skip if it's the workload's own group.
2. Compute **cross-group volume** = `total_volume − dominant_volume`. If this exceeds `migration_min_volume` (default 5), the workload has too much cross-group traffic to migrate safely — leave it as a `replicate`.
3. Compute `latency_after`: for each peer pair, if the peer's group == dominant group, that call becomes intra-group (0 ms in the approximation); otherwise it keeps its current latency.
4. Compute `latency_delta_ms = latency_after − latency_before` — must be `≤ migration_cost_weight` (default 1.0) for the migration to be considered.
5. If a `cost_provider` is plumbed in and `cost_enabled=True`, compute
   `cost_before = per_instance × (# groups with volume) `,
   `cost_after = per_instance × 1`. Skip the migration if `cost_before ≤ cost_after`.

If all checks pass → emit `action='migrate'` with `latency_delta_ms`, `cost_saving_amount`, `to_group_index=dominant`, plus the target island's nodes and a copy-pasteable YAML snippet.

### 4c. Default: replicate per group

Otherwise → emit `action='replicate'`, `to_scope='all_groups'`, `confidence='medium'`
(`'low'` when the workload has no home group data). Summary lists the detected
groups so the user can decide where to add replicas.

### 4d. No-data node handling

Workloads placed on nodes with no cross-node interactions → `action='replicate'`, `confidence='low'`, `summary='insufficient interaction data on node <n> — replicate here.'`

## The nodeAffinity snippet

For a migrate rec, the engine emits a copy-pasteable YAML fragment keyed to
the target island's nodes. The default mechanism is **`preferredDuringSchedulingIgnoredDuringExecution`** so scheduling stays soft (a node draining doesn't hard-fail the pod), keyed on `kubernetes.io/hostname` so it works on any cluster without extra labelling:

```yaml
affinity:
  nodeAffinity:
    preferredDuringSchedulingIgnoredDuringExecution:
      - weight: 100
        preference:
          matchExpressions:
            - key: kubernetes.io/hostname
              operator: In
              values:
                - node-a
                - node-b
```

Alternative mechanisms available via `LatencyConfig.migration_mechanism`:

- **`node_selector`** — emits guidance to label the target island's nodes with a shared key, then use `nodeSelector`. Cleaner in production if you already have a stable island label.
- **`topology_spread`** — emits a `topologySpreadConstraints` block keyed on a topology label. Good fit if you've labelled nodes by zone/rack already.

The tool never applies the snippet — that's on you.

## Persistence

The runner writes:

- `latency_groups` — one row per detected island, with `group_index`, `node_names[]`, `latency_ratio`, `workload_count`.
- `latency_group_workloads` — one row per workload placed in each group's nodes.
- `latency_edges` — one row per graph edge, with `kind='internal'|'external'` and `group_id` for internal edges.
- `latency_recommendations` — one row per candidate workload's decision.
- `latency_evidence` — per rec, the subgraph fragment (home node + peer nodes + edges between them) and volume/latency before/after detail. Enough for the UI's Why force-graph without loading the full graph per call.
- `latency_peers` — per rec, the peer rows with `avg_count` + `latency_ms`.

## What the UI does with all this

- **Orb** — reads `/runs/{id}/groups`. Each `LatencyGroupDTO` becomes one big translucent circle; the workloads inside it are grouped by `node_name` into mini-orbs sized by total traffic.
- **Recommendation rows** — one per `LatencyRecommendationDTO`. Chips show `from → to` group colors, badges show `cost_saving`, `latency_delta_ms`, `confidence`, `suggested_mechanism`.
- **Why modal** — reads `/runs/{id}/recommendations/{recId}/evidence` and renders `subgraph` as a force-graph. External edges are drawn dashed red — those are the latency barriers. Peers are listed with volume + latency badges. The snippet has a **Copy** button.
- **APPLY / DROP** — strictly client-side. APPLY moves the workload's mini-orb into the target island's first node in `state.view`; DROP hides the row + reverses the APPLY. No server writes.

## Determinism + testing

Every stage is a pure function; the tests hit them individually + at the whole-runner level:

- `test_latency_smooth_weigh.py` — α extremes, short series, `i_window_from_unit`, DaemonSet edge exclusion.
- `test_latency_group.py` — `grow_group` constraints; `latency_ratio` matches the disclosure's table values; `min_ratio_cover` reproduces the Fig.2 5-group cover at 0.35.
- `test_latency_cover.py` — genuine tie-break graph exercises `fewer_groups`; greedy cover reproduces the exact answer on Fig.2.
- `test_latency_whatif.py` — migrate emission on the sparse fixture; DaemonSets excluded; cost_saving computed only when a provider is set.
- `test_latency_runner.py` — end-to-end: sparse fixture produces a migrate rec for `C` with the correct snippet; no-data fixture flags the lonely node; DaemonSet-workload gets no recommendation.
- `test_api_latency.py` — contract tests for `LatencyGroupDTO`, `GraphDTO`, `LatencyRecommendationDTO`, `EvidenceDTO`.

Run them all with `cd engine && ./.venv/bin/pytest`. Expect **46 tests, all passing.**
