import { test } from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "url";
import { dirname, resolve } from "path";

import { extractFromDir, snake } from "../lib/extract.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const FIXTURES = resolve(HERE, "fixtures", "components");
const { components, errors } = extractFromDir(FIXTURES);
const byName = Object.fromEntries(components.map((c) => [c.name, c]));
const prop = (comp, name) => byName[comp].props.find((p) => p.name === name);

test("parses without errors", () => {
  assert.deepEqual(errors, []);
});

test("finds components, excludes non-components and test files", () => {
  const names = components.map((c) => c.name).sort();
  assert.deepEqual(names, ["ActionButton", "DataTable", "Heading", "Metric", "PlotlyChart"]);
  // capitalized non-component (returns a number) and __tests__ file are excluded
  assert.ok(!names.includes("ComputeTotal"));
  assert.ok(!names.includes("ShouldBeIgnored"));
});

test("snake-cases component name into a kind", () => {
  assert.equal(snake("MetricCard"), "metric_card");
  assert.equal(byName.PlotlyChart.kind, "plotly_chart");
});

test("Metric: required vs optional, union type, enum, defaults", () => {
  assert.deepEqual(prop("Metric", "label").schema, { type: "string" });
  assert.equal(prop("Metric", "label").required, true);
  assert.deepEqual(prop("Metric", "value").schema.type, ["string", "number"]);
  assert.equal(prop("Metric", "value").required, true);
  assert.equal(prop("Metric", "unit").required, false);
  assert.equal(prop("Metric", "unit").default, "");
  assert.deepEqual(prop("Metric", "variant").schema.enum, ["default", "compact", "hero"]);
  assert.equal(prop("Metric", "delta").required, false); // optional `?`, no default
});

test("Metric: prop descriptions come from interface JSDoc", () => {
  assert.match(prop("Metric", "label").description, /Metric name/);
});

test("DataTable: inline type literal, nested array items", () => {
  const rows = prop("DataTable", "rows").schema;
  assert.equal(rows.type, "array");
  assert.equal(rows.items.type, "array");
  assert.deepEqual(rows.items.items.type, ["string", "number"]);
  assert.equal(prop("DataTable", "columns").required, true);
  assert.equal(prop("DataTable", "caption").required, false);
});

test("Heading: untyped JS — defaults + JSDoc @param drive the contract", () => {
  assert.equal(byName.Heading.exported, true);
  assert.equal(prop("Heading", "text").required, true);
  assert.match(prop("Heading", "text").description, /heading text/i);
  assert.equal(prop("Heading", "level").default, 2);
  assert.equal(prop("Heading", "level").schema.type, "number"); // inferred from default
  assert.doesNotMatch(byName.Heading.description, /@param/); // tags stripped from description
});

test("ActionButton: React.FC<Props> + type-alias union resolves to enum", () => {
  assert.deepEqual(prop("ActionButton", "variant").schema.enum, ["primary", "secondary", "ghost"]);
  assert.equal(prop("ActionButton", "onClick").schema.type, "function");
  assert.equal(prop("ActionButton", "onClick").required, false);
  assert.equal(prop("ActionButton", "label").required, true);
});

test("PlotlyChart: typed identifier param, interface ref inlined to properties", () => {
  const figure = prop("PlotlyChart", "figure").schema;
  assert.equal(figure.type, "object");
  assert.ok(figure.properties.data && figure.properties.layout);
  assert.equal(prop("PlotlyChart", "figure").required, true);
  assert.equal(prop("PlotlyChart", "title").required, false);
});
