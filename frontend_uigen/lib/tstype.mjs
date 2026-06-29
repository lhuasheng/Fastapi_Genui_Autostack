/**
 * Map a TypeScript type AST node (from @babel/parser) to a JSON-Schema-ish
 * descriptor used in the component catalog. Best-effort: unknown shapes fall
 * back to `{ type: "any" }` while preserving the raw type text for reference.
 */

const KEYWORD = {
  TSStringKeyword: "string",
  TSNumberKeyword: "number",
  TSBooleanKeyword: "boolean",
  TSObjectKeyword: "object",
  TSAnyKeyword: "any",
  TSUnknownKeyword: "any",
  TSNullKeyword: "null",
  TSUndefinedKeyword: "null",
  TSVoidKeyword: "null",
};

function refName(typeName) {
  if (!typeName) return null;
  if (typeName.type === "Identifier") return typeName.name;
  if (typeName.type === "TSQualifiedName") return `${refName(typeName.left)}.${typeName.right.name}`;
  return null;
}

/** Render a type node back to compact source text (for the `tsType` annotation). */
export function typeText(node) {
  if (!node) return "any";
  if (KEYWORD[node.type]) return KEYWORD[node.type] === "any" ? "any" : node.type.replace(/^TS|Keyword$/g, "").toLowerCase();
  switch (node.type) {
    case "TSArrayType":
      return node.elementType?.type === "TSUnionType"
        ? `(${typeText(node.elementType)})[]`
        : `${typeText(node.elementType)}[]`;
    case "TSParenthesizedType": return typeText(node.typeAnnotation);
    case "TSUnionType": return node.types.map(typeText).join(" | ");
    case "TSLiteralType": {
      const v = node.literal?.value;
      return typeof v === "string" ? `"${v}"` : String(v);
    }
    case "TSTypeReference": {
      const n = refName(node.typeName);
      const args = node.typeParameters?.params?.map(typeText).join(", ");
      return args ? `${n}<${args}>` : n;
    }
    case "TSFunctionType": return "function";
    case "TSTypeLiteral": return "object";
    default: return "any";
  }
}

/**
 * @returns {{ schema: object }} a descriptor like { type, items?, enum?, ref? }.
 */
export function tsTypeToSchema(node) {
  const schema = _map(node);
  const t = typeText(node);
  if (t && t !== schema.type && t !== "any") schema.tsType = t;
  return { schema };
}

function _map(node) {
  if (!node) return { type: "any" };
  if (KEYWORD[node.type]) return { type: KEYWORD[node.type] };

  switch (node.type) {
    case "TSParenthesizedType":
      return _map(node.typeAnnotation);

    case "TSArrayType":
      return { type: "array", items: _map(node.elementType) };

    case "TSLiteralType": {
      const v = node.literal?.value;
      const t = typeof v === "boolean" ? "boolean" : typeof v === "number" ? "number" : "string";
      return { type: t, enum: [v] };
    }

    case "TSUnionType":
      return _mapUnion(node.types);

    case "TSTypeReference": {
      const name = refName(node.typeName);
      if (name === "Array" || name === "ReadonlyArray") {
        const arg = node.typeParameters?.params?.[0];
        return { type: "array", items: arg ? _map(arg) : { type: "any" } };
      }
      if (name === "ReactNode" || name === "React.ReactNode" || name === "ReactElement") {
        return { type: "node" };
      }
      // Reference to a local interface/type alias — caller may inline it; keep the name.
      return { type: "object", ref: name };
    }

    case "TSFunctionType":
      return { type: "function" };

    case "TSTypeLiteral":
      return { type: "object" };

    default:
      return { type: "any" };
  }
}

function _mapUnion(types) {
  // Drop null/undefined members (those mark optionality, handled separately).
  const real = types.filter(
    (t) => t.type !== "TSNullKeyword" && t.type !== "TSUndefinedKeyword"
  );
  if (real.length === 1) return _map(real[0]);

  // All string/number literals -> enum.
  if (real.every((t) => t.type === "TSLiteralType")) {
    const vals = real.map((t) => t.literal?.value);
    const allStr = vals.every((v) => typeof v === "string");
    const allNum = vals.every((v) => typeof v === "number");
    return { type: allStr ? "string" : allNum ? "number" : ["string", "number"], enum: vals };
  }

  // Mixed scalar union -> list of types.
  const mapped = real.map(_map);
  const simple = mapped.map((m) => m.type).filter((t) => typeof t === "string");
  if (simple.length === mapped.length) return { type: [...new Set(simple)] };
  return { anyOf: mapped };
}
