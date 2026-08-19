// Package kubernetes is the reference DiscoverConnector: it reads namespaces,
// workloads (Deployment/StatefulSet/DaemonSet), and pods (with nodeName) from
// the k8s API via the REST endpoints — read-only. Kind is recorded on each
// workload so downstream (the engine) can exclude DaemonSets from replicate/
// migrate candidates without a second lookup. Recommend-only: this package
// never issues writes.
//
// The connector talks HTTPS directly to the API server: in-cluster it reads
// the ServiceAccount token + CA from the standard mount; out-of-cluster it
// accepts a bearer token via Config.Auth. Endpoint (Config.Endpoint) can point
// at the API server URL (e.g. https://kubernetes.default.svc) or at a proxy.
package kubernetes

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/kube-huddle/collector/internal/connectors"
	"github.com/kube-huddle/collector/internal/store"
)

const (
	inClusterHost  = "https://kubernetes.default.svc"
	saTokenPath    = "/var/run/secrets/kubernetes.io/serviceaccount/token"
	saCACertPath   = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
	defaultTimeout = 30 * time.Second
)

func init() {
	connectors.RegisterDiscover(New())
}

// Connector implements connectors.DiscoverConnector against the k8s REST API.
type Connector struct {
	HTTP *http.Client // nil -> lazy default
}

// New returns a k8s discover connector with a default HTTP client.
func New() *Connector { return &Connector{} }

// Name is the registry key. The step's default `--discover-source` resolves to
// this.
func (c *Connector) Name() string { return "kubernetes" }

// HealthCheck verifies the API server is reachable (GET /api).
func (c *Connector) HealthCheck(ctx context.Context, cfg connectors.Config) error {
	body, err := c.get(ctx, cfg, "/api")
	if err != nil {
		return err
	}
	if !strings.Contains(string(body), "APIVersions") {
		return fmt.Errorf("kubernetes /api probe returned unexpected body")
	}
	return nil
}

// Discover returns namespaces + workloads + pods for the whole cluster (or
// scoped to Config.Namespaces when set). Pagination TODO — first M2 pass reads
// the full list in one request, which is fine for the fixture path and small
// clusters; a follow-up milestone can add continue-token paging.
func (c *Connector) Discover(ctx context.Context, cfg connectors.Config) (connectors.DiscoverResult, error) {
	var out connectors.DiscoverResult
	now := time.Now().UTC()

	nsList, err := c.listNamespaces(ctx, cfg)
	if err != nil {
		return out, fmt.Errorf("list namespaces: %w", err)
	}
	if len(cfg.Namespaces) > 0 {
		allowed := map[string]bool{}
		for _, n := range cfg.Namespaces {
			allowed[n] = true
		}
		filtered := nsList[:0]
		for _, n := range nsList {
			if allowed[n] {
				filtered = append(filtered, n)
			}
		}
		nsList = filtered
	}
	out.Namespaces = nsList

	deployments, err := c.listWorkloads(ctx, cfg, "deployments")
	if err != nil {
		return out, fmt.Errorf("list deployments: %w", err)
	}
	statefulSets, err := c.listWorkloads(ctx, cfg, "statefulsets")
	if err != nil {
		return out, fmt.Errorf("list statefulsets: %w", err)
	}
	daemonSets, err := c.listWorkloads(ctx, cfg, "daemonsets")
	if err != nil {
		return out, fmt.Errorf("list daemonsets: %w", err)
	}

	for _, w := range deployments {
		w.ClusterID = cfg.ClusterID
		w.Kind = "Deployment"
		w.WorkloadUID = uid(w.Namespace, w.Kind, w.Name)
		w.FetchedAt = now
		out.Workloads = append(out.Workloads, w)
	}
	for _, w := range statefulSets {
		w.ClusterID = cfg.ClusterID
		w.Kind = "StatefulSet"
		w.WorkloadUID = uid(w.Namespace, w.Kind, w.Name)
		w.FetchedAt = now
		out.Workloads = append(out.Workloads, w)
	}
	for _, w := range daemonSets {
		w.ClusterID = cfg.ClusterID
		w.Kind = "DaemonSet"
		w.WorkloadUID = uid(w.Namespace, w.Kind, w.Name)
		w.FetchedAt = now
		out.Workloads = append(out.Workloads, w)
	}

	pods, err := c.listPods(ctx, cfg)
	if err != nil {
		return out, fmt.Errorf("list pods: %w", err)
	}
	for _, p := range pods {
		p.ClusterID = cfg.ClusterID
		p.FetchedAt = now
		out.Pods = append(out.Pods, p)
	}

	if len(cfg.Namespaces) > 0 {
		allowed := map[string]bool{}
		for _, n := range cfg.Namespaces {
			allowed[n] = true
		}
		wOut := out.Workloads[:0]
		for _, w := range out.Workloads {
			if allowed[w.Namespace] {
				wOut = append(wOut, w)
			}
		}
		out.Workloads = wOut
		pOut := out.Pods[:0]
		for _, p := range out.Pods {
			if allowed[p.Namespace] {
				pOut = append(pOut, p)
			}
		}
		out.Pods = pOut
	}
	return out, nil
}

func uid(namespace, kind, name string) string { return namespace + "/" + kind + "/" + name }

// --- HTTP helpers ----------------------------------------------------------

func (c *Connector) client(cfg connectors.Config) (*http.Client, error) {
	if c.HTTP != nil {
		return c.HTTP, nil
	}
	tlsCfg := &tls.Config{MinVersion: tls.VersionTLS12}
	if data, err := os.ReadFile(saCACertPath); err == nil {
		pool := x509.NewCertPool()
		if !pool.AppendCertsFromPEM(data) {
			return nil, errors.New("kubernetes: could not parse in-cluster CA")
		}
		tlsCfg.RootCAs = pool
	}
	return &http.Client{
		Timeout:   defaultTimeout,
		Transport: &http.Transport{TLSClientConfig: tlsCfg},
	}, nil
}

func (c *Connector) get(ctx context.Context, cfg connectors.Config, path string) ([]byte, error) {
	base := strings.TrimRight(cfg.Endpoint, "/")
	if base == "" {
		base = inClusterHost
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, base+path, nil)
	if err != nil {
		return nil, err
	}
	token := cfg.Auth.Bearer
	if token == "" {
		if b, err := os.ReadFile(saTokenPath); err == nil {
			token = strings.TrimSpace(string(b))
		}
	}
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	req.Header.Set("Accept", "application/json")

	cli, err := c.client(cfg)
	if err != nil {
		return nil, err
	}
	resp, err := cli.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 128<<20))
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("kubernetes %s HTTP %d: %s", path, resp.StatusCode, strings.TrimSpace(string(body)))
	}
	return body, nil
}

// --- k8s API response shapes ----------------------------------------------

type nsList struct {
	Items []struct {
		Metadata struct{ Name string } `json:"metadata"`
	} `json:"items"`
}

func (c *Connector) listNamespaces(ctx context.Context, cfg connectors.Config) ([]string, error) {
	body, err := c.get(ctx, cfg, "/api/v1/namespaces")
	if err != nil {
		return nil, err
	}
	var d nsList
	if err := json.Unmarshal(body, &d); err != nil {
		return nil, fmt.Errorf("decode namespaces: %w", err)
	}
	out := make([]string, 0, len(d.Items))
	for _, ns := range d.Items {
		out = append(out, ns.Metadata.Name)
	}
	return out, nil
}

// workloadResp matches the shape of Deployment/StatefulSet/DaemonSet list responses.
type workloadResp struct {
	Items []struct {
		Metadata struct {
			Name      string            `json:"name"`
			Namespace string            `json:"namespace"`
			Labels    map[string]string `json:"labels"`
		} `json:"metadata"`
		Spec struct {
			Replicas int64 `json:"replicas"`
			Template struct {
				Spec struct {
					Containers []struct {
						Resources struct {
							Requests map[string]string `json:"requests"`
						} `json:"resources"`
					} `json:"containers"`
				} `json:"spec"`
			} `json:"template"`
		} `json:"spec"`
	} `json:"items"`
}

func (c *Connector) listWorkloads(ctx context.Context, cfg connectors.Config, kind string) ([]store.WorkloadIdentity, error) {
	body, err := c.get(ctx, cfg, "/apis/apps/v1/"+kind)
	if err != nil {
		return nil, err
	}
	var d workloadResp
	if err := json.Unmarshal(body, &d); err != nil {
		return nil, fmt.Errorf("decode %s: %w", kind, err)
	}
	out := make([]store.WorkloadIdentity, 0, len(d.Items))
	for _, it := range d.Items {
		reps := it.Spec.Replicas
		w := store.WorkloadIdentity{
			Namespace: it.Metadata.Namespace,
			Name:      it.Metadata.Name,
			Replicas:  &reps,
			Labels:    it.Metadata.Labels,
		}
		if len(it.Spec.Template.Spec.Containers) > 0 {
			// Only the sum-of-first-container's requests — a full aggregation lands in M3.
			if cpu := it.Spec.Template.Spec.Containers[0].Resources.Requests["cpu"]; cpu != "" {
				if v, err := parseCPUMilli(cpu); err == nil {
					w.RequestsCPUm = &v
				}
			}
			if mem := it.Spec.Template.Spec.Containers[0].Resources.Requests["memory"]; mem != "" {
				if v, err := parseMemBytes(mem); err == nil {
					w.RequestsMemBytes = &v
				}
			}
		}
		out = append(out, w)
	}
	return out, nil
}

type ownerRef struct {
	Kind string `json:"kind"`
	Name string `json:"name"`
}

type podResp struct {
	Items []struct {
		Metadata struct {
			Name            string            `json:"name"`
			Namespace       string            `json:"namespace"`
			OwnerReferences []ownerRef        `json:"ownerReferences"`
			Labels          map[string]string `json:"labels"`
		} `json:"metadata"`
		Spec struct {
			NodeName string `json:"nodeName"`
		} `json:"spec"`
	} `json:"items"`
}

func (c *Connector) listPods(ctx context.Context, cfg connectors.Config) ([]store.Pod, error) {
	body, err := c.get(ctx, cfg, "/api/v1/pods")
	if err != nil {
		return nil, err
	}
	var d podResp
	if err := json.Unmarshal(body, &d); err != nil {
		return nil, fmt.Errorf("decode pods: %w", err)
	}
	out := make([]store.Pod, 0, len(d.Items))
	for _, p := range d.Items {
		wl := workloadNameFromPod(p.Metadata.Name, p.Metadata.OwnerReferences, p.Metadata.Labels)
		if wl == "" {
			continue // unowned bare pod — skip for now
		}
		out = append(out, store.Pod{
			Namespace:    p.Metadata.Namespace,
			WorkloadName: wl,
			PodName:      p.Metadata.Name,
			NodeName:     p.Spec.NodeName,
		})
	}
	return out, nil
}

// workloadNameFromPod prefers the app label, then trims the pod-name suffix
// (Deployment: "-<hash>-<rand>"; StatefulSet: "-<ordinal>"; DaemonSet: "-<hash>").
// The k8s API's ownerReferences on the Pod point at the ReplicaSet, not the
// Deployment, so a full owner-chain walk would need a second GET; the label
// heuristic covers the common case and is enough for M2's discover fixture.
func workloadNameFromPod(podName string, owners []ownerRef, labels map[string]string) string {
	if v := labels["app.kubernetes.io/name"]; v != "" {
		return v
	}
	if v := labels["app"]; v != "" {
		return v
	}
	if len(owners) > 0 && owners[0].Name != "" {
		return owners[0].Name
	}
	// Fallback: strip last "-<suffix>" chunk from the pod name.
	if i := strings.LastIndex(podName, "-"); i > 0 {
		return podName[:i]
	}
	return podName
}
