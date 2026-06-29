import { test } from "node:test";
import assert from "node:assert/strict";

import { blockCapabilities, buildMapping } from "../lib/match.mjs";
import { assertValid } from "@genui/schema";

// Inline catalog (A2UI-native kinds) — decoupled from the frontend generator.
const CATALOG = {
  blocks: {
    table: { component: "DataGrid", required: ["columns", "rows"], properties: {
      columns: { type: "array", items: { type: "string" } },
      rows: { type: "array", items: { type: "array" } } } },
    metric: { component: "KpiCard", required: ["label", "value"], properties: {
      label: { type: "string" }, value: { type: ["string", "number"] } } },
    plotly: { component: "ChartPanel", required: ["figure"], properties: { figure: { type: "object" } } },
    link: { component: "NavLink", required: ["label", "href"], properties: {
      label: { type: "string" }, href: { type: "string" } } },
    json_viewer: { component: "RawJson", required: ["data"], properties: { data: { type: "object" } } },
  },
};

const API_GRAPH = {
  nodes: [
    { name: "users", operations: [
      { slug: "list_users", role: "list", data_shape: {
        cardinality: "collection", hints: ["array_of_objects", "has_ids"],
        item_fields: { id: { type: "integer", role: "id" }, name: { type: "string", role: "label" } } } },
      { slug: "get_user", role: "detail", data_shape: {
        cardinality: "object", hints: ["has_ids"],
        item_fields: { id: { type: "integer", role: "id" }, capacity: { type: "number", role: "measure" } } } },
    ] },
    { name: "orders", operations: [
      { slug: "get_order", role: "detail", data_shape: {
        cardinality: "object", hints: ["has_ids"],
        item_fields: { id: { type: "integer", role: "id" }, total: { type: "number", role: "measure" }, user_id: { type: "integer", role: "id" } },
        ref_targets: [{ field: "user_id", resource: "users" }] } },
    ] },
    { name: "eis", operations: [
      { slug: "get_eis", role: "detail", data_shape: {
        cardinality: "object", hints: ["numeric_series"],
        item_fields: { zre: { type: "array" }, zim: { type: "array" } } } },
    ] },
  ],
  examples: {
    list_users: [{ id: 1, name: "Ada" }, { id: 2, name: "Bo" }],
    get_order: { id: 10, total: 9.5, user_id: 1 },
  },
};

const M = buildMapping(API_GRAPH, CATALOG);
const kindsFor = (slug) => M.byEndpoint[slug].compatibleBlocks.map((b) => b.kind);
const entry = (slug, kind) => M.byEndpoint[slug].compatibleBlocks.find((b) => b.kind === kind);

test("capabilities are inferred from required prop types, not kind names", () => {
  assert.ok(blockCapabilities(CATALOG.blocks.table).has("table"));
  assert.ok(blockCapabilities(CATALOG.blocks.metric).has("metric"));
  assert.ok(blockCapabilities(CATALOG.blocks.link).has("link"));
  assert.ok(blockCapabilities(CATALOG.blocks.json_viewer).has("viewer"));
  // a figure(object) block is a chart, NOT a generic viewer
  const chartCaps = blockCapabilities(CATALOG.blocks.plotly);
  assert.ok(chartCaps.has("chart") && !chartCaps.has("viewer"));
});

test("collection of objects → table (high)", () => {
  const t = entry("list_users", "table");
  assert.equal(t.confidence, "high");
  assert.deepEqual(t.example.columns, ["id", "name"]);
  assert.deepEqual(t.example.rows[0], [1, "Ada"]);
});

test("measure field → metric, grounded in the example", () => {
  const m = entry("get_order", "metric");
  assert.equal(m.confidence, "high");
  assert.equal(m.binding.value, "$.total");
  assert.deepEqual(m.example, { kind: "metric", label: "total", value: 9.5 });
});

test("reference relationship → link toward the target resource", () => {
  const l = entry("get_order", "link");
  assert.equal(l.confidence, "medium");
  assert.match(l.binding.href, /\/users\/\{\$\.user_id\}/);
  assert.equal(l.example.href, "/users/1");
});

test("numeric series → chart", () => {
  assert.ok(kindsFor("get_eis").includes("plotly"));
  assert.equal(entry("get_eis", "plotly").confidence, "medium");
});

test("every endpoint gets the json_viewer fallback (low)", () => {
  for (const slug of Object.keys(M.byEndpoint)) {
    assert.ok(kindsFor(slug).includes("json_viewer"), slug);
  }
  assert.equal(entry("get_user", "json_viewer").confidence, "low");
});

test("results are confidence-sorted and the inverse index is built", () => {
  const confs = M.byEndpoint.list_users.compatibleBlocks.map((b) => b.confidence);
  assert.deepEqual([...confs].sort((a, b) => ({ high: 3, medium: 2, low: 1 })[b] - ({ high: 3, medium: 2, low: 1 })[a]), confs);
  assert.ok(M.byBlock.table.includes("list_users"));
});

test("mapping validates against the shared schema", () => {
  assertValid("mapping", M);
});
