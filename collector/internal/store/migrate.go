package store

import (
	"context"
	"database/sql"
	"embed"
	"fmt"
	"io/fs"
	"sort"
	"strings"
	"time"
)

// migrationsFS holds the canonical, dialect-specific schema migrations. This is
// the single source of truth for the state-DB schema; the engine (Python) reads
// the same SQLite files for its dev/test schema, so the contract can't drift.
//
//go:embed migrations/sqlite/*.sql migrations/postgres/*.sql
var migrationsFS embed.FS

// runMigrations applies every not-yet-applied migration file for the dialect, in
// filename order, each within a transaction. Bookkeeping lives in schema_migrations
// (owned here, not by the SQL files) so it is created portably from Go.
func runMigrations(ctx context.Context, db *sql.DB, dialect string, rebind func(string) string) error {
	if _, err := db.ExecContext(ctx,
		`CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)`); err != nil {
		return fmt.Errorf("ensure schema_migrations: %w", err)
	}

	applied, err := appliedVersions(ctx, db)
	if err != nil {
		return err
	}

	dir := "migrations/" + dialect
	entries, err := fs.ReadDir(migrationsFS, dir)
	if err != nil {
		return fmt.Errorf("read migrations dir %s: %w", dir, err)
	}
	var files []string
	for _, e := range entries {
		if !e.IsDir() && strings.HasSuffix(e.Name(), ".sql") {
			files = append(files, e.Name())
		}
	}
	sort.Strings(files)

	for _, f := range files {
		version := strings.SplitN(f, "_", 2)[0]
		if applied[version] {
			continue
		}
		raw, err := migrationsFS.ReadFile(dir + "/" + f)
		if err != nil {
			return err
		}
		if err := applyMigration(ctx, db, rebind, version, string(raw)); err != nil {
			return fmt.Errorf("apply migration %s: %w", f, err)
		}
	}
	return nil
}

func appliedVersions(ctx context.Context, db *sql.DB) (map[string]bool, error) {
	rows, err := db.QueryContext(ctx, `SELECT version FROM schema_migrations`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[string]bool{}
	for rows.Next() {
		var v string
		if err := rows.Scan(&v); err != nil {
			return nil, err
		}
		out[v] = true
	}
	return out, rows.Err()
}

func applyMigration(ctx context.Context, db *sql.DB, rebind func(string) string, version, script string) error {
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback() }()

	for _, stmt := range splitStatements(script) {
		if _, err := tx.ExecContext(ctx, stmt); err != nil {
			return fmt.Errorf("statement failed: %w\n---\n%s", err, stmt)
		}
	}
	if _, err := tx.ExecContext(ctx,
		rebind(`INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)`),
		version, time.Now().UTC().Format(time.RFC3339)); err != nil {
		return err
	}
	return tx.Commit()
}

// splitStatements breaks a migration script into individual statements. Neither
// database/sql driver (modernc sqlite, pgx) runs multiple statements per Exec, so
// we split on ';'. Line comments are stripped first; the schema contains no ';'
// inside string literals, so this is safe.
func splitStatements(script string) []string {
	var noComments strings.Builder
	for _, line := range strings.Split(script, "\n") {
		if i := strings.Index(line, "--"); i >= 0 {
			line = line[:i]
		}
		noComments.WriteString(line)
		noComments.WriteByte('\n')
	}
	var stmts []string
	for _, s := range strings.Split(noComments.String(), ";") {
		if s = strings.TrimSpace(s); s != "" {
			stmts = append(stmts, s)
		}
	}
	return stmts
}
