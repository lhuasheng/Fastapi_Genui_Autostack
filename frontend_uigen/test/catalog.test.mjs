import { test } from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "url";
import { dirname, resolve } from "path";
import Ajv from "ajv/dist/2020.js";

import { extractFromDir } from "../lib/extract.mjs";
import { buildCatalog } from "../lib/catalog.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const { components } = extractFromDir(resolve(HERE, "fixtures", "components"));
const catalog = buildCatalog(components, { title: "Test Catalog", source: "fixtures" });

const ajv = new Ajv({ allErrors: true, strict: false });
const validate = ajv.compile(catalog.blockSchema);

test("catalog has one block per unique kind", () => {
  assert.deepEqual(
    Object.keys(catalog.blocks).sort(),
    ["action_button", "data_table", "heading", "metric", "plotly_chart"]
  );
  assert.equal(catalog.stats.blocks, 5);
});

test("blockSchema uses allOf-of-if/then (not the buggy oneOf)", () => {
  assert.ok(Array.isArray(catalog.blockSchema.allOf));
  assert.ok(!catalog.blockSchema.oneOf);
});

test("every synthesized example validates against blockSchema", () => {
  for (const [kind, block] of Object.entries(catalog.blocks)) {
    for (const ex of block.examples) {
      assert.ok(validate(ex), `${kind} example invalid: ${ajv.errorsText(validate.errors)}`);
    }
  }
});

test("required fields are enforced", () => {
  assert.ok(!validate({ kind: "metric", label: "Cap" })); // missing required `value`
  assert.ok(validate({ kind: "metric", label: "Cap", value: 3654 }));
});

test("unknown kind is rejected", () => {
  assert.ok(!validate({ kind: "does_not_exist", foo: 1 }));
});

test("undeclared props are rejected (additionalProperties: false)", () => {
  assert.ok(!validate({ kind: "metric", label: "Cap", value: 1, bogus: true }));
});

test("enum constraint is enforced", () => {
  assert.ok(!validate({ kind: "metric", label: "C", value: 1, variant: "nope" }));
  assert.ok(validate({ kind: "metric", label: "C", value: 1, variant: "hero" }));
});

test("renderTool + composition are present for the agent", () => {
  assert.equal(catalog.renderTool.parameters.blocks.items.$ref, "#/blockSchema");
  assert.ok(catalog.composition.best_practices.length > 0);
});
