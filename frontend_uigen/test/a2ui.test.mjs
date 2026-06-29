import { test } from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "url";
import { dirname, resolve } from "path";

import { extractFromDir } from "../lib/extract.mjs";
import { buildCatalog } from "../lib/catalog.mjs";
import { assertValid } from "@genui/schema";

const HERE = dirname(fileURLToPath(import.meta.url));
const { components } = extractFromDir(resolve(HERE, "fixtures", "a2ui"));
const catalog = buildCatalog(components, { title: "A2UI catalog" });
const byName = Object.fromEntries(components.map((c) => [c.name, c]));

test("@kind JSDoc overrides the snake(name) kind", () => {
  assert.equal(byName.KpiCard.kind, "metric");
  assert.equal(byName.DataGrid.kind, "table");
  assert.equal(byName.ChartPanel.kind, "plotly");
  assert.equal(byName.NavLink.kind, "link");
  assert.equal(byName.SectionTitle.kind, "heading");
  assert.equal(byName.RawJson.kind, "json_viewer");
});

test("catalog is keyed by A2UI kinds", () => {
  assert.deepEqual(
    Object.keys(catalog.blocks).sort(),
    ["heading", "json_viewer", "link", "metric", "plotly", "table"]
  );
});

test("every block conforms to the shared a2ui-block schema", () => {
  for (const [kind, block] of Object.entries(catalog.blocks)) {
    assertValid("a2ui-block", block, `block ${kind}`);
  }
});

test("kinds carry the prop contracts the bridge will match on", () => {
  assert.deepEqual(catalog.blocks.metric.required, ["label", "value"]);
  assert.deepEqual(catalog.blocks.table.required, ["columns", "rows"]);
  assert.equal(catalog.blocks.table.properties.columns.type, "array");
  assert.equal(catalog.blocks.link.properties.href.type, "string");
});
