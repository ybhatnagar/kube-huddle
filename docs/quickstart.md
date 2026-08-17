# Quickstart — Kube Huddle in 5 minutes

You'll go from zero to a live UI showing detected node groups + migrate/replicate
recommendations, without a Kubernetes cluster, without Prometheus, without Hubble.
The engine ships a synthetic-cluster generator that reproduces the invention
disclosure's canonical example (3 islands, 5 nodes, one hot cross-island app).

## Prerequisites

- **Python ≥ 3.9** on your PATH. That's it for the quickstart.
- **Go ≥ 1.23** if you want to also build the collector (not required for this walkthrough — we bypass the collector by seeding the state DB directly).

## 1. Install the engine

```bash
git clone https://github.com/kube-huddle/kube-huddle.git
cd kube-huddle/engine
python3 -m venv .venv
./.venv/bin/pip install -e ".[dev]"
```

Verify:

```bash
./.venv/bin/kubehuddle-engine --help
# usage: engine [-h] {serve,init-db,seed,run} ...
```

## 2. Seed a synthetic cluster

```bash
./.venv/bin/kubehuddle-engine init-db --db-dsn ./demo.db
./.venv/bin/kubehuddle-engine seed    --db-dsn ./demo.db --fixture sparse_migration
```

The `sparse_migration` fixture writes 5 nodes (`N1`..`N5`), 5 Deployments
(`A1`, `A2`, `B1`, `B2`, `C`), and 6 interaction pairs into `demo.db` (SQLite).
The layout: two tight islands `{N1,N2}` and `{N3,N4}` with hot intra-island
traffic; app `C` on `N5` making ~100 calls/hr to `A1` (island 1) and 2 calls/hr
to `B1` (island 2). Perfect setup for a migrate recommendation.

Other fixtures: `--fixture fig2` (the disclosure's 8-node canonical graph),
`--fixture no_data` (isolated node → low-confidence "replicate here").

## 3. Run the pipeline

```bash
./.venv/bin/kubehuddle-engine run --db-dsn ./demo.db --cluster sparse_migration
```

Expected:

```
run bright-raven-9661 (id=1): completed
  groups=3 recommendations=5

cover:
  (N3,N4)  ratio=0.0154
  (N1,N2)  ratio=0.0182
  (N5)     ratio=0.0000
total ratio = 0.0336
```

Three islands, one recommendation per workload. The pipeline stages that ran:

1. **Smooth** — for each pair with a latency series, produce a single smoothed value.
2. **Weigh** — average pair latencies onto node-graph edges (DaemonSet edges excluded).
3. **Group** — grow candidate groups; pick the exact cover minimizing Σ ratios.
4. **Whatif** — for each non-DaemonSet workload, replicate-per-group or migrate-into-dominant.

## 4. Serve the UI

```bash
KUBEHUDDLE_UI_DIR=../ui KUBEHUDDLE_DB_DSN=./demo.db \
    ./.venv/bin/python -m uvicorn engine.api.app:app --port 8000
```

Open **http://localhost:8000/** and walk the four screens:

1. **Connect cluster** — you'll see `sparse_migration` in the grid. Click **Select →**.
2. **Select pods** — 5 Deployments show up. All checked by default (DaemonSets would be disabled + labelled). Click **Continue →**.
3. **Sources & run** — accept defaults (Hubble, α=0.5, i=10 samples, migration + cost on). Click **Collect & detect groups**.
4. **Results** — see the 3-island orb + 5 recommendation rows.

The migrate rec for `C` should read `N5 → N1` with a **Cost saving ↓** and **Latency ↓ 1500 ms** badges. Click **WHY?** to see the force-graph — 3 nodes coloured per island, all edges dashed red (external = barriers) — plus the ready-to-paste **nodeAffinity** YAML snippet.

## 5. Poke the API

Everything the UI does is a plain HTTP call. Try:

```bash
curl -s http://localhost:8000/api/v1/runs/1/groups | python -m json.tool | head -30
```

```bash
curl -s http://localhost:8000/api/v1/runs/1/recommendations \
  | python -c "import json,sys; recs=json.load(sys.stdin)['recommendations']; \
               print(json.dumps([r for r in recs if r['action']=='migrate'], indent=2))"
```

You'll get the migrate rec with `suggested_mechanism="node_affinity"` and the full YAML snippet in `suggested_snippet`. Full reference: [**docs/api.md**](api.md).

## What's next

- **Deploy on a real cluster** with Hubble installed → [docs/deployment.md](deployment.md).
- **Understand the algorithm** (edge weights, latency ratio, the exact-cover trade-off) → [docs/node-groups.md](node-groups.md).
- **Understand the modules** (collector / engine / UI / state-DB contract) → [docs/architecture.md](architecture.md).

## Troubleshooting

- **`kubehuddle-engine: command not found`** — either the venv isn't active or the entry point script isn't on PATH. Prefix commands with `./.venv/bin/`.
- **`ModuleNotFoundError: engine`** — you're not in `kube-huddle/engine/`. `cd` there.
- **UI at :8000 shows "API unreachable"** — the process serves both UI + API; if you skipped `KUBEHUDDLE_UI_DIR=../ui`, uvicorn is serving only the API. Set the env var and restart.
- **`run` says 0 groups + 0 recommendations** — the seed step didn't run against the same `db-dsn` as the run step. Verify both point at `./demo.db`.

## Cleanup

```bash
rm -f demo.db
deactivate     # if the venv is active
```
