package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"strconv"
	"strings"
	"time"

	// Registered SQL drivers. modernc is pure-Go (no cgo) so the collector image
	// stays small/static; pgx is the Postgres driver via database/sql.
	_ "github.com/jackc/pgx/v5/stdlib"
	_ "modernc.org/sqlite"
)

const (
	dialectSQLite   = "sqlite"
	dialectPostgres = "postgres"
)

// SQLStore is a database/sql-backed StateStore that speaks both SQLite and
// Postgres. Dialect differences (placeholders, timestamp/bool encoding) are
// isolated in the small helpers below; the SQL itself is shared and uses the
// ON CONFLICT upsert form supported by both engines.
type SQLStore struct {
	db      *sql.DB
	dialect string
}

// Open connects to the state DB. driver is "sqlite" (dev) or "postgres" (prod).
// For SQLite the DSN is a file path; foreign keys + a busy timeout are enabled
// via pragmas so every pooled connection enforces them.
func Open(driver, dsn string) (*SQLStore, error) {
	sqlDriver, dialect, err := resolveDriver(driver)
	if err != nil {
		return nil, err
	}
	if dialect == dialectSQLite {
		dsn = sqliteDSN(dsn)
	}
	db, err := sql.Open(sqlDriver, dsn)
	if err != nil {
		return nil, fmt.Errorf("open %s: %w", driver, err)
	}
	if dialect == dialectSQLite {
		// SQLite is single-writer; cap the pool to avoid "database is locked".
		db.SetMaxOpenConns(1)
	}
	return &SQLStore{db: db, dialect: dialect}, nil
}

func resolveDriver(driver string) (sqlDriver, dialect string, err error) {
	switch strings.ToLower(strings.TrimSpace(driver)) {
	case "", "sqlite", "sqlite3":
		return "sqlite", dialectSQLite, nil
	case "postgres", "postgresql", "pgx":
		return "pgx", dialectPostgres, nil
	default:
		return "", "", fmt.Errorf("unsupported db driver %q (use sqlite|postgres)", driver)
	}
}

// sqliteDSN appends pragmas so foreign keys are enforced and writes wait rather
// than failing under brief contention.
func sqliteDSN(dsn string) string {
	if strings.Contains(dsn, "_pragma") {
		return dsn
	}
	const pragmas = "_pragma=busy_timeout(5000)&_pragma=foreign_keys(1)"
	if strings.Contains(dsn, "?") {
		return dsn + "&" + pragmas
	}
	return dsn + "?" + pragmas
}

func (s *SQLStore) Migrate(ctx context.Context) error {
	return runMigrations(ctx, s.db, s.dialect, s.rebind)
}

func (s *SQLStore) Ping(ctx context.Context) error { return s.db.PingContext(ctx) }
func (s *SQLStore) Close() error                   { return s.db.Close() }

func (s *SQLStore) EnsureCluster(ctx context.Context, name string) (int64, error) {
	if _, err := s.db.ExecContext(ctx, s.rebind(
		`INSERT INTO clusters (name, created_at, status) VALUES (?, ?, 'unknown')
		 ON CONFLICT (name) DO NOTHING`),
		name, s.ts(time.Now())); err != nil {
		return 0, fmt.Errorf("ensure cluster: %w", err)
	}
	var id int64
	err := s.db.QueryRowContext(ctx, s.rebind(`SELECT id FROM clusters WHERE name = ?`), name).Scan(&id)
	if err != nil {
		return 0, fmt.Errorf("lookup cluster id: %w", err)
	}
	return id, nil
}

func (s *SQLStore) UpsertWorkloads(ctx context.Context, ws []WorkloadIdentity) error {
	if len(ws) == 0 {
		return nil
	}
	q := s.rebind(`INSERT INTO disc_workloads
		(cluster_id, workload_uid, namespace, kind, name, replicas, requests_cpu_m, requests_mem_bytes, labels, fetched_at)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
		ON CONFLICT (cluster_id, workload_uid) DO UPDATE SET
			namespace          = excluded.namespace,
			kind               = excluded.kind,
			name               = excluded.name,
			replicas           = excluded.replicas,
			requests_cpu_m     = excluded.requests_cpu_m,
			requests_mem_bytes = excluded.requests_mem_bytes,
			labels             = excluded.labels,
			fetched_at         = excluded.fetched_at`)

	return s.inTx(ctx, func(tx *sql.Tx) error {
		stmt, err := tx.PrepareContext(ctx, q)
		if err != nil {
			return err
		}
		defer stmt.Close()
		for _, w := range ws {
			if _, err := stmt.ExecContext(ctx,
				w.ClusterID, w.WorkloadUID, w.Namespace, w.Kind, w.Name,
				nullI64(w.Replicas), nullI64(w.RequestsCPUm), nullI64(w.RequestsMemBytes),
				labelsJSON(w.Labels), s.ts(w.FetchedAt),
			); err != nil {
				return err
			}
		}
		return nil
	})
}

func (s *SQLStore) UpsertMetricSamples(ctx context.Context, samples []MetricSample) (int, error) {
	if len(samples) == 0 {
		return 0, nil
	}
	q := s.rebind(`INSERT INTO metric_samples
		(cluster_id, workload_uid, resource, resource_kind, ts, value, unit, is_rate, collected_at)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
		ON CONFLICT (cluster_id, workload_uid, resource, ts) DO UPDATE SET
			resource_kind = excluded.resource_kind,
			value         = excluded.value,
			unit          = excluded.unit,
			is_rate       = excluded.is_rate,
			collected_at  = excluded.collected_at`)

	err := s.inTx(ctx, func(tx *sql.Tx) error {
		stmt, err := tx.PrepareContext(ctx, q)
		if err != nil {
			return err
		}
		defer stmt.Close()
		for _, m := range samples {
			if _, err := stmt.ExecContext(ctx,
				m.ClusterID, m.WorkloadUID, m.Resource, resourceKindOrDefault(m.ResourceKind), s.ts(m.TS),
				m.Value, nullStr(m.Unit), s.boolArg(m.IsRate), s.ts(m.CollectedAt),
			); err != nil {
				return err
			}
		}
		return nil
	})
	if err != nil {
		return 0, err
	}
	return len(samples), nil
}

// UpsertInteractions writes dependency edges (tier 3, interactions).
func (s *SQLStore) UpsertInteractions(ctx context.Context, edges []Interaction) (int, error) {
	if len(edges) == 0 {
		return 0, nil
	}
	q := s.rebind(`INSERT INTO interactions
		(cluster_id, src_workload_uid, dst_workload_uid, avg_count, window_start, window_end, collected_at)
		VALUES (?, ?, ?, ?, ?, ?, ?)
		ON CONFLICT (cluster_id, src_workload_uid, dst_workload_uid, window_start) DO UPDATE SET
			avg_count    = excluded.avg_count,
			window_end   = excluded.window_end,
			collected_at = excluded.collected_at`)

	err := s.inTx(ctx, func(tx *sql.Tx) error {
		stmt, err := tx.PrepareContext(ctx, q)
		if err != nil {
			return err
		}
		defer stmt.Close()
		for _, e := range edges {
			if _, err := stmt.ExecContext(ctx,
				e.ClusterID, e.SrcWorkloadUID, e.DstWorkloadUID, e.AvgCount,
				s.ts(e.WindowStart), s.ts(e.WindowEnd), s.ts(e.CollectedAt),
			); err != nil {
				return err
			}
		}
		return nil
	})
	if err != nil {
		return 0, err
	}
	return len(edges), nil
}

// UpsertLatencySamples writes raw pair-latency points (tier 3,
// interaction_latency_samples). Idempotent on (cluster, src, dst, ts).
func (s *SQLStore) UpsertLatencySamples(ctx context.Context, rows []InteractionLatencySample) (int, error) {
	if len(rows) == 0 {
		return 0, nil
	}
	q := s.rebind(`INSERT INTO interaction_latency_samples
		(cluster_id, src_workload_uid, dst_workload_uid, ts, latency_ms, unit, collected_at)
		VALUES (?, ?, ?, ?, ?, ?, ?)
		ON CONFLICT (cluster_id, src_workload_uid, dst_workload_uid, ts) DO UPDATE SET
			latency_ms   = excluded.latency_ms,
			unit         = excluded.unit,
			collected_at = excluded.collected_at`)

	err := s.inTx(ctx, func(tx *sql.Tx) error {
		stmt, err := tx.PrepareContext(ctx, q)
		if err != nil {
			return err
		}
		defer stmt.Close()
		for _, r := range rows {
			unit := r.Unit
			if unit == "" {
				unit = "ms"
			}
			if _, err := stmt.ExecContext(ctx,
				r.ClusterID, r.SrcWorkloadUID, r.DstWorkloadUID,
				s.ts(r.TS), r.LatencyMs, unit, s.ts(r.CollectedAt),
			); err != nil {
				return err
			}
		}
		return nil
	})
	if err != nil {
		return 0, err
	}
	return len(rows), nil
}

// UpsertNamespaces writes discovered namespaces (tier 2, disc_namespaces).
func (s *SQLStore) UpsertNamespaces(ctx context.Context, clusterID int64, names []string, fetchedAt time.Time) error {
	if len(names) == 0 {
		return nil
	}
	q := s.rebind(`INSERT INTO disc_namespaces (cluster_id, name, fetched_at)
		VALUES (?, ?, ?)
		ON CONFLICT (cluster_id, name) DO UPDATE SET fetched_at = excluded.fetched_at`)
	return s.inTx(ctx, func(tx *sql.Tx) error {
		stmt, err := tx.PrepareContext(ctx, q)
		if err != nil {
			return err
		}
		defer stmt.Close()
		for _, n := range names {
			if _, err := stmt.ExecContext(ctx, clusterID, n, s.ts(fetchedAt)); err != nil {
				return err
			}
		}
		return nil
	})
}

// UpsertPods writes placement records (tier 2, disc_pods).
func (s *SQLStore) UpsertPods(ctx context.Context, pods []Pod) (int, error) {
	if len(pods) == 0 {
		return 0, nil
	}
	q := s.rebind(`INSERT INTO disc_pods
		(cluster_id, namespace, workload_name, pod_name, node_name, node_instance_type, fetched_at)
		VALUES (?, ?, ?, ?, ?, ?, ?)
		ON CONFLICT (cluster_id, namespace, pod_name) DO UPDATE SET
			workload_name      = excluded.workload_name,
			node_name          = excluded.node_name,
			node_instance_type = excluded.node_instance_type,
			fetched_at         = excluded.fetched_at`)

	err := s.inTx(ctx, func(tx *sql.Tx) error {
		stmt, err := tx.PrepareContext(ctx, q)
		if err != nil {
			return err
		}
		defer stmt.Close()
		for _, p := range pods {
			if _, err := stmt.ExecContext(ctx,
				p.ClusterID, p.Namespace, p.WorkloadName, p.PodName,
				nullStr(p.NodeName), nullStr(p.NodeInstanceType), s.ts(p.FetchedAt),
			); err != nil {
				return err
			}
		}
		return nil
	})
	if err != nil {
		return 0, err
	}
	return len(pods), nil
}

func (s *SQLStore) CreateCollectionRun(ctx context.Context, r *CollectionRun) (int64, error) {
	q := s.rebind(`INSERT INTO collection_runs
		(cluster_id, scope, resources, window_start, window_end, sources_used, status, rows_written, data_as_of, started_at, finished_at, error)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
		RETURNING id`)
	var id int64
	err := s.db.QueryRowContext(ctx, q,
		r.ClusterID, nullStr(r.Scope), nullStr(r.Resources),
		s.ts(r.WindowStart), s.ts(r.WindowEnd), nullStr(r.SourcesUsed),
		r.Status, r.RowsWritten, s.tsPtr(r.DataAsOf), s.tsPtr(r.StartedAt),
		s.tsPtr(r.FinishedAt), nullStr(r.Error),
	).Scan(&id)
	if err != nil {
		return 0, fmt.Errorf("create collection_run: %w", err)
	}
	r.ID = id
	return id, nil
}

func (s *SQLStore) UpdateCollectionRun(ctx context.Context, r *CollectionRun) error {
	q := s.rebind(`UPDATE collection_runs SET
		status = ?, rows_written = ?, data_as_of = ?, started_at = ?, finished_at = ?, error = ?
		WHERE id = ?`)
	_, err := s.db.ExecContext(ctx, q,
		r.Status, r.RowsWritten, s.tsPtr(r.DataAsOf), s.tsPtr(r.StartedAt),
		s.tsPtr(r.FinishedAt), nullStr(r.Error), r.ID)
	if err != nil {
		return fmt.Errorf("update collection_run: %w", err)
	}
	return nil
}

func (s *SQLStore) inTx(ctx context.Context, fn func(*sql.Tx) error) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	if err := fn(tx); err != nil {
		_ = tx.Rollback()
		return err
	}
	return tx.Commit()
}

// --- dialect helpers -------------------------------------------------------

// rebind converts '?' placeholders to '$1, $2, …' for Postgres; SQLite uses '?'.
func (s *SQLStore) rebind(q string) string {
	if s.dialect != dialectPostgres {
		return q
	}
	var b strings.Builder
	n := 0
	for i := 0; i < len(q); i++ {
		if q[i] == '?' {
			n++
			b.WriteByte('$')
			b.WriteString(strconv.Itoa(n))
			continue
		}
		b.WriteByte(q[i])
	}
	return b.String()
}

// ts encodes a time for the dialect: ISO-8601 UTC text for SQLite (TEXT columns),
// native time.Time for Postgres (TIMESTAMPTZ).
func (s *SQLStore) ts(t time.Time) any {
	if s.dialect == dialectSQLite {
		return t.UTC().Format(time.RFC3339)
	}
	return t.UTC()
}

func (s *SQLStore) tsPtr(t *time.Time) any {
	if t == nil {
		return nil
	}
	return s.ts(*t)
}

func (s *SQLStore) boolArg(b bool) any {
	if s.dialect == dialectSQLite {
		if b {
			return 1
		}
		return 0
	}
	return b
}

func nullI64(p *int64) any {
	if p == nil {
		return nil
	}
	return *p
}

func resourceKindOrDefault(k string) string {
	if k == "" {
		return KindStandard
	}
	return k
}

func nullStr(s string) any {
	if s == "" {
		return nil
	}
	return s
}

func labelsJSON(m map[string]string) any {
	if len(m) == 0 {
		return nil
	}
	b, err := json.Marshal(m)
	if err != nil {
		return nil
	}
	return string(b)
}
