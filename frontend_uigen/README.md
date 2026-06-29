# frontend_uigen

Auto-generate an **AI-ready GenUI component catalogue** from React source — the
frontend parallel to [`backend_toolgen`](../backend_toolgen). Where `backend_toolgen`
turns an OpenAPI spec into AI tools, `frontend_uigen` turns a React component tree
into a machine-readable UI contract an LLM can use to emit valid render payloads.

> Catalogue : UI components :: OpenAPI : HTTP endpoints.

## What it produces

A single `catalog.json` whose contract is **fully auto-derived** from the components
(no hand-authored block schemas):

- `blocks` — one entry per component `kind` (snake-cased name): `description`,
  `required` fields, `properties` (typed), and a synthesized `example`.
- `blockSchema` — a JSON Schema (`allOf` of `if`/`then`) that validates any block payload.
- `composition` — generic rules for how blocks compose.
- `components` — raw inventory for developer reference.

It discovers components declared as function/arrow exports, default exports
(including anonymous ones, named from the file), and HOC wrappers
(`memo`, `forwardRef`, `observer`). The contract is derived from each
component's props, resolved (best-effort) from:

1. **TypeScript types** — inline type literals, named `interface`/`type` aliases
   (unions → `enum`, interfaces → nested `properties`), `React.FC<Props>`,
   `forwardRef<_, Props>`, and **types imported from other files** (followed one
   hop across relative imports).
2. **JSDoc** — `@param` tags and prop-level doc comments for descriptions.
3. **Default values** — inferred type + `default` from destructured defaults.

Two components that map to the same `kind` are reported under `collisions`
(and warned on stderr) rather than silently dropped.

### Known limitations

- Cross-file type resolution follows **one import hop**; multi-hop re-export
  chains (`export { X } from "./y"`) are the natural next enhancement.
- `interface extends` and intersection (`A & B`) members are not merged.
- Types imported from packages (non-relative) are not followed.

## Usage

```bash
npm install
node gen-catalog.mjs --src ./src --out ./catalog.json
node gen-catalog.mjs --src ./test/fixtures/components   # demo against fixtures
```

Flags: `--src <dir>` (required), `--out <file>` (default `catalog.json`), `--title <t>`.

## Layout

```
gen-catalog.mjs       CLI entry
lib/extract.mjs       walk + parse (Babel) + resolve component prop contracts
lib/tstype.mjs        TypeScript type node -> JSON-Schema-ish descriptor
lib/catalog.mjs       assemble blocks, blockSchema, examples, composition
test/                 node:test suite (+ ajv validation) and React fixtures
```

## Tests

```bash
npm test          # node --test
```

The suite asserts extraction (types, enums, required/optional, defaults, JSDoc) and
that every synthesized example validates against the generated `blockSchema` via `ajv`.
