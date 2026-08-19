# Deployment

Container images and a Helm chart for running the Kube Huddle in a cluster:

- **collector** CronJob (scheduled ingestion) + a **trigger-service** Deployment for
  on-demand collection (the engine's `POST /collections` calls it),
- **engine** Deployment + Service (analysis + REST API),
- **ui** Deployment + Service (optional static front-end),
- an optional bundled **Postgres** (demo) or an external database,
- a **migrate** hook that applies the schema, and read-only **RBAC** for the collector.

The tool only ever *reads* from the target cluster — nothing it deploys writes to your
workloads.

## Images

Each module has its own small image (build from the repo root):

```bash
docker build -t kubehuddle/collector:0.1.0 collector/   # ~26 MB (distroless, static Go)
docker build -t kubehuddle/engine:0.1.0    engine/      # engine + API + Postgres driver
docker build -t kubehuddle/ui:0.1.0        ui/          # ~76 MB (nginx, static bundle)
```

Push them to a registry your cluster can pull from, and set the `images.*.repository`
values accordingly. For a **local cluster**, load them into the nodes instead (the chart
uses `imagePullPolicy: IfNotPresent`, so no registry is needed):

```bash
# minikube
minikube image load kubehuddle/collector:0.1.0 kubehuddle/engine:0.1.0 kubehuddle/ui:0.1.0

# kind (standalone CLI)
kind load docker-image kubehuddle/collector:0.1.0 kubehuddle/engine:0.1.0 kubehuddle/ui:0.1.0

# kind managed by Docker Desktop (no kind CLI) — import into each worker node's containerd
for n in $(kubectl get nodes -o name | sed 's|node/||' | grep -v control-plane); do
  docker save kubehuddle/collector:0.1.0 kubehuddle/engine:0.1.0 kubehuddle/ui:0.1.0 | docker exec -i "$n" ctr -n k8s.io images import -
done
```

> After changing code, rebuild the image and reload it, then `kubectl rollout restart`
> the affected Deployment (same tag + `IfNotPresent` means nodes keep the old image
> until you replace it).

**Validated live** on a 3-node Docker Desktop Kubernetes cluster (v1.36, containerd),
alongside kube-prometheus-stack, in a dedicated `kubehuddle` namespace:
- images loaded into the nodes via `ctr import` (no registry / no `kind` CLI needed);
- `helm install` brought up Postgres, the **migrate hook applied the schema**
  (`allocations` + `qos_*` tables, settings row), engine / UI / collector-svc came up healthy;
- the demo workloads (`deploy/demo/synthetic-workloads.yaml`) deployed to `demo-qos`, and a
  `collect-now` Job (real collector image → real Prometheus) ingested allocations + utilization;
- the engine recovered the **two designed allocation groups** and ranked each
  `*-hot → Guaranteed, *-warm → Burstable, *-idle → BestEffort`, served through the UI's
  `/api` proxy, with the docs/08 YAML export.
(Interaction edges need Cilium/Hubble, absent on Docker Desktop, so that dimension is empty —
ranking still holds on cpu/memory. `current_qos` shows blank until live k8s-API discovery is wired.)
Also validated statically with `helm lint`, `helm template` (default/external-DB/ingress), and
`docker build` of all three images.

## Install (bundled Postgres — quickest)

```bash
helm install qr deploy/helm/kubehuddle \
  --namespace kubehuddle --create-namespace \
  --set collector.promUrl=http://prometheus.monitoring:9090
```

This brings up Postgres, runs the schema migration, starts the engine + UI, and schedules
the collector hourly. Point `collector.promUrl` at your Prometheus. Then:

```bash
kubectl -n kubehuddle port-forward svc/qr-kubehuddle-ui 8080:80
# open http://localhost:8080/  (the UI proxies /api to the engine)
```

## Install (external database — production)

Don't ship the bundled Postgres to production. Provide a Secret with a ready-made DSN:

```bash
kubectl -n kubehuddle create secret generic kubehuddle-db \
  --from-literal=dsn='postgres://user:pass@pg.internal:5432/kubehuddle?sslmode=require'

helm install qr deploy/helm/kubehuddle -n kubehuddle \
  --set postgres.enabled=false \
  --set database.existingSecret=kubehuddle-db \
  --set collector.promUrl=http://prometheus.monitoring:9090 \
  --set engine.corsOrigins=https://kubehuddle.your-domain \
  --set ingress.enabled=true --set ingress.host=kubehuddle.your-domain --set ingress.className=nginx
```

With an Ingress, `/` serves the UI and `/api` the engine on one host.

## Try it on minikube (no Prometheus needed)

```bash
minikube start
minikube image load kubehuddle/collector:0.1.0
minikube image load kubehuddle/engine:0.1.0
minikube image load kubehuddle/ui:0.1.0

helm install qr deploy/helm/kubehuddle -n kubehuddle --create-namespace
kubectl -n kubehuddle rollout status deploy/qr-kubehuddle-engine

# There's no Prometheus to collect from, so seed the synthetic cluster straight into
# Postgres (schema already applied by the migrate hook) and run once. `run --synthetic`
# skips the SQLite schema step for the postgres driver, seeds tiers 2–3, then analyzes:
DSN=$(kubectl -n kubehuddle get secret qr-kubehuddle-db -o jsonpath='{.data.dsn}' | base64 -d)
kubectl -n kubehuddle run seed --rm -it --restart=Never --image=kubehuddle/engine:0.1.0 \
  --image-pull-policy=IfNotPresent -- \
  run --synthetic --db-driver postgres --db-dsn "$DSN" --k 2

kubectl -n kubehuddle port-forward svc/qr-kubehuddle-ui 8080:80
# open http://localhost:8080/ → pick the "synth-qos" cluster → run → recommendations
```

## Collect on demand

The collector runs on `collector.schedule` (hourly by default). To run it right now:

```bash
kubectl -n kubehuddle create job --from=cronjob/qr-kubehuddle-collector collect-now
```

## Key values

| Value | Default | Notes |
|---|---|---|
| `images.*.repository` / `.tag` | `kubehuddle/*` `0.1.0` | set to your registry |
| `postgres.enabled` | `true` | bundled demo DB; set `false` for prod |
| `database.existingSecret` | `""` | Secret with a `dsn` key (prod) |
| `collector.schedule` | `0 * * * *` | ingestion cron |
| `collector.promUrl` | `http://prometheus.monitoring:9090` | your Prometheus |
| `collector.resources` | `cpu,memory` | also `net_tx,net_rx,ephemeral_storage` |
| `engine.corsOrigins` | `*` | lock down in prod |
| `ui.enabled` | `true` | disable to run headless |
| `ingress.enabled` | `false` | routes `/`→UI, `/api`→engine |

## Validate the chart locally

```bash
helm lint deploy/helm/kubehuddle
helm template qr deploy/helm/kubehuddle | kubeconform -strict -kubernetes-version 1.30.0
```

## Notes
- Images run as **non-root** with dropped capabilities; the engine and collector use a
  read-only root filesystem.
- Cluster credentials referenced by `credential_ref` are expected to live in Kubernetes
  Secrets (the DB never stores raw credentials). Wiring live cluster discovery to those
  Secrets lands with the (not-yet-built) Kubernetes client.
