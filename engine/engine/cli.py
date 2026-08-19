"""`engine` CLI -- headless entrypoint (Kube Huddle).

  engine serve [--host 0.0.0.0 --port 8000]   # run the FastAPI /api/v1 app
  engine init-db                               # dev: create the SQLite schema
  engine seed [--fixture fig2] [--cluster fig2]  # write a known-answer fixture
  engine run  [--cluster fig2] [--alpha .. --i-window ..]  # run the latency head
"""
from __future__ import annotations

import argparse
from typing import Optional

from .analysis_core.io.statestore import StateStore
from .runner import run_analysis
from .synth import fig2_cluster, no_data_cluster, seed_latency_cluster, sparse_migration_cluster


def _add_db_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--db-driver", default="sqlite", help="sqlite|postgres")
    p.add_argument("--db-dsn", default="./kubehuddle.db", help="connection string / sqlite path")


def _open_store(args) -> StateStore:
    return StateStore(driver=args.db_driver, dsn=args.db_dsn)


def cmd_serve(args) -> int:
    import os

    import uvicorn
    os.environ.setdefault("KUBEHUDDLE_DB_DRIVER", args.db_driver)
    os.environ.setdefault("KUBEHUDDLE_DB_DSN", args.db_dsn)
    uvicorn.run("engine.api.app:app", host=args.host, port=args.port, log_level="info")
    return 0


def cmd_init_db(args) -> int:
    store = _open_store(args)
    try:
        store.apply_schema()
        print(f"schema ready in {args.db_dsn}")
    finally:
        store.close()
    return 0


FIXTURES = {
    "fig2": fig2_cluster,
    "sparse_migration": sparse_migration_cluster,
    "no_data": no_data_cluster,
}


def cmd_seed(args) -> int:
    store = _open_store(args)
    try:
        store.apply_schema()   # idempotent on SQLite; ensures tables exist
        if args.fixture not in FIXTURES:
            print(f"unknown fixture {args.fixture!r}; choices: {sorted(FIXTURES)}")
            return 2
        fx = FIXTURES[args.fixture](cluster=args.cluster or args.fixture)
        cluster_id = seed_latency_cluster(store, fx)
        print(f"seeded fixture {fx.name!r} into cluster {fx.cluster!r} (id={cluster_id})")
        print(f"  workloads: {len(fx.workloads)}")
        print(f"  pairs: {len(fx.pairs)}")
    finally:
        store.close()
    return 0


def cmd_run(args) -> int:
    store = _open_store(args)
    try:
        overrides = {}
        if args.alpha is not None:
            overrides["alpha"] = args.alpha
        if args.i_window is not None:
            overrides["i_window"] = args.i_window
        if args.group_by is not None:
            overrides["group_by"] = args.group_by

        res = run_analysis(
            store, cluster=args.cluster, scope="all",
            config_overrides=overrides or None, ttl_hours=args.ttl_hours,
            name=args.name, run_type="latency",
        )
        print(f"run {res.name} (id={res.run_id}): {res.status}")
        print(f"  groups={res.groups} recommendations={res.recommendations}")

        groups = store.list_latency_groups(res.run_id)
        total = sum(float(g.get("latency_ratio") or 0.0) for g in groups)
        print()
        print("cover:")
        for g in groups:
            names = g.get("node_names") or []
            print(f"  ({','.join(names)}) ratio={float(g.get('latency_ratio') or 0.0):.4f}")
        print(f"total ratio = {total:.4f}")
    finally:
        store.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="engine", description="Kube Huddle -- core engine")
    sub = parser.add_subparsers(dest="cmd", required=True)

    serve = sub.add_parser("serve", help="run the FastAPI /api/v1 app")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    _add_db_flags(serve)
    serve.set_defaults(func=cmd_serve)

    init = sub.add_parser("init-db", help="create the SQLite schema (dev)")
    _add_db_flags(init)
    init.set_defaults(func=cmd_init_db)

    seed = sub.add_parser("seed", help="write a known-answer synthetic fixture")
    seed.add_argument("--fixture", default="fig2", help=f"one of: {sorted(FIXTURES)}")
    seed.add_argument("--cluster", default=None, help="cluster name (default = fixture name)")
    _add_db_flags(seed)
    seed.set_defaults(func=cmd_seed)

    run = sub.add_parser("run", help="run the latency head against a cluster")
    run.add_argument("--cluster", required=True, help="cluster name or id")
    run.add_argument("--name", default=None, help="analysis run name (default: auto-generated slug)")
    run.add_argument("--alpha", type=float, default=None)
    run.add_argument("--i-window", type=int, default=None)
    run.add_argument("--group-by", default=None)
    run.add_argument("--ttl-hours", type=int, default=24)
    _add_db_flags(run)
    run.set_defaults(func=cmd_run)

    return parser


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
