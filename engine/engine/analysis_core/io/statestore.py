"""StateStore -- the engine's DB access layer.

Reads tiers 2-3 (discovery cache, collected data) and writes tier 4 (analysis runs).
Backed by SQLite (dev) or Postgres (prod). The canonical schema lives with the
collector; `apply_schema()` runs those same SQLite migrations so the cross-module
contract cannot drift. Latency-specific result tables are added in M1.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_dt(v: Any) -> Optional[datetime]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    s = str(v).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class StateStore:
    def __init__(self, driver: str = "sqlite", dsn: str = "./kubehuddle.db"):
        self.dialect, self.conn = _connect(driver, dsn)

    def close(self) -> None:
        self.conn.close()

    # --- dialect helpers ---------------------------------------------------

    def _q(self, sql: str) -> str:
        return sql if self.dialect == "sqlite" else sql.replace("?", "%s")

    def _exec(self, sql: str, params: tuple = ()):  # returns a cursor
        cur = self.conn.cursor()
        cur.execute(self._q(sql), params)
        return cur

    def _fetchall(self, sql: str, params: tuple = ()) -> list[dict]:
        cur = self._exec(sql, params)
        return [dict(r) for r in cur.fetchall()]

    def _fetchone(self, sql: str, params: tuple = ()) -> Optional[dict]:
        cur = self._exec(sql, params)
        row = cur.fetchone()
        return dict(row) if row is not None else None

    def _insert_id(self, sql: str, params: tuple) -> int:
        """INSERT (without RETURNING) and return the new row id, cross-dialect."""
        if self.dialect == "sqlite":
            cur = self._exec(sql, params)
            return int(cur.lastrowid)
        cur = self._exec(sql + " RETURNING id", params)
        return int(cur.fetchone()["id"])

    def _bool(self, b: bool) -> Any:
        return (1 if b else 0) if self.dialect == "sqlite" else bool(b)

    def _json(self, obj: Any) -> Any:
        if obj is None:
            return None
        if self.dialect == "sqlite":
            return json.dumps(obj)
        from psycopg.types.json import Json  # lazy: only when Postgres is used
        return Json(obj)

    def _json_load(self, v: Any) -> Any:
        if v is None or v == "":
            return None
        if self.dialect == "sqlite" and isinstance(v, str):
            return json.loads(v)
        return v

    def commit(self) -> None:
        self.conn.commit()

    # --- schema (dev/test only) -------------------------------------------

    def apply_schema(self) -> None:
        """Create the schema in a fresh SQLite DB from the canonical migrations."""
        if self.dialect != "sqlite":
            raise RuntimeError("apply_schema is a SQLite dev/test helper; run `collector db migrate` for Postgres")
        if self._table_exists("metric_samples"):
            return
        self.conn.executescript(_sqlite_schema_sql())
        self.conn.commit()

    def _table_exists(self, name: str) -> bool:
        return self._fetchone("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)) is not None

    # --- tier 1 config -----------------------------------------------------

    def ensure_cluster(self, name: str) -> int:
        self._exec(
            "INSERT INTO clusters (name, created_at, status) VALUES (?, ?, 'unknown') ON CONFLICT (name) DO NOTHING",
            (name, _iso(_now())),
        )
        self.commit()
        return int(self._fetchone("SELECT id FROM clusters WHERE name = ?", (name,))["id"])

    def get_settings(self) -> Optional[dict]:
        row = self._fetchone("SELECT * FROM settings WHERE id = 1")
        if row:
            row["thresholds"] = self._json_load(row.get("thresholds"))
        return row

    # --- tier 2/3 seeding (collector normally writes these; used by tests/synth) ---

    def upsert_workload(
        self, cluster_id: int, workload_uid: str, namespace: str, kind: str, name: str,
        replicas: Optional[int] = None, requests_cpu_m: Optional[int] = None,
        requests_mem_bytes: Optional[int] = None, fetched_at: Optional[datetime] = None,
    ) -> None:
        self._exec(
            """INSERT INTO disc_workloads
               (cluster_id, workload_uid, namespace, kind, name, replicas, requests_cpu_m, requests_mem_bytes, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (cluster_id, workload_uid) DO UPDATE SET
                 namespace=excluded.namespace, kind=excluded.kind, name=excluded.name,
                 replicas=excluded.replicas, requests_cpu_m=excluded.requests_cpu_m,
                 requests_mem_bytes=excluded.requests_mem_bytes,
                 fetched_at=excluded.fetched_at""",
            (cluster_id, workload_uid, namespace, kind, name, replicas, requests_cpu_m, requests_mem_bytes,
             _iso(fetched_at or _now())),
        )
        self.commit()

    def insert_metric_samples(self, rows: list[dict]) -> int:
        sql = self._q(
            """INSERT INTO metric_samples
               (cluster_id, workload_uid, resource, resource_kind, ts, value, unit, is_rate, collected_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (cluster_id, workload_uid, resource, ts) DO UPDATE SET
                 value=excluded.value, unit=excluded.unit, is_rate=excluded.is_rate, collected_at=excluded.collected_at"""
        )
        cur = self.conn.cursor()
        params = [
            (r["cluster_id"], r["workload_uid"], r["resource"], r.get("resource_kind", "standard"),
             _iso(r["ts"]) if isinstance(r["ts"], datetime) else r["ts"],
             float(r["value"]), r.get("unit"), self._bool(bool(r.get("is_rate", False))),
             _iso(r.get("collected_at") or _now()))
            for r in rows
        ]
        cur.executemany(sql, params)
        self.commit()
        return len(rows)

    def insert_interactions(self, rows: list[dict]) -> int:
        sql = self._q(
            """INSERT INTO interactions
               (cluster_id, src_workload_uid, dst_workload_uid, avg_count, window_start, window_end, collected_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (cluster_id, src_workload_uid, dst_workload_uid, window_start) DO UPDATE SET
                 avg_count=excluded.avg_count, collected_at=excluded.collected_at"""
        )
        cur = self.conn.cursor()
        now = _iso(_now())
        params = [
            (r["cluster_id"], r["src_workload_uid"], r["dst_workload_uid"], r.get("avg_count"),
             _iso(r["window_start"]) if r.get("window_start") else None,
             _iso(r["window_end"]) if r.get("window_end") else None, now)
            for r in rows
        ]
        cur.executemany(sql, params)
        self.commit()
        return len(rows)

    # --- tier 2/3 reads ----------------------------------------------------

    def load_series(self, cluster_id: int, workload_uid: str, resource: str) -> list[tuple]:
        rows = self._fetchall(
            "SELECT ts, value FROM metric_samples WHERE cluster_id = ? AND workload_uid = ? AND resource = ? ORDER BY ts",
            (cluster_id, workload_uid, resource),
        )
        return [(r["ts"], r["value"]) for r in rows]

    def get_identity(self, cluster_id: int, workload_uid: str) -> Optional[dict]:
        return self._fetchone(
            "SELECT namespace, kind, name, replicas, requests_cpu_m, requests_mem_bytes "
            "FROM disc_workloads WHERE cluster_id = ? AND workload_uid = ?",
            (cluster_id, workload_uid),
        )

    def max_collected_at(self, cluster_id: int) -> Optional[datetime]:
        row = self._fetchone(
            "SELECT max(collected_at) AS m FROM metric_samples WHERE cluster_id = ?",
            (cluster_id,),
        )
        return _parse_dt(row["m"]) if row else None

    def get_outgoing_interactions(self, cluster_id: int, src_uid: str) -> list[dict]:
        return self._fetchall(
            "SELECT dst_workload_uid, avg_count FROM interactions WHERE cluster_id = ? AND src_workload_uid = ? ORDER BY avg_count DESC",
            (cluster_id, src_uid),
        )

    def get_incoming_interactions(self, cluster_id: int, dst_uid: str) -> list[dict]:
        return self._fetchall(
            "SELECT src_workload_uid, avg_count FROM interactions WHERE cluster_id = ? AND dst_workload_uid = ? ORDER BY avg_count DESC",
            (cluster_id, dst_uid),
        )

    # --- latency: tier 2/3 writes (collector/synth) -----------------------

    def upsert_pod(
        self, cluster_id: int, namespace: str, workload_name: str, pod_name: str,
        node_name: Optional[str] = None, node_instance_type: Optional[str] = None,
        fetched_at: Optional[datetime] = None,
    ) -> None:
        self._exec(
            """INSERT INTO disc_pods
               (cluster_id, namespace, workload_name, pod_name, node_name, node_instance_type, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (cluster_id, namespace, pod_name) DO UPDATE SET
                 workload_name=excluded.workload_name, node_name=excluded.node_name,
                 node_instance_type=excluded.node_instance_type, fetched_at=excluded.fetched_at""",
            (cluster_id, namespace, workload_name, pod_name, node_name, node_instance_type,
             _iso(fetched_at or _now())),
        )
        self.commit()

    def insert_latency_samples(self, rows: list[dict]) -> int:
        sql = self._q(
            """INSERT INTO interaction_latency_samples
               (cluster_id, src_workload_uid, dst_workload_uid, ts, latency_ms, unit, collected_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (cluster_id, src_workload_uid, dst_workload_uid, ts) DO UPDATE SET
                 latency_ms=excluded.latency_ms, unit=excluded.unit, collected_at=excluded.collected_at"""
        )
        cur = self.conn.cursor()
        now = _iso(_now())
        params = [
            (r["cluster_id"], r["src_workload_uid"], r["dst_workload_uid"],
             _iso(r["ts"]) if isinstance(r["ts"], datetime) else r["ts"],
             float(r["latency_ms"]), r.get("unit", "ms"), r.get("collected_at") or now)
            for r in rows
        ]
        cur.executemany(sql, params)
        self.commit()
        return len(rows)

    # --- latency: tier 2/3 reads ------------------------------------------

    def load_pod_placements(self, cluster_id: int) -> dict[str, str]:
        """Return workload_uid -> node_name for the cluster.

        Joins disc_pods.workload_name back to disc_workloads via (namespace, name)
        so we get the stable workload_uid the interactions/latency tables use.
        """
        rows = self._fetchall(
            """SELECT w.workload_uid AS uid, p.node_name AS node_name
                 FROM disc_pods p
                 JOIN disc_workloads w
                   ON w.cluster_id = p.cluster_id
                  AND w.namespace  = p.namespace
                  AND w.name       = p.workload_name
                WHERE p.cluster_id = ? AND p.node_name IS NOT NULL""",
            (cluster_id,),
        )
        return {r["uid"]: r["node_name"] for r in rows}

    def load_latency_series_by_pair(self, cluster_id: int) -> dict[tuple, list[float]]:
        """Return {(src_uid, dst_uid) -> [latency_ms, ...]} ordered by ts."""
        rows = self._fetchall(
            """SELECT src_workload_uid AS src, dst_workload_uid AS dst, latency_ms
                 FROM interaction_latency_samples
                WHERE cluster_id = ?
                ORDER BY src_workload_uid, dst_workload_uid, ts""",
            (cluster_id,),
        )
        out: dict[tuple, list[float]] = {}
        for r in rows:
            out.setdefault((r["src"], r["dst"]), []).append(float(r["latency_ms"]))
        return out

    def load_interactions_volume(self, cluster_id: int) -> dict[tuple, float]:
        """Return {(src_uid, dst_uid) -> avg_count} from the interactions table."""
        rows = self._fetchall(
            """SELECT src_workload_uid AS src, dst_workload_uid AS dst, avg_count
                 FROM interactions
                WHERE cluster_id = ?""",
            (cluster_id,),
        )
        return {(r["src"], r["dst"]): float(r["avg_count"] or 0.0) for r in rows}

    def list_all_workloads(self, cluster_id: int) -> list[dict]:
        return self._fetchall(
            "SELECT workload_uid, namespace, kind, name FROM disc_workloads WHERE cluster_id = ?",
            (cluster_id,),
        )

    # --- latency: tier 4 writes (results) ---------------------------------

    def insert_latency_group(
        self, run_id: int, group_index: int, node_names: list[str],
        latency_ratio: float, workload_count: int = 0, total_size: float = 0.0,
        label: Optional[str] = None, color_hint: Optional[str] = None,
    ) -> int:
        label = label or ", ".join(node_names)
        return self._insert_id(
            """INSERT INTO latency_groups
               (run_id, group_index, label, node_names, latency_ratio, workload_count, total_size, color_hint)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (run_id, group_index, label, self._json(node_names), latency_ratio,
             workload_count, total_size, color_hint),
        )

    def insert_latency_group_workload(
        self, group_id: int, run_id: int, workload_uid: str,
        workload_name: Optional[str], namespace: Optional[str], kind: Optional[str],
        node_name: Optional[str], size_value: float = 0.0,
        size_metric: str = "interaction_volume",
    ) -> int:
        return self._insert_id(
            """INSERT INTO latency_group_workloads
               (group_id, run_id, workload_uid, workload_name, namespace, kind, node_name, size_value, size_metric)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (group_id, run_id, workload_uid, workload_name, namespace, kind, node_name,
             size_value, size_metric),
        )

    def insert_latency_edge(
        self, run_id: int, node_a: str, node_b: str, weight_ms: float,
        sample_count: int, kind: str, group_id: Optional[int] = None,
    ) -> int:
        return self._insert_id(
            """INSERT INTO latency_edges
               (run_id, node_a, node_b, weight_ms, sample_count, kind, group_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (run_id, node_a, node_b, weight_ms, sample_count, kind, group_id),
        )

    def insert_latency_recommendation(
        self, run_id: int, workload_uid: str, action: str, *,
        workload_kind: Optional[str] = None, workload_name: Optional[str] = None,
        namespace: Optional[str] = None,
        from_scope: Optional[str] = None, to_scope: Optional[str] = None,
        from_group_index: Optional[int] = None, to_group_index: Optional[int] = None,
        cost_saving_amount: Optional[float] = None,
        savings_currency: Optional[str] = None, savings_period: Optional[str] = None,
        latency_delta_ms: Optional[float] = None,
        suggested_mechanism: Optional[str] = None, suggested_snippet: Optional[str] = None,
        confidence: str = "low", summary_text: Optional[str] = None,
    ) -> int:
        return self._insert_id(
            """INSERT INTO latency_recommendations
               (run_id, workload_uid, workload_kind, workload_name, namespace,
                action, from_scope, to_scope, from_group_index, to_group_index,
                cost_saving_amount, savings_currency, savings_period, latency_delta_ms,
                suggested_mechanism, suggested_snippet, confidence, summary_text)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (run_id, workload_uid, workload_kind, workload_name, namespace,
             action, from_scope, to_scope, from_group_index, to_group_index,
             cost_saving_amount, savings_currency, savings_period, latency_delta_ms,
             suggested_mechanism, suggested_snippet, confidence, summary_text),
        )

    # --- latency: tier 4 reads (API + CLI) --------------------------------

    def list_latency_groups(self, run_id: int) -> list[dict]:
        rows = self._fetchall(
            """SELECT id, group_index, label, node_names, latency_ratio,
                      workload_count, total_size, color_hint
                 FROM latency_groups WHERE run_id = ? ORDER BY group_index""",
            (run_id,),
        )
        for r in rows:
            r["node_names"] = self._json_load(r.get("node_names")) or []
        return rows

    def list_latency_recommendations(self, run_id: int) -> list[dict]:
        return self._fetchall(
            "SELECT * FROM latency_recommendations WHERE run_id = ? ORDER BY id",
            (run_id,),
        )

    def get_latency_recommendation(self, recommendation_id: int) -> Optional[dict]:
        return self._fetchone(
            "SELECT * FROM latency_recommendations WHERE id = ?",
            (recommendation_id,),
        )

    def list_latency_group_workloads(self, run_id: int) -> list[dict]:
        return self._fetchall(
            """SELECT id, group_id, workload_uid, workload_name, namespace, kind,
                      node_name, size_value, size_metric
                 FROM latency_group_workloads
                WHERE run_id = ? ORDER BY group_id, id""",
            (run_id,),
        )

    def list_latency_edges(self, run_id: int) -> list[dict]:
        return self._fetchall(
            """SELECT id, node_a, node_b, weight_ms, sample_count, kind, group_id
                 FROM latency_edges WHERE run_id = ? ORDER BY id""",
            (run_id,),
        )

    def get_latency_evidence(self, recommendation_id: int) -> Optional[dict]:
        row = self._fetchone(
            "SELECT id, subgraph, detail FROM latency_evidence WHERE recommendation_id = ?",
            (recommendation_id,),
        )
        if not row:
            return None
        row["subgraph"] = self._json_load(row.get("subgraph"))
        row["detail"] = self._json_load(row.get("detail"))
        return row

    def list_latency_peers(self, recommendation_id: int) -> list[dict]:
        return self._fetchall(
            """SELECT id, peer_workload_uid, peer_workload, relation, avg_count, latency_ms
                 FROM latency_peers WHERE recommendation_id = ? ORDER BY id""",
            (recommendation_id,),
        )

    def insert_latency_evidence(
        self, recommendation_id: int, subgraph: Any, detail: Any,
    ) -> int:
        return self._insert_id(
            """INSERT INTO latency_evidence (recommendation_id, subgraph, detail)
               VALUES (?, ?, ?)""",
            (recommendation_id, self._json(subgraph), self._json(detail)),
        )

    def insert_latency_peer(
        self, recommendation_id: int, *, peer_workload_uid: Optional[str],
        peer_workload: Optional[str], relation: Optional[str],
        avg_count: Optional[float], latency_ms: Optional[float],
    ) -> int:
        return self._insert_id(
            """INSERT INTO latency_peers
               (recommendation_id, peer_workload_uid, peer_workload, relation, avg_count, latency_ms)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (recommendation_id, peer_workload_uid, peer_workload, relation,
             avg_count, latency_ms),
        )

    # --- tier 4 writes (analysis runs) ------------------------------------

    def create_analysis_run(
        self, name: str, cluster_id: int, scope: Any, config: dict,
        data_as_of: Optional[datetime], stale: bool, ttl_hours: int,
        collection_run_id: Optional[int] = None, run_type: str = "latency",
    ) -> int:
        created = _now()
        expires = created + timedelta(hours=ttl_hours)
        return self._insert_id(
            """INSERT INTO analysis_runs
               (name, cluster_id, run_type, scope, config, collection_run_id, data_as_of, stale, status, created_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?)""",
            (name, cluster_id, run_type, self._json(scope), self._json(config), collection_run_id, _iso(data_as_of),
             self._bool(stale), _iso(created), _iso(expires)),
        )

    def finish_analysis_run(self, run_id: int, status: str, error: Optional[str] = None) -> None:
        self._exec(
            "UPDATE analysis_runs SET status = ?, completed_at = ?, error = ? WHERE id = ?",
            (status, _iso(_now()), error, run_id),
        )
        self.commit()

    # --- tier 4 reads (API / CLI) -----------------------------------------

    def get_run(self, run_id: int) -> Optional[dict]:
        row = self._fetchone(
            """SELECT r.*, c.name AS cluster_name FROM analysis_runs r
               LEFT JOIN clusters c ON c.id = r.cluster_id WHERE r.id = ?""",
            (run_id,),
        )
        if not row:
            return None
        row["stale"] = bool(row.get("stale"))
        for k in ("scope", "config"):
            row[k] = self._json_load(row.get(k))
        return row

    def list_runs(self, cluster_id: Optional[int] = None, limit: int = 50) -> list[dict]:
        cols = "id, name, cluster_id, run_type, status, data_as_of, stale, created_at, completed_at"
        if cluster_id is None:
            rows = self._fetchall(f"SELECT {cols} FROM analysis_runs ORDER BY id DESC LIMIT ?", (limit,))
        else:
            rows = self._fetchall(
                f"SELECT {cols} FROM analysis_runs WHERE cluster_id = ? ORDER BY id DESC LIMIT ?", (cluster_id, limit))
        for r in rows:
            r["stale"] = bool(r.get("stale"))
        return rows

    # --- tier 1 config: clusters (API CRUD) --------------------------------

    def create_cluster(self, name: str, api_url=None, auth_method=None, credential_ref=None, ca_cert=None) -> dict:
        cid = self._insert_id(
            """INSERT INTO clusters (name, api_url, auth_method, credential_ref, ca_cert, created_at, status)
               VALUES (?, ?, ?, ?, ?, ?, 'unknown')""",
            (name, api_url, auth_method, credential_ref, ca_cert, _iso(_now())),
        )
        self.commit()
        return self.get_cluster(cid)

    def list_clusters(self) -> list[dict]:
        return self._fetchall("SELECT * FROM clusters ORDER BY id")

    def get_cluster(self, cluster_id: int) -> Optional[dict]:
        return self._fetchone("SELECT * FROM clusters WHERE id = ?", (cluster_id,))

    def delete_cluster(self, cluster_id: int) -> bool:
        cur = self._exec("DELETE FROM clusters WHERE id = ?", (cluster_id,))
        self.commit()
        return cur.rowcount > 0

    def update_cluster_status(self, cluster_id: int, status: str, touch: bool = False) -> None:
        if touch:
            self._exec("UPDATE clusters SET status = ?, last_connected_at = ? WHERE id = ?",
                       (status, _iso(_now()), cluster_id))
        else:
            self._exec("UPDATE clusters SET status = ? WHERE id = ?", (status, cluster_id))
        self.commit()

    # --- tier 1 config: data sources (API CRUD) ----------------------------

    def create_data_source(self, cluster_id, type_, name, endpoint=None, auth_config=None, settings=None, enabled=True) -> dict:
        sid = self._insert_id(
            """INSERT INTO data_sources (cluster_id, type, name, endpoint, auth_config, settings, enabled, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (cluster_id, type_, name, endpoint, self._json(auth_config), self._json(settings),
             self._bool(enabled), _iso(_now())),
        )
        self.commit()
        return self.get_data_source(sid)

    def list_data_sources(self, cluster_id: Optional[int] = None) -> list[dict]:
        if cluster_id is None:
            rows = self._fetchall("SELECT * FROM data_sources ORDER BY id")
        else:
            rows = self._fetchall(
                "SELECT * FROM data_sources WHERE cluster_id = ? OR cluster_id IS NULL ORDER BY id", (cluster_id,))
        return [self._decode_source(r) for r in rows]

    def get_data_source(self, source_id: int) -> Optional[dict]:
        row = self._fetchone("SELECT * FROM data_sources WHERE id = ?", (source_id,))
        return self._decode_source(row) if row else None

    def update_data_source(self, source_id: int, **fields) -> Optional[dict]:
        sets, params = [], []
        for k in ("name", "endpoint", "type", "health"):
            if fields.get(k) is not None:
                sets.append(f"{k} = ?")
                params.append(fields[k])
        if fields.get("enabled") is not None:
            sets.append("enabled = ?")
            params.append(self._bool(fields["enabled"]))
        for k in ("auth_config", "settings"):
            if fields.get(k) is not None:
                sets.append(f"{k} = ?")
                params.append(self._json(fields[k]))
        if sets:
            params.append(source_id)
            self._exec(f"UPDATE data_sources SET {', '.join(sets)} WHERE id = ?", tuple(params))
            self.commit()
        return self.get_data_source(source_id)

    def delete_data_source(self, source_id: int) -> bool:
        cur = self._exec("DELETE FROM data_sources WHERE id = ?", (source_id,))
        self.commit()
        return cur.rowcount > 0

    def set_source_health(self, source_id: int, health: str) -> None:
        self._exec("UPDATE data_sources SET health = ?, last_checked_at = ? WHERE id = ?",
                   (health, _iso(_now()), source_id))
        self.commit()

    def _decode_source(self, row: dict) -> dict:
        for k in ("auth_config", "settings"):
            row[k] = self._json_load(row.get(k))
        row["enabled"] = bool(row.get("enabled"))
        return row

    # --- settings (API) ----------------------------------------------------

    def update_settings(self, **fields) -> dict:
        sets, params = [], []
        for k in ("metric_ttl_hours", "discovery_ttl_min", "result_ttl_hours", "default_resources", "default_window"):
            if fields.get(k) is not None:
                sets.append(f"{k} = ?")
                params.append(fields[k])
        if fields.get("thresholds") is not None:
            sets.append("thresholds = ?")
            params.append(self._json(fields["thresholds"]))
        if sets:
            self._exec(f"UPDATE settings SET {', '.join(sets)} WHERE id = 1", tuple(params))
            self.commit()
        return self.get_settings()

    # --- discovery cache reads (API) -- derived from what we have data for ----

    def list_namespaces(self, cluster_id: int) -> list[dict]:
        return self._fetchall(
            "SELECT DISTINCT namespace AS name FROM disc_workloads WHERE cluster_id = ? ORDER BY namespace", (cluster_id,))

    def list_workloads(self, cluster_id: int, namespace: Optional[str] = None) -> list[dict]:
        if namespace:
            return self._fetchall(
                "SELECT * FROM disc_workloads WHERE cluster_id = ? AND namespace = ? ORDER BY name", (cluster_id, namespace))
        return self._fetchall(
            "SELECT * FROM disc_workloads WHERE cluster_id = ? ORDER BY namespace, name", (cluster_id,))

    # --- collection runs read (API) ----------------------------------------

    def get_collection_run(self, collection_id: int) -> Optional[dict]:
        row = self._fetchone("SELECT * FROM collection_runs WHERE id = ?", (collection_id,))
        if row:
            for k in ("scope", "resources", "sources_used"):
                row[k] = self._json_load(row.get(k))
        return row


# --- connection + schema resolution ---------------------------------------

def _connect(driver: str, dsn: str):
    d = (driver or "sqlite").lower()
    if d in ("sqlite", "sqlite3", ""):
        conn = sqlite3.connect(dsn, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")  # engine + collector may share the file
        return "sqlite", conn
    if d in ("postgres", "postgresql", "pgx"):
        import psycopg
        from psycopg.rows import dict_row
        conn = psycopg.connect(dsn, row_factory=dict_row, autocommit=False)
        return "postgres", conn
    raise ValueError(f"unsupported db driver {driver!r} (use sqlite|postgres)")


def _sqlite_schema_sql() -> str:
    """Locate and read the canonical SQLite migrations shipped with the collector."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        cand = parent / "collector" / "internal" / "store" / "migrations" / "sqlite"
        if cand.is_dir():
            return "\n".join(p.read_text() for p in sorted(cand.glob("*.sql")))
    raise FileNotFoundError(
        "canonical SQLite migrations not found (collector/internal/store/migrations/sqlite); "
        "run `collector db migrate` to create the schema"
    )
