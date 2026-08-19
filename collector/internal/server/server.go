// Package server is the collector's on-demand trigger service (docs/04 §F.2): the
// engine's API calls POST /ingest to collect right now (for the UI "collect then run"
// flow), while a k8s CronJob drives scheduled collection against the same collector
// code. The pure-batch CLI (`collector ingest`) remains fully functional on its own.
//
// POST /ingest is asynchronous: it creates the collection_runs row, returns its id,
// and finishes the ingestion in the background. Callers poll the row (the engine's
// GET /collections/{id}) for terminal status.
package server

import (
	"context"
	"encoding/json"
	"net/http"
	"time"

	"github.com/kube-huddle/collector/internal/connectors"
	"github.com/kube-huddle/collector/internal/ingest"
	"github.com/kube-huddle/collector/internal/store"
)

// Deps are the collection settings the trigger service defaults to (overridable
// per request).
type Deps struct {
	Store                    store.StateStore
	DefaultSource            string
	DefaultInteractionSource string
	DefaultEndpoint          string
	DefaultAuth              connectors.AuthConfig
	DefaultCluster           string
}

type ingestRequest struct {
	ClusterID         *int64   `json:"cluster_id"`
	Cluster           string   `json:"cluster"`
	Namespaces        []string `json:"namespaces"`
	Resources         []string `json:"resources"`
	Since             string   `json:"since"`
	Step              string   `json:"step"`
	Source            string   `json:"source"`
	InteractionSource string   `json:"interaction_source"`
	LatencySource     string   `json:"latency_source"`
	DiscoverSource    string   `json:"discover_source"`
	Steps             []string `json:"steps"`
	PromURL           string   `json:"prom_url"`
	PromBearer        string   `json:"prom_bearer"`
}

// Serve starts the trigger service on addr and blocks until ctx is cancelled.
func Serve(ctx context.Context, addr string, deps Deps) error {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
	mux.HandleFunc("/ingest", handleIngest(deps))

	srv := &http.Server{Addr: addr, Handler: mux, ReadHeaderTimeout: 5 * time.Second}
	go func() {
		<-ctx.Done()
		shutCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = srv.Shutdown(shutCtx)
	}()
	if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		return err
	}
	return nil
}

func handleIngest(deps Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "POST only"})
			return
		}
		var body ingestRequest
		if r.Body != nil {
			_ = json.NewDecoder(r.Body).Decode(&body) // empty body → all defaults
		}

		sinceDur, err := ingest.ParseSince(orDefault(body.Since, "7d"))
		if err != nil {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
			return
		}
		stepDur, err := time.ParseDuration(orDefault(body.Step, "1h"))
		if err != nil {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid step: " + err.Error()})
			return
		}

		ctx := r.Context()
		var clusterID int64
		if body.ClusterID != nil {
			clusterID = *body.ClusterID
		} else {
			clusterID, err = deps.Store.EnsureCluster(ctx, orDefault(body.Cluster, deps.DefaultCluster))
			if err != nil {
				writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
				return
			}
		}

		auth := deps.DefaultAuth
		if body.PromBearer != "" {
			auth.Bearer = body.PromBearer
		}
		resources := body.Resources
		if len(resources) == 0 {
			resources = []string{store.ResourceCPU, store.ResourceMemory}
		}
		selected := body.Steps
		if len(selected) == 0 {
			selected = []string{"metrics", "interactions", "latency"}
		}
		now := time.Now().UTC()
		req := ingest.Request{
			ClusterID:         clusterID,
			Source:            orDefault(body.Source, deps.DefaultSource),
			InteractionSource: orDefault(body.InteractionSource, orDefault(deps.DefaultInteractionSource, "hubble")),
			LatencySource:     orDefault(body.LatencySource, "hubble"),
			DiscoverSource:    orDefault(body.DiscoverSource, "kubernetes"),
			Endpoint:          orDefault(body.PromURL, deps.DefaultEndpoint),
			Auth:              auth,
			Namespaces:        body.Namespaces,
			Resources:         resources,
			Window:            connectors.Window{Start: now.Add(-sinceDur), End: now, Step: stepDur},
			Steps:             selected,
		}

		run, err := ingest.Start(ctx, deps.Store, req)
		if err != nil {
			writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
			return
		}
		// Finish in the background; the caller polls the collection_runs row.
		go func() { _ = ingest.Finish(context.Background(), deps.Store, req, run) }()

		writeJSON(w, http.StatusAccepted, map[string]any{"collection_id": run.ID, "status": run.Status})
	}
}

func orDefault(v, def string) string {
	if v == "" {
		return def
	}
	return v
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}
