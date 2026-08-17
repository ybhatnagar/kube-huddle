# Deployment guide

Kube Huddle ships three container images and a Helm chart. Everything is
recommend-only: the chart never grants write RBAC on the target cluster, the
engine never posts back, and every workload runs non-root with a read-only
root filesystem.

## Requirements

- **kubectl ≥ 1.28**, **helm ≥ 3.12**, **docker** (or another buildkit-compatible builder).
- A Kubernetes cluster you can install into. Everything below has been
  smoke-tested on **docker-desktop** (native k8s), **kind**, and **minikube**.
- **Cilium + Hubble** on the target cluster if you want real interaction + latency data. Kube Huddle also works against **Istio** (`istio_request_duration_milliseconds`), **OTel service-graph metrics**, or Prometheus histograms exposed by any mesh — just switch `collector.interactionSource` / `collector.latencySource` in `values.yaml`.

## 1. Build the images

```bash
./scripts/build-images.sh                 # default tag 0.1.0
# or ./scripts/build-images.sh v0.2.0     # custom tag
```

That produces:

```
kubehuddle/collector:0.1.0   (26 MB, distroless-nonroot)
kubehuddle/engine:0.1.0      (510 MB, python:3.12-slim)
kubehuddle/ui:0.1.0          (76 MB, nginx-unprivileged)
```

For a local `kind` cluster, load the images so the chart doesn't need a registry:

```bash
kind create cluster --name kubehuddle
./scripts/build-images.sh 0.1.0 --kind kubehuddle
```

**docker-desktop** shares its docker daemon with the cluster — no `kind load` needed.

## 2. Install the chart

```bash
helm upgrade --install kubehuddle deploy/helm/kubehuddle/ \
    --namespace kubehuddle --create-namespace \
    --wait --timeout 3m
```

The install renders 4 Deployments (engine, ui, collector-svc, postgres), 4
Services, 1 CronJob, a post-install `Job` running `collector db migrate`, and
a ClusterRole + Role for read-only k8s access. The install waits until every
pod is Ready before returning.

## 3. Reach the UI

```bash
kubectl -n kubehuddle port-forward svc/kubehuddle-ui 8080:80
# open http://localhost:8080/
```

The UI's nginx container proxies `/api/` to the engine so the browser only
talks to one origin. Confirm health:

```bash
curl -s http://localhost:8080/api/v1/healthz
# {"status":"ok"}
```

## 4. Verify with a seeded fixture (optional but useful)

Skip the collector-Hubble round-trip on your first install by seeding the
`sparse_migration` fixture inside the running engine pod:

```bash
kubectl -n kubehuddle exec deploy/kubehuddle-engine -- \
    python -m engine.cli seed --fixture sparse_migration
kubectl -n kubehuddle exec deploy/kubehuddle-engine -- \
    python -m engine.cli run --cluster sparse_migration
```

Refresh the UI → `sparse_migration` appears in the cluster grid, and Screen 4
shows the 3-island orb + 5 recommendations.

## 5. Populate real data

The collector CronJob runs hourly by default; the first run happens on the
next top-of-hour tick. To trigger it right away:

```bash
kubectl -n kubehuddle create job --from=cronjob/kubehuddle-collector collect-now
kubectl -n kubehuddle logs -f job/collect-now
```

For discovery (namespaces + workloads with `kind` + pods with `nodeName`),
either wait for the next scheduled tick (the CronJob passes `--discover`) or
run once:

```bash
kubectl -n kubehuddle exec deploy/kubehuddle-collector-svc -- \
    collector discover --cluster in-cluster
```

## External Postgres (recommended for production)

The bundled Postgres is a convenient demo. For prod, stand up your own DB and
hand the chart a Secret whose `dsn` key holds the full DSN:

```bash
kubectl -n kubehuddle create secret generic kubehuddle-db-dsn \
    --from-literal=dsn='postgres://user:pass@db.example:5432/kubehuddle?sslmode=require'

helm upgrade --install kubehuddle deploy/helm/kubehuddle/ \
    --namespace kubehuddle \
    --set postgres.enabled=false \
    --set database.existingSecret=kubehuddle-db-dsn
```

The post-install `collector db migrate` Job re-runs on every upgrade — the migrations
are idempotent (they track state in a `schema_migrations` table).

## Ingress

The chart ships an optional Ingress that routes `/api` → engine and everything
else → UI on a single host:

```yaml
# values-prod.yaml
ingress:
  enabled: true
  className: nginx
  host: kubehuddle.example.com
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt-prod
  tls:
    - hosts: [kubehuddle.example.com]
      secretName: kubehuddle-tls
```

```bash
helm upgrade --install kubehuddle deploy/helm/kubehuddle/ \
    -n kubehuddle -f values-prod.yaml
```

## RBAC

The chart creates:

- **ClusterRole `kubehuddle-readonly`** with `get`/`list` on `namespaces`,
  `pods`, `nodes`, and `apps/{deployments,statefulsets,daemonsets,replicasets}`.
  Bound to the release's ServiceAccount cluster-wide.
- **Role `kubehuddle-secrets`** with `get` on `secrets` in the release
  namespace only. Used by the engine to read credential Secrets you reference
  from cluster records (kubeconfig / token / cert / basic-auth).

**No mutating verbs. No cross-namespace secret access.** Audit with
`kubectl -n kubehuddle get role,rolebinding,clusterrolebinding -l app.kubernetes.io/name=kubehuddle`.

## Connecting external clusters

To point Kube Huddle at a cluster *other* than the one it's installed in,
add it via the UI's **Add cluster** modal. The engine reads a Secret in the
release namespace whose shape depends on the auth method:

| Auth method | Secret keys |
|---|---|
| kubeconfig | `kubeconfig` (a full kubeconfig YAML) |
| token | `token` (bearer) |
| client_cert | `tls.crt` + `tls.key` |
| basic | `username` + `password` |

Example for a linked kind cluster:

```bash
KC=$(kubectl config view --raw --minify --flatten -o yaml \
     | sed 's|server: https://127.0.0.1:[0-9]*|server: https://<reachable-ip>:6443|')
kubectl -n kubehuddle create secret generic kubeconfig-prod \
    --from-literal=kubeconfig="$KC"
```

Then in the UI: **Add cluster → Kubeconfig tab**, cluster name `prod`,
credential reference `kubeconfig-prod`. **Test connection** hits the target
cluster's `/version` from the engine pod and reports back.

## Customizing collection sources

The CronJob passes CLI flags baked from `values.yaml`:

```yaml
collector:
  schedule: "0 * * * *"       # hourly
  interactionSource: hubble   # hubble | istio | otel
  latencySource: hubble
  promUrl: "http://prometheus.monitoring:9090"
  since: "7d"
  step: "1h"
  namespaces: ""              # empty = all
  resources: "cpu,memory"
```

For Istio, set `interactionSource: istio` and `latencySource: istio` — the
Prometheus connector's default queries switch to
`istio_requests_total` + `istio_request_duration_milliseconds_{sum,count}`.
For OTel, `interactionSource: otel` uses
`traces_service_graph_request_total` + `traces_service_graph_request_client_seconds_*`.

Any query is overridable via `Config.Extra["latency_query"]` /
`Config.Extra["interactions_query"]` at collector runtime, or via
`collector.extraArgs` in `values.yaml`.

## Uninstall

```bash
helm -n kubehuddle uninstall kubehuddle
kubectl delete ns kubehuddle
```

The bundled Postgres PVC is deleted with the namespace. If you were on an
external DB, only the tables inside the DB remain (they're safe to leave for
the next install).

## Troubleshooting

- **`ImagePullBackOff` on kind** — you skipped the `--kind` flag on `build-images.sh`. Re-run: `./scripts/build-images.sh 0.1.0 --kind kubehuddle`.
- **`collector-svc` restart-loops for the first 30s** — waiting for Postgres. It stabilizes as soon as `pg_isready` returns. Check with `kubectl -n kubehuddle get pods`.
- **UI shows "API unreachable at /api/v1"** — the port-forward broke. Re-run `kubectl -n kubehuddle port-forward svc/kubehuddle-ui 8080:80`.
- **Empty workload tree on Screen 2** — the state DB's discovery cache hasn't been populated. Run `collector discover --cluster <name>` (see *Populate real data* above).
- **`helm lint` complains about `icon:`** — cosmetic; ignored. Set a real SVG in `Chart.yaml` when you have one.

## Security posture

- **Read-only cluster access.** No `create`/`update`/`patch`/`delete` verbs anywhere in the chart.
- **Non-root runtimes.** Every workload sets `runAsNonRoot: true`, `readOnlyRootFilesystem: true`, `seccompProfile: RuntimeDefault`, drops all capabilities.
- **Credential storage.** Cluster credentials are stored as k8s Secret references — never plaintext in the state DB.
- **Recommend-only.** Migrate/replicate suggestions carry copy-pasteable `nodeAffinity` snippets; **the user** applies them.
