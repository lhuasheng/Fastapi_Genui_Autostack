import { test } from "node:test";
import assert from "node:assert/strict";

import { buildManifest } from "../lib/merge.mjs";
import { assertValid } from "@genui/schema";

const tools = [{ name: "users", description: "…", input_schema: { type: "object" } }];
const catalog = {
  renderTool: { description: "render(title, blocks)" },
  blocks: { metric: { component: "KpiCard", required: ["label", "value"], properties: {} } },
  blockSchema: { type: "object" },
  composition: { description: "flat" },
  collisions: [{ kind: "widget", kept: {}, dropped: {} }],
};
const apiGraph = {
  nodes: [{ name: "users", operations: [
    { slug: "list_users", data_shape: { cardinality: "collection", item_fields: {}, hints: [] } },
  ] }],
  probe: { stats: { network_calls: 0 } },
};
const mapping = { byEndpoint: { list_users: { compatibleBlocks: [{ kind: "metric", confidence: "low" }] } }, byBlock: { metric: ["list_users"] } };

const manifest = buildManifest({ tools, catalog, apiGraph, mapping, source: { spec: "s.json", components: "./c" } });

test("manifest carries tools, a2ui blocks, dataShapes, mapping", () => {
  assert.equal(manifest.tools.length, 1);
  assert.ok(manifest.a2ui.blocks.metric);
  assert.deepEqual(manifest.dataShapes.list_users.cardinality, "collection");
  assert.ok(manifest.mapping.byEndpoint.list_users);
});

test("provenance gathers probe stats + collisions", () => {
  assert.equal(manifest.provenance.probe_stats.network_calls, 0);
  assert.equal(manifest.provenance.collisions.length, 1);
});

test("merged manifest validates against the shared schema", () => {
  assertValid("manifest", manifest);
});
