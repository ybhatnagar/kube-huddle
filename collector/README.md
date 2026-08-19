# Collector (Go)

Pulls per-workload CPU/memory metrics and inter-workload interaction counts + latency
from Prometheus / Cilium Hubble (or any source you add) and writes them into the shared
state database. The engine reads that database independently — the collector and engine
never call each other.

## Build & test
```bash
go build -o bin/collector ./cmd/collector
go test ./...
```
Uses a pure-Go SQLite driver (no cgo), so the binary is small and static. Postgres is
supported via `pgx`. The unit tests cover the SQLite path and Prometheus response
parsing; the SQL is shared across both databases behind one interface.

## Commands

```bash
# Create / verify the database schema
./bin/collector db migrate --db-dsn ./state.db

# List the metric sources that are registered
./bin/collector connectors list            # -> prometheus

# Pull metrics + interactions from Prometheus/Hubble into the DB
./bin/collector ingest --metrics --interactions \
    --prom-url http://prometheus.monitoring:9090 \
    --namespace my-namespace \        # omit for all namespaces; comma-separate for several
    --resources cpu,memory \
    --since 14d --step 1h \            # look back 14 days at 1-hour resolution
    --cluster prod \
    --db-dsn ./state.db

# On-demand trigger service: POST /ingest collects now (called by the engine's
# POST /collections). Runs the same ingestion as the CLI; add the same db/prom flags.
./bin/collector serve --addr :8081 --db-dsn ./state.db --prom-url http://prometheus.monitoring:9090
```

Typical production setup: run `ingest` on a schedule (e.g. a Kubernetes CronJob) so the
engine always has recent data to analyze.

## Configuration

Every setting can come from a flag, an environment variable, or a JSON config file, in
that order of precedence (**flag > env > file > built-in default**):

| Flag | Env var | Default |
|---|---|---|
| `--db-driver` | `KUBEHUDDLE_DB_DRIVER` | `sqlite` (or `postgres`) |
| `--db-dsn` | `KUBEHUDDLE_DB_DSN` | `./kubehuddle.db` |
| `--prom-url` | `KUBEHUDDLE_PROM_URL` | `http://prometheus.monitoring:9090` |
| `--prom-bearer` | `KUBEHUDDLE_PROM_BEARER` | — |
| `--config` | — | path to a JSON file keyed by flag name |

The endpoint and auth are always configurable, so the same binary works whether it runs
inside the cluster (default in-cluster service URL) or from your laptop.

## Adding a metrics source

A connector is one implementation of the `MetricsConnector` interface (fetch series,
normalize, health-check). The Prometheus connector under
`internal/connectors/prometheus/` is the reference — copy it, register your connector in
an `init()`, and it becomes available as `--source <name>`. No changes to the core.

Five resources are supported out of the box — `cpu`, `memory`, `net_tx`, `net_rx`,
`ephemeral_storage` — selected with `--resources`. The PromQL is fully configurable
because the right query depends on your metrics setup: the defaults expect the common
kube-state-metrics labels (`namespace`, `workload`, `workload_type`); override any
resource's query with the config-file key `query_<resource>` (e.g. `query_cpu`,
`query_net_tx`). Counters (CPU seconds, network bytes) are converted to a rate at
ingestion; gauges (memory, disk bytes) are stored as-is.

## Layout
```
cmd/collector/            CLI + the ingest orchestrator
internal/connectors/      the MetricsConnector interface, a registry, and prometheus/
internal/steps/           ingestion steps (metrics, interactions)
internal/store/           database access (SQLite / Postgres) + the schema migrations
internal/server/          on-demand trigger service (POST /ingest)
internal/ingest/          collection orchestration shared by the CLI and the service
internal/config/          flag > env > file settings resolver
```
