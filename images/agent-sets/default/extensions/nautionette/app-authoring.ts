/** File-backed app publication avoids putting compiled SDK bundles in model context. */
import { readFile, stat } from "node:fs/promises";

export function appAuthoringSchema(tool: any, member = false) {
  if (member || tool.name !== "backend_publish_app") return tool.inputSchema;
  const schema = structuredClone(tool.inputSchema);
  let body = schema.properties?.body;
  if (body?.$ref?.startsWith("#/$defs/")) body = schema.$defs?.[body.$ref.slice(8)];
  if (!body?.properties?.html || !Array.isArray(body.required)) return tool.inputSchema;
  body.properties.html_path = { type: "string", description: "Local UTF-8 self-contained HTML file (max 1 MiB). Use instead of html for compiled app bundles." };
  body.required = body.required.filter((key: string) => key !== "html");
  body.oneOf = [{ required: ["html"], not: { required: ["html_path"] } }, { required: ["html_path"], not: { required: ["html"] } }];
  return schema;
}

export async function appAuthoringInput(name: string, input: any, member = false) {
  if (name !== "backend_publish_app" || !input?.body?.html_path) return input;
  if (member || input.body.html !== undefined) throw new Error("Use either inline html or a local HTML file");
  const { html_path: path, ...body } = input.body;
  if (typeof path !== "string") throw new Error("Provide a local HTML file path");
  const info = await stat(path);
  if (!info.isFile() || info.size > 1048576) throw new Error("App source must be a file of at most 1 MiB");
  const html = await readFile(path, "utf8");
  if (Buffer.byteLength(html) > 1048576) throw new Error("App source exceeds 1 MiB");
  return { ...input, body: { ...body, html } };
}

export function appAuthoringResult(name: string, result: any) {
  if (name !== "backend_publish_app" || result?.isError) return result;
  // The backend returns full source for human Settings clients. A publication
  // receipt for an agent needs only revision identity/hash, never its SDK code.
  const content = result?.content?.map((part: any) => {
    if (part.type !== "text") return part;
    try {
      const value = JSON.parse(part.text);
      if (value.revision?.html) value.revision.html = "[source retained in the app registry]";
      return { ...part, text: JSON.stringify(value) };
    } catch { return part; }
  });
  return { ...result, ...(content ? { content } : {}) };
}
