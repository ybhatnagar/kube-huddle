# REST API reference

Base path: `/api/v1`. The engine serves everything here. The UI's nginx
container proxies `/api/*` to the engine so the browser sees a single origin;
in standalone dev mode you point the browser at the engine directly (default
`http://localhost:8000/`).

All request + response bodies are JSON. Timestamps are ISO-8601 UTC.
IDs are stringified integers for the wire; the state DB stores them as native
integers.

## Health

### `GET /healthz`

Liveness probe. Always returns `{"status":"ok"}`.

## Clusters

### `POST /clusters` → `ClusterDTO`

Register a cluster. Credentials are **references** to k8s Secrets in the engine's
own namespace — never plaintext values.

Request:
```json
{
  "name": "prod",
  "api_url": "https://kubernetes.default.svc",
  "auth_method": "token",
  "credential_ref": "sa-token-prod",
  "ca_cert": null
}
```

Response `201`:
```json
{
  "id": "1",
  "name": "prod",
  "api_url": "https://kubernetes.default.svc",
  "auth_method": "token",
  "status": "unknown",
  "created_at": "2026-01-15T09:42:11Z",
  "last_connected_at": null
}
```

Errors: `409 Conflict` if the name is taken.

### `GET /clusters` → `{clusters: ClusterDTO[]}`

List all registered clusters.

### `GET /clusters/{id}` → `ClusterDTO`

Get one. `404` on unknown id.

### `DELETE /clusters/{id}` → `{deleted: true}`

Remove a cluster and cascade-delete its collections + runs.

### `POST /clusters:test` → live probe result

Live connectivity probe against the fields in the request body — **does not
persist a cluster**. Requires at least `api_url` or `credential_ref`.

Request:
```json
{
  "api_url": "https://kubernetes.default.svc",
  "auth_method": "token",
  "credential_ref": "sa-token-prod"
}
```

Response `200`:
```json
{"reachable": true, "server": "https://kubernetes.default.svc", "server_version": "v1.29.3"}
```

Or:
```json
{"reachable": false, "detail": "reached API server (HTTP 401); credentials may lack read access"}
```

The engine resolves the credential based on `auth_method`:

| `auth_method` | Secret key it looks for |
|---|---|
| `token` | `token` (bearer) |
| `basic` | `username` + `password` |
| `client_cert` | `tls.crt` + `tls.key` |
| `kubeconfig` | `kubeconfig` (or `config`) — full YAML |

### `POST /clusters/{id}:test` → live probe of a saved cluster

Same shape as `POST /clusters:test`, but uses the cluster's stored
`api_url` / `auth_method` / `credential_ref` fields. Updates the cluster's
`status` column (`reachable`/`unreachable`) and `last_connected_at` on success.

## Discovery (from cache)

### `GET /clusters/{id}/namespaces?refresh=false` → `{namespaces: [...]}`

List namespaces the collector has discovered for this cluster. `refresh=true`
returns `501` today — live discovery is not yet wired.

### `GET /clusters/{id}/namespaces/{namespace}/workloads?refresh=false` → `{workloads: WorkloadDTO[]}`

List workloads in one namespace. `WorkloadDTO` shape:
```json
{
  "workload_uid": "team/Deployment/api",
  "namespace": "team",
  "kind": "Deployment",
  "name": "api",
  "replicas": 3,
  "requests_cpu_m": 250,
  "requests_mem_bytes": 67108864
}
```

## Data sources

### `POST /clusters/{id}/data_sources` → `DataSourceDTO`

Register a per-cluster source.

Request:
```json
{
  "type": "prometheus",
  "name": "prom-main",
  "endpoint": "http://prometheus.monitoring:9090",
  "enabled": true,
  "auth_config": null,
  "settings": null
}
```

`type` is one of `prometheus | opencost | mesh | custom_api | file | interactions`.

### `GET /clusters/{id}/data_sources` → `{sources: DataSourceDTO[]}`

### `PUT /data_sources/{id}` → `DataSourceDTO`

Update fields. Body is partial (any of `name`, `type`, `endpoint`, `enabled`, `auth_config`, `settings`).

### `DELETE /data_sources/{id}` → `{deleted: true}`

### `POST /data_sources/{id}:test` → health result

Probes the source and updates its `health` column.

## Settings

### `GET /settings` → `SettingsDTO`

Returns global defaults:
```json
{
  "metric_ttl_hours": 24,
  "discovery_ttl_min": 10,
  "result_ttl_hours": 24,
  "default_resources": "cpu,memory",
  "default_window": "7d",
  "thresholds": {"seasonality_gain": 0.30, "band": 0.10, "jump_min": 50}
}
```

### `PUT /settings` → `SettingsDTO`

Partial update; any of the above keys.

## Collection (Tier 3 population)

### `POST /collections` → `{collection_id, status}`

Ask the collector's trigger service to ingest **now**. Non-fatal if the
collector is down (the engine falls back to stored data + flags the run stale).

Request:
```json
{
  "cluster_id": 1,
  "scope": "all",
  "resources": ["cpu", "memory"],
  "window": "7d",
  "interaction_source": "hubble"
}
```

Response `200`:
```json
{"collection_id": "42", "status": "running"}
```

Or `503 Service Unavailable` if the collector trigger service is unreachable.

### `GET /collections/{id}` → `CollectionDTO`

Poll for terminal status.

```json
{
  "id": "42",
  "status": "success",
  "progress": 100,
  "data_as_of": "2026-01-15T09:41:56Z",
  "rows_written": 8371,
  "error": null
}
```

`status` ∈ `pending | running | success | failed | partial`.

## Runs (the pipeline)

### `POST /runs` → `RunResultDTO`

Kick off an analysis run.

Request:
```json
{
  "cluster_id": 1,
  "scope": "all",
  "config": {
    "alpha": 0.5,
    "i_window": 10,
    "i_unit": "samples",
    "cover_mode": "exact",
    "enable_migration": true,
    "cost_enabled": true,
    "group_by": "node"
  },
  "ttl": "24h",
  "run_type": "latency"
}
```

Response `200`:
```json
{
  "run_id": "17",
  "name": "bright-raven-9661",
  "status": "completed",
  "groups": 3,
  "recommendations": 5,
  "data_as_of": "2026-01-15T09:41:56Z",
  "stale": false
}
```

Unknown `run_type` → `400 Bad Request` with `detail: "unknown run_type: 'xyz'"`.

### `GET /runs?cluster_id=1&limit=50` → `{runs: RunSummaryDTO[]}`

Run history. Each entry:
```json
{
  "id": "17",
  "name": "bright-raven-9661",
  "cluster_id": 1,
  "run_type": "latency",
  "status": "completed",
  "stale": false,
  "data_as_of": "2026-01-15T09:41:56Z",
  "created_at": "2026-01-15T09:42:00Z",
  "completed_at": "2026-01-15T09:42:03Z"
}
```

### `GET /runs/{id}` → run status

Same shape as an entry in `/runs`.

## Latency-run payloads

The DTO shapes below are the cross-module contract. The engine's contract tests
(`engine/tests/test_api_latency.py`) assert every field, so PRs that reshape a
DTO have to update both the builder in `engine/engine/api/dto.py` and the docs
here.

### `GET /runs/{id}/groups` → `{groups: LatencyGroupDTO[]}`

The detected islands. Feeds the UI's orb.

```json
{
  "group_id": "1",
  "group_index": 0,
  "label": "N3, N4",
  "node_names": ["N3", "N4"],
  "latency_ratio": 0.0154,
  "workload_count": 2,
  "total_size": 0.0,
  "color_hint": null,
  "workloads": [
    {
      "workload_uid": "default/Deployment/B1",
      "name": "B1",
      "namespace": "default",
      "kind": "Deployment",
      "node_name": "N3",
      "size": 0.0,
      "size_metric": "interaction_volume"
    }
  ]
}
```

`404 Not Found` if the run doesn't exist.

### `GET /runs/{id}/graph` → `GraphDTO`

The weighing-engine graph — nodes + edges. Nodes are derived from
`latency_groups.node_names` + edge endpoints (no-data nodes drop off).

```json
{
  "nodes": [
    {"id": "N1", "group_index": 2},
    {"id": "N3", "group_index": 0},
    {"id": "N5", "group_index": 1}
  ],
  "edges": [
    {"a": "N1", "b": "N3", "latency_ms": 20.0, "count": 1, "kind": "external"},
    {"a": "N1", "b": "N5", "latency_ms": 15.0, "count": 1, "kind": "external"}
  ]
}
```

`kind` ∈ `internal | external` — external edges are the latency barriers.

### `GET /runs/{id}/recommendations` → `{recommendations: LatencyRecommendationDTO[]}`

```json
{
  "recommendation_id": "5",
  "workload": {"kind": "Deployment", "name": "C", "namespace": "default"},
  "workload_uid": "default/Deployment/C",
  "action": "migrate",
  "from": {"scope": "N5", "label": "N5", "group_index": 1},
  "to":   {"scope": "N1, N2", "label": "N1, N2", "group_index": 2},
  "cost_saving": null,
  "latency_delta_ms": -1500.0,
  "suggested_mechanism": "node_affinity",
  "suggested_snippet": "affinity:\n  nodeAffinity:\n    ...",
  "confidence": "medium",
  "summary": "Migrate into group 2 (N1, N2): low cross-group volume (2.0/hr), net Δlatency×vol -1500.0 ms·calls."
}
```

`action` ∈ `migrate | replicate`.

`from` and `to` are `{scope, label, group_index}` triples or `null`.

`cost_saving` is `{amount, currency, period}` or `null` (null when the runner had no `cost_provider`).

`latency_delta_ms` is negative when the migration reduces total network time.

`suggested_mechanism` ∈ `node_affinity | node_selector | topology_spread`. `node_affinity` is the default; the snippet is copy-pasteable YAML the **user** applies.

`confidence` ∈ `high | medium | low`.

### `GET /runs/{id}/recommendations/{recId}/evidence` → `EvidenceDTO`

The "why" behind one recommendation. Contains a subgraph fragment + before/after detail + interacting peers.

```json
{
  "recommendation_id": "5",
  "subgraph": {
    "nodes": [{"id": "N1", "group_index": 2}, {"id": "N5", "group_index": 1}],
    "edges": [{"a": "N1", "b": "N5", "latency_ms": 15.0, "count": 1, "kind": "external"}]
  },
  "detail": {
    "volume_before": 102.0,
    "volume_after": 102.0,
    "latency_before": 1550.0,
    "latency_after": 50.0
  },
  "peers": [
    {"peer_workload": "default/Deployment/A1", "peer_workload_uid": "default/Deployment/A1", "relation": "peer", "avg_count": 100.0, "latency_ms": 15.0},
    {"peer_workload": "default/Deployment/B1", "peer_workload_uid": "default/Deployment/B1", "relation": "peer", "avg_count": 2.0,   "latency_ms": 25.0}
  ]
}
```

`404 Not Found` if the recommendation doesn't exist.

## Errors

Every endpoint returns a JSON body on non-2xx:

```json
{"detail": "cluster not found"}
```

Common status codes:

| Status | When |
|---|---|
| `400 Bad Request` | Unknown `run_type`; malformed body. |
| `404 Not Found` | Unknown cluster / source / run / recommendation. |
| `409 Conflict` | Duplicate cluster name. |
| `500 Internal Server Error` | Unexpected engine failure (check pod logs). |
| `501 Not Implemented` | `refresh=true` on discovery endpoints. |
| `503 Service Unavailable` | Collector trigger service unreachable from `POST /collections`. |

## CORS

Set via `KUBEHUDDLE_CORS_ORIGINS` env var (comma-separated). Default `*` — lock down in production. The chart's `engine.corsOrigins` value passes through.

## Rate limiting / auth

Not built. Kube Huddle assumes it's deployed behind a mesh, ingress, or gateway
that handles both. All endpoints are anonymous by design; the intended flow is
that the ingress does authn (OIDC, mTLS, or a bearer token upstream). The
engine itself has no notion of users.

## OpenAPI schema

The engine is a FastAPI app; a machine-readable schema is available at:

```
GET /openapi.json
```

And the Swagger explorer at `/docs` (FastAPI's default).
