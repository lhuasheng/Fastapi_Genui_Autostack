/**
 * Component extraction over a React source tree.
 *
 * For each component (capitalized function/arrow/HOC that returns JSX) we
 * resolve its prop contract from, in order of preference:
 *   1. TypeScript types  — inline literal, named interface/alias, React.FC<Props>,
 *                          forwardRef<_, Props>, and types imported from other files
 *   2. JSDoc @param tags  — for untyped JS components
 *   3. default values     — inferred from the destructured default literal
 *
 * Output is a flat list of component descriptors consumed by lib/catalog.mjs.
 */
import { readFileSync, readdirSync, statSync, existsSync } from "fs";
import { resolve, relative, dirname, basename } from "path";
import { parse } from "@babel/parser";

import { tsTypeToSchema, typeText } from "./tstype.mjs";

const SOURCE_EXTS = [".jsx", ".tsx", ".js", ".ts"];
const RESOLVE_EXTS = [".ts", ".tsx", ".d.ts", ".js", ".jsx"];

export function snake(s) {
  return String(s)
    .replace(/([a-z0-9])([A-Z])/g, "$1_$2")
    .replace(/[^\w]+/g, "_")
    .replace(/_+/g, "_")
    .replace(/^_|_$/g, "")
    .toLowerCase();
}

export function walkFiles(dir, exts = SOURCE_EXTS) {
  const out = [];
  for (const entry of readdirSync(dir)) {
    const full = resolve(dir, entry);
    if (statSync(full).isDirectory()) {
      if (entry === "__tests__" || entry === "node_modules" || entry === ".git") continue;
      out.push(...walkFiles(full, exts));
    } else if (
      exts.some((e) => entry.endsWith(e)) &&
      !entry.includes(".test.") &&
      !entry.includes(".spec.") &&
      !entry.endsWith(".d.ts")
    ) {
      out.push(full);
    }
  }
  return out;
}

export function parseFile(path) {
  return parse(readFileSync(path, "utf8"), {
    sourceType: "module",
    plugins: ["jsx", "typescript"],
    errorRecovery: true,
  });
}

const isComponentName = (n) => n && /^[A-Z]/.test(n);

function nameFromFile(p) {
  const base = basename(p).replace(/\.(jsx?|tsx?)$/, "");
  const pascal = base
    .split(/[^A-Za-z0-9]/)
    .filter(Boolean)
    .map((s) => s[0].toUpperCase() + s.slice(1))
    .join("");
  return isComponentName(pascal) ? pascal : "Default";
}

/** Bounded recursive scan: does this subtree contain JSX? */
function containsJSX(node, budget = { n: 4000 }) {
  if (!node || typeof node !== "object" || budget.n-- <= 0) return false;
  if (node.type === "JSXElement" || node.type === "JSXFragment") return true;
  for (const k of Object.keys(node)) {
    if (k === "leadingComments" || k === "trailingComments" || k === "loc" || k === "type") continue;
    const v = node[k];
    if (Array.isArray(v)) {
      for (const c of v) if (c && typeof c.type === "string" && containsJSX(c, budget)) return true;
    } else if (v && typeof v.type === "string") {
      if (containsJSX(v, budget)) return true;
    }
  }
  return false;
}

function serializeDefault(node) {
  if (!node) return undefined;
  switch (node.type) {
    case "StringLiteral": return node.value;
    case "NumericLiteral": return node.value;
    case "BooleanLiteral": return node.value;
    case "NullLiteral": return null;
    case "ArrayExpression": return [];
    case "ObjectExpression": return {};
    case "Identifier": return node.name === "undefined" ? undefined : node.name;
    case "UnaryExpression":
      if (node.operator === "-" && node.argument?.type === "NumericLiteral") return -node.argument.value;
      return undefined;
    default: return undefined;
  }
}

function schemaFromDefault(def) {
  if (def === undefined) return null;
  if (typeof def === "string") return { type: "string" };
  if (typeof def === "number") return { type: "number" };
  if (typeof def === "boolean") return { type: "boolean" };
  if (Array.isArray(def)) return { type: "array" };
  if (def === null) return { type: "null" };
  if (typeof def === "object") return { type: "object" };
  return null;
}

/** Split a JSDoc block into clean per-line text (leading `*` stripped). */
function blockLines(value) {
  return value.split("\n").map((l) => l.replace(/^\s*\*+/, "").trim());
}

function componentDoc(comments) {
  const block = (comments ?? []).filter((c) => c.type === "CommentBlock").at(-1);
  if (!block) return "";
  // Drop @param/@returns tag lines from the human description.
  return blockLines(block.value)
    .filter((l) => l && !l.startsWith("@"))
    .join(" ")
    .replace(/\s+/g, " ")
    .trim();
}

/** Explicit A2UI kind override, e.g. `@kind metric`, from a component's JSDoc. */
function parseKindTag(comments) {
  for (const c of comments ?? []) {
    if (c.type !== "CommentBlock") continue;
    const m = /@kind\s+([\w-]+)/.exec(c.value);
    if (m) return snake(m[1]);
  }
  return null;
}

function parseParamTags(comments) {
  const out = {};
  for (const c of comments ?? []) {
    if (c.type !== "CommentBlock") continue;
    const re = /@param\s+(?:\{([^}]+)\}\s+)?(\w+)\s*-?\s*(.*)/g;
    let m;
    while ((m = re.exec(c.value)) !== null) {
      const [, type, name, desc] = m;
      out[name] = { type: type?.trim(), description: desc?.trim() ?? "" };
    }
  }
  return out;
}

// ── Type index + cross-file resolution ───────────────────────────────────────

/**
 * Index named types in a file:
 *   interface / object type-alias -> { kind: "members", members }
 *   any other type-alias (union, scalar, etc.) -> { kind: "type", node }
 */
function collectTypeDecls(ast) {
  const idx = {};
  const visit = (node) => {
    if (node.type === "TSInterfaceDeclaration") idx[node.id.name] = { kind: "members", members: node.body.body };
    if (node.type === "TSTypeAliasDeclaration") {
      idx[node.id.name] = node.typeAnnotation?.type === "TSTypeLiteral"
        ? { kind: "members", members: node.typeAnnotation.members }
        : { kind: "type", node: node.typeAnnotation };
    }
  };
  for (const stmt of ast.program.body) {
    visit(stmt);
    if (stmt.type === "ExportNamedDeclaration" && stmt.declaration) visit(stmt.declaration);
  }
  return idx;
}

/** local imported name -> { source } for following type references across files. */
function collectImports(ast) {
  const imports = {};
  for (const stmt of ast.program.body) {
    if (stmt.type !== "ImportDeclaration") continue;
    const source = stmt.source.value;
    for (const spec of stmt.specifiers) {
      if (spec.type === "ImportSpecifier" || spec.type === "ImportDefaultSpecifier") {
        imports[spec.local.name] = { source };
      }
    }
  }
  return imports;
}

function resolveModule(spec, fromAbsPath) {
  if (!spec.startsWith(".")) return null; // bare/package import — don't follow
  const base = resolve(dirname(fromAbsPath), spec);
  const candidates = [base, ...RESOLVE_EXTS.map((e) => base + e), ...RESOLVE_EXTS.map((e) => resolve(base, "index" + e))];
  return candidates.find((p) => existsSync(p) && statSync(p).isFile()) ?? null;
}

/**
 * Resolves type names within a file and one hop across local imports.
 * Caches each file's { types, imports } by absolute path.
 */
function createResolver() {
  const cache = new Map();
  const indexOf = (absPath) => {
    if (cache.has(absPath)) return cache.get(absPath);
    let res;
    try {
      const ast = parseFile(absPath);
      res = { types: collectTypeDecls(ast), imports: collectImports(ast) };
    } catch {
      res = { types: {}, imports: {} };
    }
    cache.set(absPath, res);
    return res;
  };
  return {
    prime(absPath, ast) {
      cache.set(absPath, { types: collectTypeDecls(ast), imports: collectImports(ast) });
    },
    /** @returns {{entry, definedIn}|null} */
    lookup(name, ctxPath) {
      if (!name) return null;
      const fi = indexOf(ctxPath);
      if (fi.types[name]) return { entry: fi.types[name], definedIn: ctxPath };
      const imp = fi.imports[name];
      if (imp) {
        const target = resolveModule(imp.source, ctxPath);
        if (target) {
          const tfi = indexOf(target);
          if (tfi.types[name]) return { entry: tfi.types[name], definedIn: target };
        }
      }
      return null;
    },
  };
}

/** Inline a schema's `ref`: unions -> enum, interfaces -> properties; follows imports. */
function resolveRefs(schema, resolver, ctxPath, seen = new Set(), depth = 0) {
  if (!schema || typeof schema !== "object") return schema;
  if (schema.items) schema.items = resolveRefs(schema.items, resolver, ctxPath, seen, depth);
  if (schema.properties) {
    for (const k of Object.keys(schema.properties))
      schema.properties[k] = resolveRefs(schema.properties[k], resolver, ctxPath, seen, depth);
  }
  if (schema.ref) {
    const r = resolver.lookup(schema.ref, ctxPath);
    if (!r || seen.has(schema.ref) || depth > 4) return schema; // unresolved/cyclic: keep ref
    const next = new Set(seen).add(schema.ref);
    if (r.entry.kind === "type") {
      return resolveRefs(tsTypeToSchema(r.entry.node).schema, resolver, r.definedIn, next, depth + 1);
    }
    const props = {};
    for (const m of r.entry.members) {
      if (m.type !== "TSPropertySignature") continue;
      const nm = m.key?.name ?? m.key?.value;
      if (!nm) continue;
      props[nm] = resolveRefs(tsTypeToSchema(m.typeAnnotation?.typeAnnotation).schema, resolver, r.definedIn, next, depth + 1);
    }
    return { type: "object", properties: props };
  }
  return schema;
}

/** Resolve a type node to its members + the file they were defined in. */
function membersFromType(typeNode, resolver, ctxPath) {
  if (!typeNode) return null;
  if (typeNode.type === "TSTypeAnnotation") typeNode = typeNode.typeAnnotation;
  if (typeNode.type === "TSTypeLiteral") return { members: typeNode.members, definedIn: ctxPath };
  if (typeNode.type === "TSTypeReference") {
    const r = resolver.lookup(typeNode.typeName?.name, ctxPath);
    if (r?.entry?.kind === "members") return { members: r.entry.members, definedIn: r.definedIn };
  }
  return null;
}

/** TSPropertySignature[] -> { name -> { schema, optional, description } }, refs resolved. */
function typeMembersFor(typeNode, resolver, ctxPath) {
  const found = membersFromType(typeNode, resolver, ctxPath);
  if (!found) return {};
  const map = {};
  for (const m of found.members) {
    if (m.type !== "TSPropertySignature") continue;
    const name = m.key?.name ?? m.key?.value;
    if (!name) continue;
    map[name] = {
      schema: resolveRefs(tsTypeToSchema(m.typeAnnotation?.typeAnnotation).schema, resolver, found.definedIn),
      optional: !!m.optional,
      description: componentDoc(m.leadingComments),
    };
  }
  return map;
}

/** React.FC<Props> / forwardRef on a declarator -> the props type node. */
function fcPropsType(declaratorId) {
  const ann = declaratorId?.typeAnnotation?.typeAnnotation;
  if (ann?.type !== "TSTypeReference") return null;
  const n = ann.typeName;
  const name = n?.name ?? (n?.type === "TSQualifiedName" ? n.right?.name : null);
  if (name !== "FC" && name !== "FunctionComponent" && name !== "VFC") return null;
  return ann.typeParameters?.params?.[0] ?? null;
}

/** Unwrap a component value: direct fn, or a known HOC call (memo/forwardRef/observer). */
function unwrapComponent(node) {
  if (!node) return null;
  if (node.type === "ArrowFunctionExpression" || node.type === "FunctionExpression") {
    return { fn: node, explicitType: null };
  }
  if (node.type === "CallExpression") {
    const c = node.callee;
    const nm = c?.name ?? (c?.type === "MemberExpression" ? c.property?.name : null);
    if (nm === "memo" || nm === "forwardRef" || nm === "observer") {
      const arg = node.arguments?.find(
        (a) => a.type === "ArrowFunctionExpression" || a.type === "FunctionExpression"
      );
      if (!arg) return null;
      const tp = node.typeParameters?.params;
      // forwardRef<RefT, PropsT> puts props in the 2nd type arg; memo<PropsT> in the 1st.
      const explicitType = nm === "forwardRef" ? tp?.[1] ?? null : tp?.[0] ?? null;
      return { fn: arg, explicitType };
    }
  }
  return null;
}

function resolveProps(param, resolver, ctxPath, explicitTypeNode) {
  let typeMembers = explicitTypeNode ? typeMembersFor(explicitTypeNode, resolver, ctxPath) : {};
  if (!Object.keys(typeMembers).length) typeMembers = typeMembersFor(param?.typeAnnotation, resolver, ctxPath);

  // Destructured props: `({ a, b = 2 })`
  if (param?.type === "ObjectPattern") {
    const props = [];
    for (const p of param.properties) {
      if (p.type === "RestElement") {
        props.push({ name: `...${p.argument?.name ?? "rest"}`, schema: { type: "any" }, required: false, rest: true });
        continue;
      }
      const name = p.key?.name ?? p.key?.value;
      if (!name) continue;
      const hasDefault = p.value?.type === "AssignmentPattern";
      const def = hasDefault ? serializeDefault(p.value.right) : undefined;
      const tm = typeMembers[name];
      const schema = tm?.schema ?? schemaFromDefault(def) ?? { type: "any" };
      props.push({
        name,
        schema,
        ...(def !== undefined && { default: def }),
        required: hasDefault ? false : !(tm?.optional ?? false),
        description: tm?.description ?? "",
      });
    }
    return props;
  }

  // Typed identifier props: `(props: Props)` — surface the type members directly.
  if (Object.keys(typeMembers).length) {
    return Object.entries(typeMembers).map(([name, tm]) => ({
      name,
      schema: tm.schema,
      required: !tm.optional,
      description: tm.description,
    }));
  }

  // Untyped identifier props: `(props)` — opaque object.
  if (param?.type === "Identifier") {
    return [{ name: param.name, schema: { type: "object" }, required: true, description: "" }];
  }
  return [];
}

function collectComponents(ast, absPath, relPath, resolver) {
  const components = [];

  const visitFn = (fnNode, name, exported, leadingComments, explicitTypeNode) => {
    if (!isComponentName(name)) return;
    if (!containsJSX(fnNode.body ?? fnNode)) return; // exclude capitalized non-components
    const paramsDoc = parseParamTags(leadingComments);
    let props = resolveProps(fnNode.params?.[0], resolver, absPath, explicitTypeNode);
    // Backfill JSDoc descriptions for untyped destructured props.
    props = props.map((p) => ({
      ...p,
      description: p.description || paramsDoc[p.name]?.description || "",
    }));
    components.push({
      name,
      kind: parseKindTag(leadingComments) || snake(name),
      file: relPath,
      exported,
      description: componentDoc(leadingComments),
      props,
    });
  };

  const handleVarDecl = (decl, exported) => {
    for (const vd of decl.declarations) {
      const u = unwrapComponent(vd.init);
      if (u) {
        visitFn(u.fn, vd.id?.name, exported, vd.leadingComments ?? decl.leadingComments, u.explicitType ?? fcPropsType(vd.id));
      }
    }
  };

  for (const stmt of ast.program.body) {
    if (stmt.type === "ExportNamedDeclaration" && stmt.declaration) {
      const d = stmt.declaration;
      if (d.type === "FunctionDeclaration" && d.id) visitFn(d, d.id.name, true, stmt.leadingComments ?? d.leadingComments);
      if (d.type === "VariableDeclaration") handleVarDecl(d, true);
    } else if (stmt.type === "ExportDefaultDeclaration") {
      const d = stmt.declaration;
      if (d?.type === "FunctionDeclaration") {
        visitFn(d, d.id?.name ?? nameFromFile(relPath), true, stmt.leadingComments ?? d.leadingComments);
      } else {
        const u = unwrapComponent(d); // anonymous arrow/fn or HOC default export
        if (u) visitFn(u.fn, nameFromFile(relPath), true, stmt.leadingComments, u.explicitType);
      }
    } else if (stmt.type === "FunctionDeclaration" && stmt.id) {
      visitFn(stmt, stmt.id.name, false, stmt.leadingComments);
    } else if (stmt.type === "VariableDeclaration") {
      handleVarDecl(stmt, false);
    }
  }
  return components;
}

export function extractFromDir(srcDir, root = srcDir) {
  const files = walkFiles(srcDir);
  const resolver = createResolver();
  const parsed = [];
  const errors = [];

  // Parse + prime the resolver first so cross-file type lookups work for any file.
  for (const file of files) {
    try {
      const ast = parseFile(file);
      resolver.prime(file, ast);
      parsed.push({ file, ast });
    } catch (err) {
      errors.push({ file: relative(root, file), error: err.message });
    }
  }

  const components = [];
  for (const { file, ast } of parsed) {
    try {
      components.push(...collectComponents(ast, file, relative(root, file), resolver));
    } catch (err) {
      errors.push({ file: relative(root, file), error: err.message });
    }
  }
  return { components, errors };
}

export { typeText };
