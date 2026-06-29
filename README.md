# GenUI pipeline

Write backend functions that reveal data — get AI **data tools** *and* an auto-wired
**GenUI rendering layer** for free. You never hand-write the data→UI bindings.

```
backend functions ──▶ backend_toolgen ──▶ tools.json + api_graph.json (data shapes, relationships)
                                                  │
React components ──▶ frontend_uigen ──▶ catalog.json (A2UI block contracts)
                                                  │
                                   bridge ──▶ mapping.json  (which block renders which data)
                                                  │
                               orchestrator ──▶ manifest.json  ◀── what the A2UI agent loads
```

**The unifying idea:** both generators emit *typed schemas*, so wiring them is a
**type-driven match** — an endpoint's data shape (collection / object / measures / ids /
relationships) is paired with the UI block whose required props that data can satisfy. The
result is a *mapping table* (compatible blocks + binding hints + a grounded example); the
agent picks at render time.

## Packages (npm workspaces + shared schema)

| Package | Lang | Role |
|---|---|---|
| `schema/` | — | Source-of-truth JSON Schemas (`manifest`, `a2ui-block`, `data-shape`, `mapping`) + ajv helpers |
| `backend_toolgen/` | Python | OpenAPI → AI tools; live read-only probe → `api_graph.json` with a `data_shape` per endpoint |
| `frontend_uigen/` | Node | React → A2UI-native `catalog.json` (kinds via `@kind`, typed props) |
| `bridge/` | Node | Type-driven matcher → `mapping.json` |
| `orchestrator/` | Node | One command → merged, schema-validated `manifest.json` |

## Quick start

```bash
npm install                      # links workspaces (Node); Python uses .venv
node orchestrator/genui.mjs \
  --spec path/to/openapi.json \
  --components path/to/react/src \
  --out ./genui_out
# add: --probe --probe-base-url <url> --probe-auth bearer:$TOK   (live data)
#  or: --stub --probe-replay --probe-cache <cassettes> ...        (deterministic/offline)
```

Output `genui_out/manifest.json` gives the agent, all auto-derived:
**`tools`** (fetch data) · **`a2ui.blocks`** (render) · **`mapping`** (which block per data shape).

## Tests

```bash
npm test                                   # all Node workspaces
.venv/bin/python backend_toolgen/tests/test_data_shape.py   # backend (one of several)
```

A representative end-to-end result on the fixtures: `get_order` (a record with a `total`
measure and a `user_id` foreign key) auto-maps to a **metric** for the total and a **link**
to `/users/{user_id}` — a cross-resource drill-down derived purely from the backend.
# Fastapi_Genui_Autostack
