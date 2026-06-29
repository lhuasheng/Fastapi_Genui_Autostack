#!/usr/bin/env node
/**
 * gen-mapping.mjs — produce the data→UI mapping table from a backend api_graph
 * and a frontend catalog.
 *
 * Usage:
 *   node gen-mapping.mjs --api-graph api_graph.json --catalog catalog.json --out mapping.json
 */
import { readFileSync, writeFileSync, mkdirSync } from "fs";
import { resolve, dirname } from "path";

import { buildMapping } from "./lib/match.mjs";
import { assertValid } from "@genui/schema";

function parseArgs(argv) {
  const a = { out: "mapping.json" };
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === "--api-graph") a.apiGraph = argv[++i];
    else if (argv[i] === "--catalog") a.catalog = argv[++i];
    else if (argv[i] === "--out" || argv[i] === "-o") a.out = argv[++i];
    else if (argv[i] === "--help" || argv[i] === "-h") a.help = true;
  }
  return a;
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.help || !args.apiGraph || !args.catalog) {
    console.log("usage: node gen-mapping.mjs --api-graph <f> --catalog <f> [--out mapping.json]");
    process.exit(args.help ? 0 : 1);
  }
  const apiGraph = JSON.parse(readFileSync(resolve(process.cwd(), args.apiGraph), "utf8"));
  const catalog = JSON.parse(readFileSync(resolve(process.cwd(), args.catalog), "utf8"));

  const mapping = buildMapping(apiGraph, catalog);
  assertValid("mapping", mapping, "mapping");

  const outPath = resolve(process.cwd(), args.out);
  mkdirSync(dirname(outPath), { recursive: true });
  writeFileSync(outPath, JSON.stringify(mapping, null, 2));

  const endpoints = Object.keys(mapping.byEndpoint).length;
  const pairs = Object.values(mapping.byEndpoint).reduce((n, e) => n + e.compatibleBlocks.length, 0);
  console.error(`[ok] ${endpoints} endpoints → ${pairs} block matches across ${Object.keys(mapping.byBlock).length} kinds → ${args.out}`);
}

main();
