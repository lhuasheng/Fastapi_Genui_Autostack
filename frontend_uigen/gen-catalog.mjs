#!/usr/bin/env node
/**
 * gen-catalog.mjs — auto-generate an AI-ready GenUI component catalogue.
 *
 * The frontend parallel to backend_toolgen's openapi_to_tools: instead of
 * turning an OpenAPI spec into AI tools, it turns a React component tree into
 * a machine-readable UI contract an LLM can use to emit valid render payloads.
 *
 * Usage:
 *   node gen-catalog.mjs --src ./src --out ./catalog.json
 *   node gen-catalog.mjs --src ./test/fixtures/components            # defaults out to ./catalog.json
 *   node gen-catalog.mjs --src ./src --title "My App UI Catalog"
 */
import { writeFileSync, mkdirSync } from "fs";
import { resolve, dirname } from "path";

import { extractFromDir } from "./lib/extract.mjs";
import { buildCatalog } from "./lib/catalog.mjs";

function parseArgs(argv) {
  const args = { src: null, out: "catalog.json", title: "GenUI Component Catalog" };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--src") args.src = argv[++i];
    else if (a === "--out" || a === "-o") args.out = argv[++i];
    else if (a === "--title") args.title = argv[++i];
    else if (a === "--help" || a === "-h") args.help = true;
  }
  return args;
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.help || !args.src) {
    console.log("usage: node gen-catalog.mjs --src <dir> [--out catalog.json] [--title <t>]");
    process.exit(args.help ? 0 : 1);
  }

  const srcDir = resolve(process.cwd(), args.src);
  const outPath = resolve(process.cwd(), args.out);

  const { components, errors } = extractFromDir(srcDir);
  const catalog = buildCatalog(components, { title: args.title, source: args.src, errors });

  mkdirSync(dirname(outPath), { recursive: true });
  writeFileSync(outPath, JSON.stringify(catalog, null, 2));

  for (const c of catalog.collisions ?? []) {
    console.error(
      `[warn] kind "${c.kind}" collision: kept ${c.kept.component} (${c.kept.file}), ` +
        `dropped ${c.dropped.component} (${c.dropped.file})`
    );
  }
  console.error(
    `[ok] ${catalog.stats.components} components → ${catalog.stats.blocks} block kinds` +
      `${catalog.collisions ? `, ${catalog.collisions.length} kind collisions` : ""}` +
      `${errors.length ? `, ${errors.length} parse errors` : ""} → ${args.out}`
  );
}

main();
