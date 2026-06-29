import { test } from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "url";
import { dirname, resolve } from "path";

import { extractFromDir } from "../lib/extract.mjs";
import { buildCatalog } from "../lib/catalog.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const { components, errors } = extractFromDir(resolve(HERE, "fixtures", "advanced"));
const byName = Object.fromEntries(components.map((c) => [c.name, c]));
const prop = (comp, name) => byName[comp]?.props.find((p) => p.name === name);

test("parses without errors", () => {
  assert.deepEqual(errors, []);
});

test("fix #1: HOC-wrapped components are found", () => {
  assert.ok(byName.MemoCard, "memo() component missing");
  assert.ok(byName.RefCard, "forwardRef() component missing");
  // memo: props typed on the inner arrow
  assert.equal(prop("MemoCard", "label").required, true);
  assert.deepEqual(prop("MemoCard", "tone").schema.enum, ["a", "b"]);
  assert.equal(prop("MemoCard", "tone").default, "a");
  // forwardRef: props come from the 2nd generic arg
  assert.equal(prop("RefCard", "label").schema.type, "string");
  assert.equal(prop("RefCard", "label").required, true);
});

test("fix #1: anonymous default export is named from the file", () => {
  assert.ok(byName.PageBanner, "anonymous default component missing");
  assert.equal(prop("PageBanner", "heading").required, true);
  assert.equal(prop("PageBanner", "sticky").schema.type, "boolean");
  assert.equal(prop("PageBanner", "sticky").required, false);
});

test("enhancement: types imported from another file resolve", () => {
  assert.ok(byName.Card);
  assert.equal(prop("Card", "id").schema.type, "string");
  assert.match(prop("Card", "id").description, /Stable id/); // JSDoc from imported interface
  assert.equal(prop("Card", "title").required, true);
  // `size?: Size` — a union alias local to the imported file — resolves to an enum
  assert.deepEqual(prop("Card", "size").schema.enum, ["sm", "md", "lg"]);
  assert.equal(prop("Card", "size").required, false);
});

test("fix #2: kind collisions are surfaced, not silently dropped", () => {
  const catalog = buildCatalog(components, { source: "advanced" });
  assert.ok(catalog.collisions?.length >= 1, "collision not reported");
  const widget = catalog.collisions.find((c) => c.kind === "widget");
  assert.ok(widget && widget.kept.component === "Widget" && widget.dropped.component === "Widget");
  assert.notEqual(widget.kept.file, widget.dropped.file);
  assert.equal(catalog.stats.kindCollisions, catalog.collisions.length);
  // both colliding components still appear in the raw inventory
  assert.equal(components.filter((c) => c.kind === "widget").length, 2);
});
