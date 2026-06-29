/**
 * Shared schema validation for the GenUI pipeline.
 *
 * Loads all four schemas into one Ajv instance so cross-`$ref`s resolve, and
 * exposes helpers keyed by short name: "data-shape" | "a2ui-block" | "mapping" | "manifest".
 */
import { readFileSync } from "fs";
import { resolve, dirname } from "path";
import { fileURLToPath } from "url";
import Ajv from "ajv/dist/2020.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const NAMES = ["data-shape", "a2ui-block", "mapping", "manifest"];
const BASE = "https://genui.dev/schema/";

const ajv = new Ajv({ allErrors: true, strict: false });
for (const name of NAMES) {
  ajv.addSchema(JSON.parse(readFileSync(resolve(HERE, `${name}.schema.json`), "utf8")));
}

/** Compiled validator for a schema by short name. */
export function validator(name) {
  const v = ajv.getSchema(`${BASE}${name}.json`);
  if (!v) throw new Error(`unknown schema: ${name}`);
  return v;
}

/** Returns { valid, errors } without throwing. */
export function validate(name, data) {
  const v = validator(name);
  return { valid: !!v(data), errors: v.errors ?? null };
}

/** Throws with a readable message if `data` does not conform to schema `name`. */
export function assertValid(name, data, label = "data") {
  const v = validator(name);
  if (!v(data)) throw new Error(`${label} invalid vs ${name}: ${ajv.errorsText(v.errors)}`);
  return data;
}

export const schemaNames = NAMES;
