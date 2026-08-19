# UI (static HTML + vanilla JS)

Single-page wizard: connect cluster → pick pods → set sources → view detected
islands + recommendations. No build step, no framework, no external libraries.

## Layout

```
ui/
├── index.html            # the whole app: shell + 4 screens + modals + JS
├── nginx.default.conf    # standalone nginx serves index.html and proxies /api/ to the engine
└── Dockerfile            # nginx-unprivileged base, listens :8080
```

## Serve locally

Two options:

- **From the engine** — set `KUBEHUDDLE_UI_DIR=../ui` before `kubehuddle-engine serve`. The engine mounts the static files at `/`. This is the friendliest dev loop.
- **From nginx** — `docker run --rm -p 8080:8080 kubehuddle/ui:0.1.0`. The nginx config proxies `/api/` to the engine, so it needs the engine reachable at the address in the config (see `nginx.default.conf`).

## API contract the UI depends on

Everything the UI needs is in [`docs/api.md`](../docs/api.md). Screen 4 in
particular pulls from four endpoints:

- `GET /runs/{id}/groups` → orb rendering
- `GET /runs/{id}/graph` → optional weighing-engine graph (not used in the current build)
- `GET /runs/{id}/recommendations` → wide recommendation rows
- `GET /runs/{id}/recommendations/{recId}/evidence` → Why force-graph + peers + nodeAffinity snippet

## Extension points

- **API base URL** — override with `window.KUBEHUDDLE_API = '<url>'` before `index.html` loads. Default: `/api/v1`.
- **Grouping granularity** — the top segmented control maps to `config.group_by`. The engine's placement resolver honours `'node'` today; a `'zone'` topology label is on the roadmap.
- **Colours** — the palette is defined in `<style>` at the top of `index.html`. The orb palette (per-group colours) is the `COLORS` array in the `<script>` block.

## What's client-side only

- **APPLY / DROP** on recommendation cards are pure state mutations — they never hit the server. APPLY moves the workload's mini-orb into the target island; DROP hides the row and reverses the APPLY.
- **The orb + Why force-graph** are self-contained SVG (a small pack/force relaxation), no D3.

## Design notes

- Every image, font, and script is inline — the UI works fully air-gapped.
- The topbar's env chip *"read-only · never applies to the cluster"* is intentional; it should stay visible on every screen.
- Two design references live in `../design/`: `ui-mockup.html` is the reference for the final UI (visual), and `orb-options.html` explored four density models for the orb before Option C (node-aggregate + drill-down) was picked.
