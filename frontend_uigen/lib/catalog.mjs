/**
 * Assemble the AI-ready GenUI catalog from extracted components.
 *
 * The catalog is to UI components what OpenAPI is to HTTP endpoints: a
 * machine-readable contract an LLM reads to emit valid block payloads.
 *
 * Shape (a fresh, cleaner schema — not the legacy components.json):
 *   {
 *     blocks:      { <kind>: { component, description, required, properties, examples } },
 *     blockSchema: <JSON Schema oneOf over all kinds, for validation>,
 *     composition: <how blocks compose>,
 *     components:  <raw inventory for reference>,
 *   }
 */

const CATALOG_VERSION = "1.0";

/** Drop non-standard keys so blockSchema is valid JSON Schema for validators. */
function toJsonSchema(schema) {
  if (!schema || typeof schema !== "object") return {};
  const out = {};
  let { type } = schema;
  if (type === "any" || type === "function") type = undefined; // unconstrained / unvalidatable
  if (type === "node") type = undefined;
  if (type !== undefined) out.type = type;
  if (schema.enum) out.enum = schema.enum;
  if (schema.items) out.items = toJsonSchema(schema.items);
  if (schema.properties) {
    out.type = "object";
    out.properties = Object.fromEntries(
      Object.entries(schema.properties).map(([k, v]) => [k, toJsonSchema(v)])
    );
  }
  if (schema.anyOf) out.anyOf = schema.anyOf.map(toJsonSchema);
  return out;
}

/** A minimal placeholder value for a required prop, by schema. */
function placeholder(schema, name, depth = 0) {
  if (!schema || depth > 3) return null;
  if (schema.enum?.length) return schema.enum[0];
  switch (Array.isArray(schema.type) ? schema.type[0] : schema.type) {
    case "string": return name;
    case "number": return 0;
    case "boolean": return true;
    case "array": return schema.items ? [placeholder(schema.items, name, depth + 1)] : [];
    case "object":
      if (schema.properties)
        return Object.fromEntries(
          Object.entries(schema.properties).map(([k, v]) => [k, placeholder(v, k, depth + 1)])
        );
      return {};
    case "node": return "...";
    default: return name; // any / unknown — use the prop name as a readable hint
  }
}

function richProperty(p) {
  const out = { ...p.schema };
  if (p.description) out.description = p.description;
  if (p.default !== undefined) out.default = p.default;
  return out;
}

function synthExample(kind, props) {
  const ex = { kind };
  for (const p of props) {
    if (p.rest || !p.required) continue;
    if (p.schema?.type === "function") continue; // not expressible in JSON
    ex[p.name] = placeholder(p.schema, p.name);
  }
  return ex;
}

function buildBlock(c) {
  const fields = c.props.filter((p) => !p.rest);
  return {
    component: c.name,
    file: c.file,
    description: c.description,
    required: fields.filter((p) => p.required).map((p) => p.name),
    properties: Object.fromEntries(fields.map((p) => [p.name, richProperty(p)])),
    additionalProperties: c.props.some((p) => p.rest),
    examples: [synthExample(c.kind, c.props)],
  };
}

function buildBlockSchema(blocks) {
  const kinds = Object.keys(blocks);
  return {
    type: "object",
    required: ["kind"],
    properties: { kind: { type: "string", enum: kinds } },
    // allOf (not oneOf): each branch's `if` gates its `then`; non-matching
    // branches pass vacuously, so exactly the matching kind's rules apply.
    allOf: kinds.map((k) => {
      const b = blocks[k];
      return {
        if: { properties: { kind: { const: k } }, required: ["kind"] },
        then: {
          required: ["kind", ...b.required],
          properties: {
            kind: { const: k },
            ...Object.fromEntries(
              Object.entries(b.properties).map(([n, s]) => [n, toJsonSchema(s)])
            ),
          },
          additionalProperties: b.additionalProperties,
        },
      };
    }),
  };
}

const COMPOSITION = {
  description: "How catalog blocks compose into a render payload.",
  model:
    "The agent emits an ordered, flat list of block objects. Each block has a 'kind' plus that kind's fields. Blocks render top-to-bottom.",
  nesting: "Blocks do not nest — the list is flat. Use multiple blocks for sections.",
  best_practices: [
    "Lead with a heading-like block summarising the result.",
    "Group headline numbers (metric-like blocks) near the top.",
    "Follow with detail blocks (tables, charts).",
    "End with action/navigation blocks.",
    "Keep the payload focused — prefer ≤ 10 blocks.",
  ],
};

export function buildCatalog(components, { title = "GenUI Component Catalog", source = "", errors = [] } = {}) {
  // One block per kind. Collisions (two components -> same kind) are surfaced,
  // not silently dropped — a partial-but-quiet catalog is the worst outcome.
  const blocks = {};
  const owner = {};
  const collisions = [];
  for (const c of components) {
    if (blocks[c.kind]) {
      collisions.push({
        kind: c.kind,
        kept: { component: owner[c.kind].name, file: owner[c.kind].file },
        dropped: { component: c.name, file: c.file },
      });
      continue;
    }
    blocks[c.kind] = buildBlock(c);
    owner[c.kind] = c;
  }

  const typed = components.filter((c) => c.props.some((p) => p.schema?.type && p.schema.type !== "any")).length;

  return {
    $schema: "https://json-schema.org/draft/2020-12/schema",
    title,
    description:
      "Machine-readable contract for a GenUI render tool. An LLM reads this to emit valid block payloads that render as UI. Auto-generated from React source.",
    version: CATALOG_VERSION,
    generated: new Date().toISOString(),
    ...(source && { source }),

    renderTool: {
      description: "Agent calls render(title: str, blocks: list[dict]). Each block has 'kind' + that kind's fields.",
      parameters: {
        title: { type: "string", description: "Surface title shown at the top" },
        blocks: {
          type: "array",
          description: "Ordered list of block objects, each validated by blockSchema.",
          items: { $ref: "#/blockSchema" },
        },
      },
    },

    blockSchema: buildBlockSchema(blocks),
    blocks,
    composition: COMPOSITION,

    components: {
      description: "Raw component inventory (developer reference). The AI contract is `blocks`.",
      total: components.length,
      items: components.map((c) => ({
        name: c.name,
        kind: c.kind,
        file: c.file,
        exported: c.exported,
        description: c.description,
        props: c.props,
      })),
    },

    stats: {
      components: components.length,
      blocks: Object.keys(blocks).length,
      typedComponents: typed,
      ...(collisions.length && { kindCollisions: collisions.length }),
    },
    ...(collisions.length && { collisions }),
    ...(errors.length && { parseErrors: errors }),
  };
}
