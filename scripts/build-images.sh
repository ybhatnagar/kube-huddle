#!/usr/bin/env bash
# Build the three container images for the Kube Huddle.
# Tag: kubehuddle/{collector,engine,ui}:$TAG (default 0.1.0, override via first arg
# or the LATENCYREC_TAG env var to match your Helm values `images.*.tag`).
#
# Optional: `--kind <cluster>` also loads the images into the named kind cluster
# so the local Helm install picks them up without a registry push.

set -euo pipefail

TAG="${1:-${LATENCYREC_TAG:-0.1.0}}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "=== Building kubehuddle/collector:${TAG} ==="
docker build -t "kubehuddle/collector:${TAG}" "${REPO_ROOT}/collector"

echo "=== Building kubehuddle/engine:${TAG} ==="
docker build -t "kubehuddle/engine:${TAG}" "${REPO_ROOT}/engine"

echo "=== Building kubehuddle/ui:${TAG} ==="
docker build -t "kubehuddle/ui:${TAG}" "${REPO_ROOT}/ui"

echo "=== Sizes ==="
docker images --format "table {{.Repository}}\t{{.Tag}}\t{{.Size}}" \
  | grep -E "kubehuddle/(collector|engine|ui)|REPOSITORY"

# Optional: `--kind <cluster-name>` to load into a kind cluster.
if [[ "${2:-}" == "--kind" && -n "${3:-}" ]]; then
  echo "=== Loading images into kind cluster '${3}' ==="
  kind load docker-image "kubehuddle/collector:${TAG}" --name "${3}"
  kind load docker-image "kubehuddle/engine:${TAG}"    --name "${3}"
  kind load docker-image "kubehuddle/ui:${TAG}"        --name "${3}"
fi

echo
echo "Next: helm upgrade --install kubehuddle deploy/helm/kubehuddle/ \\"
echo "        --set images.collector.tag=${TAG} \\"
echo "        --set images.engine.tag=${TAG} \\"
echo "        --set images.ui.tag=${TAG}"
