// Command collector is Module 1 — the data collector CLI. It ingests metrics (and
// later interactions) into the state DB via pluggable connectors. Default run mode
// is CLI + k8s CronJob; on-demand runs use a one-shot k8s Job or the trigger
// service (`serve`).
package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"strings"
	"time"

	"github.com/kube-huddle/collector/internal/config"
	"github.com/kube-huddle/collector/internal/connectors"
	_ "github.com/kube-huddle/collector/internal/connectors/kubernetes" // self-registers "kubernetes"
	_ "github.com/kube-huddle/collector/internal/connectors/prometheus" // self-registers "prometheus" (metrics + interactions + latency)
	"github.com/kube-huddle/collector/internal/ingest"
	"github.com/kube-huddle/collector/internal/server"
	"github.com/kube-huddle/collector/internal/store"
)

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	var err error
	switch os.Args[1] {
	case "ingest":
		err = cmdIngest(os.Args[2:])
	case "discover":
		err = cmdDiscover(os.Args[2:])
	case "db":
		err = cmdDB(os.Args[2:])
	case "connectors":
		err = cmdConnectors(os.Args[2:])
	case "serve":
		err = cmdServe(os.Args[2:])
	case "-h", "--help", "help":
		usage()
		return
	default:
		fmt.Fprintf(os.Stderr, "unknown command %q\n\n", os.Args[1])
		usage()
		os.Exit(2)
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		os.Exit(1)
	}
}

func usage() {
	fmt.Fprint(os.Stderr, `collector — Kube Huddle data collector

Usage:
  collector ingest [--all|--metrics|--interactions|--latency] [flags]  ingest into the state DB
  collector discover [flags]                                          k8s API: namespaces + workloads(kind) + pods(node_name)
  collector db migrate [flags]                                        apply/verify schema
  collector connectors list                                           show registered connectors
  collector serve [--addr :8081] [flags]                              on-demand trigger service

Common flags:
  --db-driver   sqlite|postgres            (env KUBEHUDDLE_DB_DRIVER, default sqlite)
  --db-dsn      connection string / path   (env KUBEHUDDLE_DB_DSN, default ./kubehuddle.db)
  --config      path to a JSON config file (flags > env > file > default)

Ingest flags:
  --source             metrics connector           (default prometheus)
  --interaction-source hubble|istio|otel           (env KUBEHUDDLE_INTERACTION_SOURCE, default hubble)
  --latency-source     hubble|istio|otel           (env KUBEHUDDLE_LATENCY_SOURCE, default hubble)
  --prom-url           Prometheus endpoint         (env KUBEHUDDLE_PROM_URL, default http://prometheus.monitoring:9090)
  --prom-bearer        bearer token for Prometheus (env KUBEHUDDLE_PROM_BEARER)
  --namespace          comma-separated namespaces  (default: all)
  --resources          comma-separated resources   (default cpu,memory; any free-text resource allowed)
  --since              lookback window             (default 7d; accepts 7d, 24h, 90m)
  --step               sample resolution           (default 1h)
  --cluster            cluster name                (default "default")

Discover flags:
  --k8s-url            k8s API endpoint            (env KUBEHUDDLE_K8S_URL, default in-cluster)
  --k8s-bearer         k8s API bearer token        (env KUBEHUDDLE_K8S_BEARER)
  --namespace          comma-separated namespaces  (default: all)
  --cluster            cluster name                (default "default")
`)
}

// commonDBFlags registers the db flags shared by every subcommand.
func commonDBFlags(fs *flag.FlagSet) (driver, dsn, cfgPath *string) {
	driver = fs.String("db-driver", "", "sqlite|postgres")
	dsn = fs.String("db-dsn", "", "connection string / sqlite path")
	cfgPath = fs.String("config", "", "path to JSON config file")
	return
}

func openStore(r *config.Resolver) (store.StateStore, error) {
	driver := r.String("db-driver", "KUBEHUDDLE_DB_DRIVER", "sqlite")
	def := "./kubehuddle.db"
	if driver != "sqlite" && driver != "" {
		def = "" // no safe default DSN for postgres
	}
	dsn := r.String("db-dsn", "KUBEHUDDLE_DB_DSN", def)
	if dsn == "" {
		return nil, fmt.Errorf("--db-dsn is required for driver %q", driver)
	}
	return store.Open(driver, dsn)
}

func cmdIngest(args []string) error {
	fs := flag.NewFlagSet("ingest", flag.ExitOnError)
	all := fs.Bool("all", false, "run all steps")
	doMetrics := fs.Bool("metrics", false, "run the metrics step")
	doInteractions := fs.Bool("interactions", false, "run the interactions step")
	doLatency := fs.Bool("latency", false, "run the latency step")
	doDiscover := fs.Bool("discover", false, "run the k8s discover step (namespaces + workloads.kind + pods.node_name)")
	// These are read back through the resolver by flag name, so their pointers
	// aren't captured; registering them is what matters.
	fs.String("source", "", "metrics connector name")
	fs.String("interaction-source", "", "interaction source: hubble|istio|otel")
	fs.String("latency-source", "", "latency source: hubble|istio|otel")
	fs.String("prom-url", "", "Prometheus endpoint")
	fs.String("prom-bearer", "", "Prometheus bearer token")
	fs.String("namespace", "", "comma-separated namespaces (default all)")
	fs.String("resources", "", "comma-separated resources")
	fs.String("since", "", "lookback window, e.g. 7d")
	fs.String("step", "", "sample resolution, e.g. 1h")
	fs.String("cluster", "", "cluster name")
	_, _, cfgPath := commonDBFlags(fs)
	if err := fs.Parse(args); err != nil {
		return err
	}
	r, err := config.NewResolver(fs, *cfgPath)
	if err != nil {
		return err
	}

	sinceDur, err := ingest.ParseSince(r.String("since", "", "7d"))
	if err != nil {
		return err
	}
	stepDur, err := time.ParseDuration(r.String("step", "", "1h"))
	if err != nil {
		return fmt.Errorf("invalid --step: %w", err)
	}

	selected := selectedSteps(*all, *doMetrics, *doInteractions, *doLatency, *doDiscover)
	sourceName := r.String("source", "", "prometheus")
	interactionSource := r.String("interaction-source", "KUBEHUDDLE_INTERACTION_SOURCE", "hubble")
	latencySource := r.String("latency-source", "KUBEHUDDLE_LATENCY_SOURCE", "hubble")
	clusterName := r.String("cluster", "", "default")
	namespaces := splitCSV(r.String("namespace", "", ""))
	resList := splitCSV(r.String("resources", "", "cpu,memory"))

	st, err := openStore(r)
	if err != nil {
		return err
	}
	defer st.Close()

	ctx := context.Background()
	if err := st.Migrate(ctx); err != nil {
		return fmt.Errorf("migrate: %w", err)
	}
	clusterID, err := st.EnsureCluster(ctx, clusterName)
	if err != nil {
		return err
	}

	now := time.Now().UTC()
	win := connectors.Window{Start: now.Add(-sinceDur), End: now, Step: stepDur}
	req := ingest.Request{
		ClusterID:         clusterID,
		Source:            sourceName,
		InteractionSource: interactionSource,
		LatencySource:     latencySource,
		DiscoverSource:    "kubernetes",
		Endpoint:          r.String("prom-url", "KUBEHUDDLE_PROM_URL", "http://prometheus.monitoring:9090"),
		Auth:              connectors.AuthConfig{Bearer: r.String("prom-bearer", "KUBEHUDDLE_PROM_BEARER", "")},
		Namespaces:        namespaces,
		Resources:         resList,
		Window:            win,
		Steps:             selected,
	}

	fmt.Printf("ingest: cluster=%q source=%q resources=%v window=%s..%s step=%s\n",
		clusterName, sourceName, resList,
		win.Start.Format(time.RFC3339), win.End.Format(time.RFC3339), stepDur)

	run, err := ingest.Run(ctx, st, req)
	if err != nil {
		return err
	}
	fmt.Printf("done: status=%s rows=%d\n", run.Status, run.RowsWritten)
	if run.Error != "" {
		fmt.Fprintf(os.Stderr, "errors: %s\n", run.Error)
		return fmt.Errorf("collection %s", run.Status)
	}
	return nil
}

func cmdDiscover(args []string) error {
	fs := flag.NewFlagSet("discover", flag.ExitOnError)
	fs.String("k8s-url", "", "k8s API endpoint")
	fs.String("k8s-bearer", "", "k8s API bearer token")
	fs.String("namespace", "", "comma-separated namespaces (default all)")
	fs.String("cluster", "", "cluster name")
	_, _, cfgPath := commonDBFlags(fs)
	if err := fs.Parse(args); err != nil {
		return err
	}
	r, err := config.NewResolver(fs, *cfgPath)
	if err != nil {
		return err
	}
	clusterName := r.String("cluster", "", "default")
	namespaces := splitCSV(r.String("namespace", "", ""))
	endpoint := r.String("k8s-url", "KUBEHUDDLE_K8S_URL", "")
	bearer := r.String("k8s-bearer", "KUBEHUDDLE_K8S_BEARER", "")

	st, err := openStore(r)
	if err != nil {
		return err
	}
	defer st.Close()

	ctx := context.Background()
	if err := st.Migrate(ctx); err != nil {
		return fmt.Errorf("migrate: %w", err)
	}
	clusterID, err := st.EnsureCluster(ctx, clusterName)
	if err != nil {
		return err
	}

	// Discover runs standalone — no metrics/latency window, just a snapshot.
	req := ingest.Request{
		ClusterID:      clusterID,
		DiscoverSource: "kubernetes",
		Endpoint:       endpoint,
		Auth:           connectors.AuthConfig{Bearer: bearer},
		Namespaces:     namespaces,
		Window:         connectors.Window{Start: time.Now().UTC(), End: time.Now().UTC()},
		Steps:          []string{"discover"},
	}
	fmt.Printf("discover: cluster=%q endpoint=%q namespaces=%v\n", clusterName, endpoint, namespaces)
	run, err := ingest.Run(ctx, st, req)
	if err != nil {
		return err
	}
	fmt.Printf("done: status=%s rows=%d\n", run.Status, run.RowsWritten)
	if run.Error != "" {
		fmt.Fprintf(os.Stderr, "errors: %s\n", run.Error)
		return fmt.Errorf("discover %s", run.Status)
	}
	return nil
}

func cmdDB(args []string) error {
	if len(args) == 0 || args[0] != "migrate" {
		return fmt.Errorf("usage: collector db migrate [flags]")
	}
	fs := flag.NewFlagSet("db migrate", flag.ExitOnError)
	_, _, cfgPath := commonDBFlags(fs)
	if err := fs.Parse(args[1:]); err != nil {
		return err
	}
	r, err := config.NewResolver(fs, *cfgPath)
	if err != nil {
		return err
	}
	st, err := openStore(r)
	if err != nil {
		return err
	}
	defer st.Close()
	if err := st.Migrate(context.Background()); err != nil {
		return err
	}
	fmt.Println("migrations applied")
	return nil
}

func cmdConnectors(args []string) error {
	if len(args) == 0 || args[0] != "list" {
		return fmt.Errorf("usage: collector connectors list")
	}
	metrics, interactions, latency, discover := connectors.Names()
	printList := func(title string, names []string) {
		fmt.Println(title + ":")
		if len(names) == 0 {
			fmt.Println("  (none registered)")
		}
		for _, n := range names {
			fmt.Printf("  - %s\n", n)
		}
	}
	printList("metrics connectors", metrics)
	printList("interaction connectors", interactions)
	printList("latency connectors", latency)
	printList("discover connectors", discover)
	return nil
}

func cmdServe(args []string) error {
	fs := flag.NewFlagSet("serve", flag.ExitOnError)
	addr := fs.String("addr", ":8081", "listen address")
	fs.String("source", "", "default metrics connector")
	fs.String("interaction-source", "", "default interaction source: hubble|istio|otel")
	fs.String("prom-url", "", "default Prometheus endpoint")
	fs.String("prom-bearer", "", "default Prometheus bearer token")
	fs.String("cluster", "", "default cluster name")
	_, _, cfgPath := commonDBFlags(fs)
	if err := fs.Parse(args); err != nil {
		return err
	}
	r, err := config.NewResolver(fs, *cfgPath)
	if err != nil {
		return err
	}
	st, err := openStore(r)
	if err != nil {
		return err
	}
	defer st.Close()

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()
	if err := st.Migrate(ctx); err != nil {
		return fmt.Errorf("migrate: %w", err)
	}
	deps := server.Deps{
		Store:                    st,
		DefaultSource:            r.String("source", "", "prometheus"),
		DefaultInteractionSource: r.String("interaction-source", "KUBEHUDDLE_INTERACTION_SOURCE", "hubble"),
		DefaultEndpoint:          r.String("prom-url", "KUBEHUDDLE_PROM_URL", "http://prometheus.monitoring:9090"),
		DefaultAuth:              connectors.AuthConfig{Bearer: r.String("prom-bearer", "KUBEHUDDLE_PROM_BEARER", "")},
		DefaultCluster:           r.String("cluster", "", "in-cluster"),
	}
	fmt.Printf("collector trigger service listening on %s\n", *addr)
	return server.Serve(ctx, *addr, deps)
}

// --- helpers ---------------------------------------------------------------

func selectedSteps(all, metrics, interactions, latency, discover bool) []string {
	if all {
		return []string{"discover", "metrics", "interactions", "latency"}
	}
	var out []string
	if discover {
		out = append(out, "discover")
	}
	if metrics {
		out = append(out, "metrics")
	}
	if interactions {
		out = append(out, "interactions")
	}
	if latency {
		out = append(out, "latency")
	}
	if len(out) == 0 {
		return []string{"metrics"} // sensible default
	}
	return out
}

func splitCSV(s string) []string {
	var out []string
	for _, part := range strings.Split(s, ",") {
		if p := strings.TrimSpace(part); p != "" {
			out = append(out, p)
		}
	}
	return out
}
