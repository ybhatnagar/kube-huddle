# Engine (Python)

Reads collected data from the state DB, runs the four-stage pipeline
(smooth → weigh → group → whatif), writes recommendations back. Serves the
`/api/v1` HTTP surface via FastAPI.

## Layout

```
engine/
├── engine/
│   ├── analysis_core/          # shared: state-DB access, config, interaction-graph helpers
│   ├── api/                    # FastAPI app + DTO builders
│   ├── recommenders/latency/   # the latency head — smooth.py + weigh.py + group.py + whatif.py + runner.py
│   ├── synth/                  # synthetic-cluster fixtures
│   ├── cli.py                  # `kubehuddle-engine seed/run/serve/init-db`
│   └── runner.py               # top-level dispatch on `run_type`
├── tests/                      # 46 tests: stage units, contract tests, end-to-end
├── pyproject.toml              # kubehuddle-engine, deps: fastapi, uvicorn, numpy, pandas, networkx, PyYAML
└── Dockerfile                  # multi-stage: python:3.12-slim venv → clean slim runtime, UID 10001
```

## Build + test

```bash
python3 -m venv .venv && ./.venv/bin/pip install -e ".[dev]"
./.venv/bin/pytest -q
```

## CLI

Every subcommand accepts `--db-driver sqlite|postgres` and `--db-dsn <path or DSN>`.
Env-var overrides: `KUBEHUDDLE_DB_DRIVER`, `KUBEHUDDLE_DB_DSN`. Defaults:
`sqlite` / `./kubehuddle.db`.

| Subcommand | What it does |
|---|---|
| `init-db` | Create the SQLite schema (dev). Postgres uses `collector db migrate`. |
| `seed --fixture {fig2\|sparse_migration\|no_data}` | Write a known-answer synthetic cluster into the state DB. |
| `run --cluster <name>` | Execute the latency head against the cluster's collected data. Accepts `--alpha`, `--i-window`, `--group-by`, `--ttl-hours`. |
| `serve [--host --port]` | Run the FastAPI `/api/v1` app. `KUBEHUDDLE_UI_DIR=../ui` mounts the static UI at `/`. |

See the top-level [`docs/quickstart.md`](../docs/quickstart.md) for the
five-minute walkthrough and [`docs/api.md`](../docs/api.md) for the full REST
surface.

## Design notes

- The four pipeline stages (`smooth`, `weigh`, `group`, `whatif`) are pure functions with no I/O. The `runner.py` is the only place they meet the state DB.
- `analysis_core/` holds reusable primitives; a second recommender head would add its own `recommenders/<head>/` package and register in `engine/runner.py`'s dispatch.
- Contract tests in `tests/test_api_latency.py` assert the DTO shapes against [`design-docs/04-schema-and-api.md §E`](../design-docs/04-schema-and-api.md). Keep them green.
