import { test } from "node:test";
import assert from "node:assert/strict";
import { validate, assertValid, schemaNames } from "../validate.mjs";

test("all four schemas compile and are resolvable", () => {
  assert.deepEqual(schemaNames, ["data-shape", "a2ui-block", "mapping", "manifest"]);
});

test("a valid data-shape passes", () => {
  const ds = {
    cardinality: "collection",
    item_fields: { id: { type: "integer", role: "id" }, name: { type: "string", role: "label" } },
    hints: ["array_of_objects", "has_ids"],
    ref_targets: [{ field: "user_id", resource: "users" }],
  };
  assertValid("data-shape", ds);
});

test("an invalid data-shape fails (bad cardinality, missing type)", () => {
  assert.equal(validate("data-shape", { cardinality: "list", item_fields: {}, hints: [] }).valid, false);
  assert.equal(validate("data-shape", { cardinality: "object", item_fields: { x: {} }, hints: [] }).valid, false);
});

test("mapping with a cross-ref data-shape validates", () => {
  const mapping = {
    byEndpoint: {
      list_users: {
        dataShape: { cardinality: "collection", item_fields: { id: { type: "integer" } }, hints: [] },
        compatibleBlocks: [
          { kind: "table", confidence: "high", reason: "array of objects", binding: { columns: "$[].keys" } },
        ],
      },
    },
    byBlock: { table: ["list_users"] },
  };
  assertValid("mapping", mapping);
});

test("a minimal manifest validates; missing a2ui fails", () => {
  const ok = {
    version: "1.0",
    tools: [],
    a2ui: { blocks: { table: { component: "DataTable", required: ["columns", "rows"], properties: {} } } },
    mapping: { byEndpoint: {}, byBlock: {} },
  };
  assertValid("manifest", ok);
  assert.equal(validate("manifest", { version: "1.0", tools: [], mapping: { byEndpoint: {}, byBlock: {} } }).valid, false);
});
