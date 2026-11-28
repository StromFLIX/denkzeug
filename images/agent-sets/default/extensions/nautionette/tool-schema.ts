/** Wire-only compatibility; registered schemas and runtime validation stay intact. */
const MAP_SCHEMAS = new Set(["properties", "patternProperties", "$defs", "definitions", "dependentSchemas"]);
const SINGLE_SCHEMAS = new Set(["items", "additionalItems", "additionalProperties", "unevaluatedItems", "unevaluatedProperties", "contains", "propertyNames", "not", "if", "then", "else"]);
const ARRAY_SCHEMAS = new Set(["anyOf", "oneOf", "allOf", "prefixItems"]);
const ARRAY_KEYWORDS = ["items", "additionalItems", "prefixItems", "contains", "minContains", "maxContains", "minItems", "maxItems", "uniqueItems", "unevaluatedItems"];

export function geminiToolSchema(schema: any): any {
  if (!schema || typeof schema !== "object" || Array.isArray(schema)) return schema;
  const next: Record<string, any> = { ...schema };
  // Walk schema positions only: defaults, examples, const/enum and extension
  // metadata are data, even when their objects contain a key called "type".
  for (const [key, value] of Object.entries(next)) {
    if (MAP_SCHEMAS.has(key) && value && typeof value === "object" && !Array.isArray(value)) {
      next[key] = Object.fromEntries(Object.entries(value).map(([name, child]) => [name, geminiToolSchema(child)]));
    } else if (ARRAY_SCHEMAS.has(key) && Array.isArray(value)) {
      next[key] = value.map(geminiToolSchema);
    } else if (SINGLE_SCHEMAS.has(key)) {
      next[key] = Array.isArray(value) ? value.map(geminiToolSchema) : geminiToolSchema(value);
    }
  }
  const types = next.type;
  if (Array.isArray(types) && types.includes("array") && types.some(type => type !== "array" && type !== "null")) {
    // Copilot Gemini rejects type:["array","string"] with sibling items.
    // An equivalent union with items on the array branch is accepted. Nullable
    // arrays already work; do not rewrite them or unrelated scalar unions.
    const arrayConstraints: Record<string, any> = {};
    for (const key of ARRAY_KEYWORDS) {
      if (Object.hasOwn(next, key)) {
        arrayConstraints[key] = next[key];
        delete next[key];
      }
    }
    delete next.type;
    const anyOf = types.map(type => ({ type, ...(type === "array" ? arrayConstraints : {}) }));
    if (Object.hasOwn(next, "anyOf")) {
      next.allOf = [...(next.allOf ?? []), { anyOf }];
    } else {
      next.anyOf = anyOf;
    }
  }
  return next;
}

export function toolSchemaPayload(payload: Record<string, any>, model: string, api: string) {
  if (!model.startsWith("copilot/gemini-") || api !== "openai-completions" || !Array.isArray(payload.tools)) return payload;
  return {
    ...payload,
    tools: payload.tools.map((tool: any) => tool.type === "function" && tool.function?.parameters
      ? { ...tool, function: { ...tool.function, parameters: geminiToolSchema(tool.function.parameters) } }
      : tool),
  };
}
