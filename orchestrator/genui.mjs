#!/usr/bin/env node
/**
 * genui.mjs — one command for the whole pipeline.
 *
 *   1. backend_toolgen (Python)  → tools.json + api_graph.json (data tools + data shapes)
 *   2. frontend_uigen  (Node)    → catalog.json (A2UI block catalogue)
 *   3. bridge          (Node)    → mapping.json (data → block compatibility)
 *   4. merge + validate          → manifest.json (what the A2UI agent loads)
 *
 * Usage:
 *   node genui.mjs --spec openapi.json --components ../frontend/src --out ./genui_out
 *   node genui.mjs --spec spec.json --components ./c --out ./out \
 *       --stub --probe-replay --probe-cache ./cassettes --probe-base-url http://127.0.0.1:8077
 */
import { execFileSync } from "child_process";
import { readFileSync, writeFileSync, existsSync, mkdirSync } from "fs";
import { resolve, dirname } from "path";
import { fileURLToPath } from "url";

import { buildManifest } from "./lib/merge.mjs";
import { assertValid } from "@genui/schema";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(HERE, "..");

function parseArgs(argv) {
  const a = { out: "genui_out", passthrough: [] };
  const take = () => argv[++i];
  for (var i = 0; i < argv.length; i++) {
    const f = argv[i];
    if (f === "--spec" || f === "--file") a.spec = take();
    else if (f === "--url") a.url = take();
    else if (f === "--components") a.components = take();
    else if (f === "--out" || f === "-o") a.out = take();
    else if (f === "--python") a.python = take();
    else if (f === "--model") a.passthrough.push("--model", take());
    else if (f === "--stub") a.passthrough.push("--stub");
    else if (f === "--probe-replay") a.passthrough.push("--probe-replay");
    else if (f === "--probe") a.passthrough.push("--probe");
    else if (f === "--probe-cache") a.passthrough.push("--probe-cache", resolve(process.cwd(), take()));
    else if (f === "--probe-base-url") a.passthrough.push("--probe-base-url", take());
    else if (f === "--probe-auth") a.passthrough.push("--probe-auth", take());
    else if (f === "--help" || f === "-h") a.help = true;
  }
  return a;
}

function pickPython(explicit) {
  if (explicit) return explicit;
  const venv = resolve(ROOT, ".venv/bin/python");
  return existsSync(venv) ? venv : "python3";
}

function run(cmd, args, cwd) {
  console.error(`$ ${cmd} ${args.join(" ")}`);
  execFileSync(cmd, args, { cwd, stdio: ["ignore", "inherit", "inherit"] });
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.help || !args.components || (!args.spec && !args.url)) {
    console.log("usage: node genui.mjs (--spec <openapi.json> | --url <u>) --components <dir> [--out genui_out] [backend flags]");
    process.exit(args.help ? 0 : 1);
  }
  const out = resolve(process.cwd(), args.out);
  mkdirSync(out, { recursive: true });

  // 1. backend → tools.json + api_graph.json (written into `out`)
  const py = pickPython(args.python);
  const backendSrc = args.spec ? ["--file", resolve(process.cwd(), args.spec)] : ["--url", args.url];
  run(py, ["openapi_to_tools.py", ...backendSrc, "-o", out, ...args.passthrough], resolve(ROOT, "backend_toolgen"));

  // 2. frontend → catalog.json
  run("node", ["gen-catalog.mjs", "--src", resolve(process.cwd(), args.components), "--out", resolve(out, "catalog.json")],
    resolve(ROOT, "frontend_uigen"));

  // 3. bridge → mapping.json
  run("node", ["gen-mapping.mjs", "--api-graph", resolve(out, "api_graph.json"),
    "--catalog", resolve(out, "catalog.json"), "--out", resolve(out, "mapping.json")],
    resolve(ROOT, "bridge"));

  // 4. merge + validate
  const read = (f) => JSON.parse(readFileSync(resolve(out, f), "utf8"));
  const manifest = buildManifest({
    tools: read("tools.json"),
    catalog: read("catalog.json"),
    apiGraph: read("api_graph.json"),
    mapping: read("mapping.json"),
    source: { spec: args.spec ?? args.url, components: args.components },
  });
  assertValid("manifest", manifest, "manifest");
  writeFileSync(resolve(out, "manifest.json"), JSON.stringify(manifest, null, 2));

  const ep = Object.keys(manifest.mapping.byEndpoint).length;
  console.error(`\n[ok] manifest: ${manifest.tools.length} tools · ${Object.keys(manifest.a2ui.blocks).length} blocks · ` +
    `${ep} endpoints mapped → ${args.out}/manifest.json`);
}

main();
