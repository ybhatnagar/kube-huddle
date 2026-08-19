"""Synthetic data fixtures for the latency head (docs/05 §D).

Fixtures produce known-answer graphs the engine + API + UI can run end-to-end on
without a live cluster.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Sequence


@dataclass(frozen=True)
class LatencyPair:
    """One directed workload pair with a known smoothed latency."""
    src: str            # workload_uid, e.g. "team/Deployment/w1"
    dst: str
    latency_ms: float   # the target smoothed value; we seed 3 identical samples
    avg_count: float = 100.0


@dataclass(frozen=True)
class Fixture:
    name: str
    cluster: str
    namespace: str
    workloads: Sequence[tuple]           # (workload_uid, name, node_name, kind)
    pairs: Sequence[LatencyPair]
    all_nodes: Sequence[str] = field(default_factory=tuple)


def fig2_cluster(namespace: str = "default", cluster: str = "fig2") -> Fixture:
    """The disclosure's 8-node graph (docs/01 §B Stage 2).

    Edges (avg latency, ms): N1-N3:1, N1-N2:2, N2-N3:3, N3-N4:4, N2-N4:5,
    N4-N6:8, N4-N8:10, N6-N7:2, N6-N5:1, N7-N5:3, N5-N8:7.

    One workload per node — W1..W8 on N1..N8 — and one directed pair per edge
    with latency = edge weight. The engine's Stage-2 W_ij averages all pairs
    between (Ni, Nj) → with n=1 that yields exactly the edge weight, so the
    reconstructed node graph matches Fig.2 exactly.
    """
    workloads = tuple(
        (f"{namespace}/Deployment/w{i}", f"w{i}", f"N{i}", "Deployment")
        for i in range(1, 9)
    )

    def uid(i: int) -> str:
        return f"{namespace}/Deployment/w{i}"

    edges = [
        (1, 3, 1.0), (1, 2, 2.0), (2, 3, 3.0), (3, 4, 4.0), (2, 4, 5.0),
        (4, 6, 8.0), (4, 8, 10.0),
        (6, 7, 2.0), (6, 5, 1.0), (7, 5, 3.0), (5, 8, 7.0),
    ]
    pairs = tuple(LatencyPair(src=uid(i), dst=uid(j), latency_ms=w) for i, j, w in edges)
    return Fixture(
        name="fig2_cluster",
        cluster=cluster,
        namespace=namespace,
        workloads=workloads,
        pairs=pairs,
        all_nodes=tuple(f"N{i}" for i in range(1, 9)),
    )


# --- seeding ---------------------------------------------------------------


def seed_latency_cluster(store, fixture: Fixture) -> int:
    """Write cluster/workloads/pods/interactions/latency samples for the fixture.

    Returns the cluster_id. Each pair is seeded as three identical latency
    samples so Stage-1 exponential smoothing recovers the target value exactly
    for any α, i.
    """
    cluster_id = store.ensure_cluster(fixture.cluster)
    now = datetime.now(timezone.utc)

    for uid_, name, node, kind in fixture.workloads:
        store.upsert_workload(
            cluster_id=cluster_id, workload_uid=uid_, namespace=fixture.namespace,
            kind=kind, name=name, replicas=1,
        )
        store.upsert_pod(
            cluster_id=cluster_id, namespace=fixture.namespace,
            workload_name=name, pod_name=f"{name}-0", node_name=node,
        )

    # Latency samples: 3 identical values per pair (smoothed → target).
    lat_rows = []
    int_rows = []
    for p in fixture.pairs:
        for k in range(3):
            lat_rows.append({
                "cluster_id": cluster_id,
                "src_workload_uid": p.src,
                "dst_workload_uid": p.dst,
                "ts": now - timedelta(minutes=(2 - k)),
                "latency_ms": p.latency_ms,
                "unit": "ms",
            })
        int_rows.append({
            "cluster_id": cluster_id,
            "src_workload_uid": p.src,
            "dst_workload_uid": p.dst,
            "avg_count": p.avg_count,
            "window_start": now - timedelta(hours=1),
            "window_end": now,
        })
    store.insert_latency_samples(lat_rows)
    store.insert_interactions(int_rows)
    return cluster_id


def sparse_migration_cluster(namespace: str = "default", cluster: str = "sparse") -> Fixture:
    """3 clean islands + one low-volume app straddling — the KubeCon slide-18
    pattern (docs/05 §D). Expected whatif output: exactly one 'migrate' rec
    for app C, moving into the group where its dominant peer lives.

    Layout:
      Island A = {N1, N2}, apps {A1@N1, A2@N2}, hot internal traffic.
      Island B = {N3, N4}, apps {B1@N3, B2@N4}, hot internal traffic.
      Island C = {N5},     app  {C@N5}, sparse cross-island calls dominated by
                           calls to A1.
    """
    workloads = (
        (f"{namespace}/Deployment/A1", "A1", "N1", "Deployment"),
        (f"{namespace}/Deployment/A2", "A2", "N2", "Deployment"),
        (f"{namespace}/Deployment/B1", "B1", "N3", "Deployment"),
        (f"{namespace}/Deployment/B2", "B2", "N4", "Deployment"),
        (f"{namespace}/Deployment/C",  "C",  "N5", "Deployment"),
    )

    def uid(name: str) -> str:
        return f"{namespace}/Deployment/{name}"

    pairs = (
        # Island A — tight (low internal latency, high volume).
        LatencyPair(uid("A1"), uid("A2"), 1.0, avg_count=500.0),
        # Island B — tight.
        LatencyPair(uid("B1"), uid("B2"), 1.0, avg_count=500.0),
        # A ↔ B — high inter-island latency (so they stay separate islands).
        LatencyPair(uid("A1"), uid("B1"), 20.0, avg_count=200.0),
        LatencyPair(uid("A2"), uid("B2"), 20.0, avg_count=200.0),
        # C ↔ A1 (dominant peer, high volume).
        LatencyPair(uid("C"), uid("A1"), 15.0, avg_count=100.0),
        # C ↔ B1 (tiny cross-island volume — the whole point of the fixture).
        LatencyPair(uid("C"), uid("B1"), 25.0, avg_count=2.0),
    )
    return Fixture(
        name="sparse_migration_cluster",
        cluster=cluster,
        namespace=namespace,
        workloads=workloads,
        pairs=pairs,
        all_nodes=("N1", "N2", "N3", "N4", "N5"),
    )


def no_data_cluster(namespace: str = "default", cluster: str = "nodata") -> Fixture:
    """Two connected nodes plus one isolated node (no interactions at all).

    Expected whatif output: a low-confidence 'replicate here (insufficient
    data)' rec for the workload on N3.
    """
    workloads = (
        (f"{namespace}/Deployment/api",  "api",  "N1", "Deployment"),
        (f"{namespace}/Deployment/worker", "worker", "N2", "Deployment"),
        (f"{namespace}/Deployment/lonely", "lonely", "N3", "Deployment"),
    )

    def uid(name: str) -> str:
        return f"{namespace}/Deployment/{name}"

    pairs = (LatencyPair(uid("api"), uid("worker"), 2.0, avg_count=100.0),)
    return Fixture(
        name="no_data_cluster",
        cluster=cluster,
        namespace=namespace,
        workloads=workloads,
        pairs=pairs,
        all_nodes=("N1", "N2", "N3"),
    )


__all__ = [
    "Fixture", "LatencyPair", "fig2_cluster", "sparse_migration_cluster",
    "no_data_cluster", "seed_latency_cluster",
]
