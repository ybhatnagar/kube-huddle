# Contributing

Bug reports, feature suggestions, and PRs are welcome. A few conventions keep
the codebase pleasant.

## Getting set up

```bash
git clone https://github.com/kube-huddle/kube-huddle.git
cd kube-huddle

# Engine
cd engine
python3 -m venv .venv && ./.venv/bin/pip install -e ".[dev]"
./.venv/bin/pytest -q     # 46 tests should pass

# Collector
cd ../collector
go test ./...             # 6 packages, all green
go build -o bin/collector ./cmd/collector

# UI: no build step (static HTML + vanilla JS)
```

Try the quickstart end-to-end to make sure your local environment is sane:
[docs/quickstart.md](quickstart.md).

## What to work on

The **Not built yet** section of the README is the honest backlog:

- **Real OpenCost `CostProvider` wired through `POST /runs`.** The engine already accepts a `cost_provider` callable; wiring it up in the HTTP path is a small, self-contained change.
- **Live discovery refresh.** `GET /clusters/{id}/namespaces?refresh=true` currently returns 501. Wiring the k8s client into the discovery endpoints unlocks the UI's `Refresh` button.
- **Grouping by topology labels.** The UI has a `By zone / topology` toggle that already sends `config.group_by`. The engine's placement resolver needs to honour that key instead of always collapsing by `node_name`.
- **Semantic zoom on the orb.** Click an island to expand it and label every workload. Reference lives in `design/orb-options.html` Option D.
- **New recommender heads.** `analysis_runs.run_type` is polymorphic on purpose — see [architecture.md § Adding a second recommender head](architecture.md#adding-a-second-recommender-head).

Look at the GitHub issues before starting non-trivial work — someone might
already be on it.

## Style + conventions

**Python** (engine): PEP 8, type hints on public functions, `dataclass` for
value types. Keep the four pipeline stages pure (no I/O, no `time.time()`
except in tests). If you need a helper, put it next to the stage that uses it,
not in a `utils.py`.

**Go** (collector): `gofmt -s`, standard `errors.Is` / `%w`, no external
loggers (the CLI writes to stderr with `log`). Connectors register in `init()`
— keep the pattern.

**HTML/JS** (UI): no build step, no framework, no external libraries. If you
need to add a dependency, it goes into the CSS or as inline vendored JS. The
UI must keep working air-gapped.

**Commits**: imperative-mood subject lines under 72 chars, wrap body at 80.
`git commit --signoff` if you can — DCO is our compliance floor for OSS PRs.

## Tests

Every change needs one of:

- **New test** for the new behaviour.
- **Updated test** for the behaviour you changed.
- **Explanation** in the PR body if a test isn't the right shape for what you did (docs-only PRs, chart-value tweaks).

CI runs both suites. Locally:

```bash
cd engine && ./.venv/bin/pytest -q
cd collector && go test ./...
```

Contract tests live in `engine/tests/test_api_latency.py` — assert every DTO
shape against `design-docs/04-schema-and-api.md §E`. **Don't loosen them.** If
you need a new field, add it and update both the DTO builder and the doc.

## Contracts (please respect these)

Two things are the actual public API. Everything else is internal.

1. **The state-DB schema.** `collector/internal/store/migrations/{sqlite,postgres}/`. Additive migrations only. Never rename a column that's read from the engine side — add a new one and dual-read. Never drop a table without a `0XXX_drop_<table>.sql` migration that's been out for at least one release.

2. **The `/api/v1` DTOs.** `engine/engine/api/dto.py`. Breaking changes require a version bump on the base path (`/api/v2`) — the engine's runner is versioned via `run_type`, but the DTO shapes are not (yet).

Adding a new recommender head → new `run_type`, new tables, new DTOs. Anything
that changes an existing shape → PR review touching both the migration file
and the DTO builder.

## Security

- **Read-only cluster access.** No PR gets merged that adds a mutating verb to the RBAC in `deploy/helm/kubehuddle/templates/rbac.yaml`. Kube Huddle is recommend-only end to end.
- **No credentials in the state DB.** Cluster credentials are references to k8s Secrets, resolved at probe time by the engine using its own in-cluster SA. If you need to add auth material to a stored row, use a Secret reference — never plaintext.
- **CSP / air-gap.** The UI must keep working with no external network calls. If you want to add a chart library or a font, it goes into `ui/vendor/` and gets referenced with a relative path.

## Release

Version tags are `vMAJOR.MINOR.PATCH`. Images get the same tag; the Helm
chart's `appVersion` gets bumped in the same commit.

```bash
# example
git tag v0.2.0
./scripts/build-images.sh v0.2.0
docker push kubehuddle/collector:v0.2.0
docker push kubehuddle/engine:v0.2.0
docker push kubehuddle/ui:v0.2.0
```

Then update `deploy/helm/kubehuddle/Chart.yaml` (`version:` for the chart,
`appVersion:` for the images) and `values.yaml` (`images.*.tag`) and cut a
Helm release with `helm package + helm repo index`.

## Design docs vs user docs

Two doc trees, on purpose:

- **`design-docs/`** — technical design authored during the build (docs 01–07,
  M1–M6 notes, build prompts). Point of view: the author designing the
  system. Not maintained after a milestone lands.
- **`docs/`** — user-facing docs (this dir). Point of view: someone who wants
  to use / understand / deploy / hack on the tool. Kept current.

If you touch behavior, update `docs/`. If you're documenting an old decision
you undid, update the relevant M*-NOTES.md in `design-docs/`.

## Reporting a bug

Open an issue with:

- What you did (commands, `POST` bodies, cluster state).
- What you expected.
- What happened instead (logs, error message, screenshot).
- Environment: OS, Python version, Go version, k8s version, helm version.

Redact anything sensitive. If it's a security bug, don't open a public
issue — email the maintainer directly.

## Reporting a security issue

Do **not** open a public issue. Email the maintainer at the address in the
top-level `CODEOWNERS` (add one if it's missing). Coordinated disclosure over
30 days from acknowledgement.

## License

MIT. By opening a PR you're agreeing your contribution is under the same
terms as the rest of the repo.
