/**
 * The bridge: match each endpoint's `data_shape` (from api_graph) to the UI blocks
 * (from catalog) whose required props that data can satisfy.
 *
 * Matching is *type-driven and catalog-agnostic*: a block's capability is inferred
 * from the JSON types of its required props, not from its kind name. The output is a
 * mapping TABLE (compatible options + binding hints + a grounded example) — the agent
 * picks at render time. See schema/mapping.schema.json.
 */

// ── type predicates over a block prop's schema ────────────────────────────────
const typeArr = (s) => (Array.isArray(s?.type) ? s.type : [s?.type]);
const isArr = (s) => s?.type === "array";
const isArrOfArr = (s) => isArr(s) && s.items?.type === "array";
const isArrOfStr = (s) => isArr(s) && (s.items?.type === "string" || typeArr(s.items).includes("string"));
const isArrOfNum = (s) => isArr(s) && (s.items?.type === "number" || s.items?.type === "integer");
const isStr = (s) => typeArr(s).includes("string") && !isArr(s);
const isNumish = (s) => typeArr(s).some((t) => t === "number" || t === "integer");
const isUnionStrNum = (s) => typeArr(s).includes("string") && typeArr(s).includes("number");
const isObj = (s) => s?.type === "object";

const TARGET_RE = /href|url|link|to|path|id/i;
const DATA_RE = /data|json|payload|raw|value/i;

const reqEntries = (block) => (block.required || []).map((n) => [n, (block.properties || {})[n] || {}]);

/** Capabilities a block can fulfil, inferred from its required prop types. */
export function blockCapabilities(block) {
  const e = reqEntries(block);
  const caps = new Set();

  if (e.some(([, s]) => isArrOfArr(s)) && e.some(([, s]) => isArrOfStr(s))) caps.add("table");

  const hasValue = e.some(([, s]) => isNumish(s) || isUnionStrNum(s));
  const hasLabel = e.some(([, s]) => isStr(s));
  if (!caps.has("table") && e.length >= 2 && e.length <= 3 && hasLabel && hasValue) caps.add("metric");

  if (e.some(([, s]) => isObj(s)) || e.some(([, s]) => isArrOfNum(s))) caps.add("chart");

  if (e.length >= 1 && e.length <= 2 && e.every(([, s]) => isStr(s)) && e.some(([n]) => TARGET_RE.test(n)))
    caps.add("link");

  if (e.length === 1 && (isObj(e[0][1]) || e[0][1].type === "any" || e[0][1].type === undefined) && DATA_RE.test(e[0][0]))
    caps.add("viewer");

  return caps;
}

// ── prop-name pickers (which actual prop plays each role) ──────────────────────
const pick = (block, pred, fallbackIdx = 0) => {
  const e = reqEntries(block);
  return (e.find(([, s]) => pred(s)) || e[fallbackIdx] || e[0])[0];
};
const tableProps = (b) => ({ cols: pick(b, isArrOfStr), rows: pick(b, isArrOfArr, 1) });
const chartProp = (b) => pick(b, isObj);
const viewerProp = (b) => (b.required || [])[0];
function metricProps(b) {
  const e = reqEntries(b);
  const valueE = e.find(([, s]) => isNumish(s) || isUnionStrNum(s));
  const labelE = e.find(([n, s]) => isStr(s) && n !== valueE?.[0]) || e[0];
  return { label: labelE[0], value: (valueE || e[1] || e[0])[0] };
}
function linkProps(b) {
  const e = reqEntries(b);
  const target = e.find(([n]) => TARGET_RE.test(n)) || e[0];
  const label = e.find(([n, s]) => n !== target[0] && isStr(s));
  return { target: target[0], label: label ? label[0] : null };
}

// ── example helpers ───────────────────────────────────────────────────────────
const exampleItem = (ex) => (Array.isArray(ex) ? ex.find((x) => x && typeof x === "object") ?? ex[0] : ex);
const RANK = { high: 3, medium: 2, low: 1 };

function matchBlock(kind, block, caps, ds, example, resource) {
  const out = [];
  const fields = ds.item_fields || {};
  const roleFields = (r) => Object.entries(fields).filter(([, f]) => f.role === r).map(([n]) => n);
  const measures = roleFields("measure");
  const ids = roleFields("id");
  const labels = roleFields("label");
  const item = exampleItem(example);
  const hint = (h) => (ds.hints || []).includes(h);

  if (caps.has("table") && ds.cardinality === "collection" && hint("array_of_objects")) {
    const { cols, rows } = tableProps(block);
    const entry = { kind, confidence: "high", reason: "collection of objects → table",
      binding: { [cols]: "$[].keys", [rows]: "$[].values" } };
    if (Array.isArray(example) && item && typeof item === "object") {
      const keys = Object.keys(item);
      entry.example = { kind, [cols]: keys, [rows]: example.slice(0, 3).map((r) => keys.map((k) => r?.[k])) };
    }
    out.push(entry);
  }

  if (caps.has("metric") && measures.length) {
    const { label, value } = metricProps(block);
    const confidence = ds.cardinality === "object" ? "high" : "medium";
    const path = (f) => (ds.cardinality === "object" ? `$.${f}` : `$[].${f}`);
    for (const m of measures) {
      const entry = { kind, confidence, reason: `measure '${m}' → metric`,
        binding: { [label]: `const:${m}`, [value]: path(m) } };
      if (item && item[m] !== undefined) entry.example = { kind, [label]: m, [value]: item[m] };
      out.push(entry);
    }
  }

  if (caps.has("chart") && hint("numeric_series")) {
    out.push({ kind, confidence: "medium", reason: "numeric series → chart",
      binding: { [chartProp(block)]: "$" } });
  }

  if (caps.has("link") && (ids.length || (ds.ref_targets || []).length)) {
    const { target, label } = linkProps(block);
    const ref = (ds.ref_targets || [])[0];
    const idField = ref?.field || ids[0];
    const refRes = ref?.resource || resource;
    const binding = { [target]: `route:/${refRes}/{$.${idField}}` };
    if (label) binding[label] = labels.length ? `$.${labels[0]}` : `const:Open ${refRes}`;
    const entry = { kind, confidence: ref ? "medium" : "low",
      reason: ref ? `references ${refRes} → link` : "has id → link", binding };
    if (item && item[idField] !== undefined) {
      entry.example = { kind, [target]: `/${refRes}/${item[idField]}` };
      if (label) entry.example[label] = labels.length ? item[labels[0]] : `Open ${refRes}`;
    }
    out.push(entry);
  }

  if (caps.has("viewer")) {
    const data = viewerProp(block);
    const entry = { kind, confidence: "low", reason: "generic fallback", binding: { [data]: "$" } };
    if (example !== undefined) entry.example = { kind, [data]: item ?? example };
    out.push(entry);
  }

  return out;
}

export function buildMapping(apiGraph, catalog) {
  const blocks = catalog.blocks || {};
  const caps = Object.fromEntries(
    Object.entries(blocks).map(([kind, b]) => [kind, { block: b, caps: blockCapabilities(b) }])
  );
  const examples = apiGraph.examples || {};

  const byEndpoint = {};
  const byBlock = {};
  for (const node of apiGraph.nodes || []) {
    for (const op of node.operations || []) {
      const ds = op.data_shape;
      if (!ds) continue;
      const compatible = [];
      for (const [kind, { block, caps: cap }] of Object.entries(caps)) {
        compatible.push(...matchBlock(kind, block, cap, ds, examples[op.slug], node.name));
      }
      compatible.sort((a, b) => RANK[b.confidence] - RANK[a.confidence]);
      byEndpoint[op.slug] = { dataShape: ds, compatibleBlocks: compatible };
      for (const e of compatible) (byBlock[e.kind] ??= []).push(op.slug);
    }
  }
  for (const k of Object.keys(byBlock)) byBlock[k] = [...new Set(byBlock[k])];
  return { byEndpoint, byBlock };
}
