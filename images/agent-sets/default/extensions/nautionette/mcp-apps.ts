/** MCP Apps metadata and resource snapshots. Never send app HTML/_meta to the model. */
export const APP_MIME = "text/html;profile=mcp-app";
export const APP_CAPABILITIES = {
  extensions: { "io.modelcontextprotocol/ui": { mimeTypes: [APP_MIME] } },
};
export const modelVisible = (tool: any) => !Array.isArray(tool?._meta?.ui?.visibility) || tool._meta.ui.visibility.includes("model");
export function appResource(tool: any): string | undefined {
  const uri = tool?._meta?.ui?.resourceUri ?? tool?._meta?.["ui/resourceUri"];
  return typeof uri === "string" && uri.startsWith("ui://") && uri.length <= 2048 ? uri : undefined;
}
export function modelResult(result: any): string {
  const content = Array.isArray(result?.content) ? result.content : [];
  const text = content.filter((part: any) => part?.type === "text").map((part: any) => part.text).join("\n");
  return text || JSON.stringify(result?.structuredContent ?? { content, isError: result?.isError === true }, null, 2);
}
export async function snapshotApp(rpc: any, tool: any, input: unknown, result: any, signal?: AbortSignal) {
  const uri = appResource(tool);
  if (!uri || result?.isError) return undefined;
  // Use the advertised federated URI EXACTLY: agentgateway encodes its target
  // inside ui://TARGET+resource. Never guess a target from the tool's name.
  const resource = await rpc("resources/read", { uri }, signal);
  const item = resource?.contents?.find((part: any) => part?.uri === uri && part?.mimeType === APP_MIME);
  if (!item) throw new Error("MCP App resource has no supported HTML content");
  const html = typeof item.text === "string" ? item.text : typeof item.blob === "string" ? Buffer.from(item.blob, "base64").toString("utf8") : "";
  if (!html || Buffer.byteLength(html, "utf8") > 1048576) throw new Error("MCP App HTML exceeds the resource limit");
  const payload = {
    title: tool.title || tool.name, resource_uri: uri, html,
    input: input || {}, result: result || { content: [] },
    csp: item._meta?.ui?.csp || {},
  };
  if (Buffer.byteLength(JSON.stringify(payload), "utf8") > 2097152) throw new Error("MCP App result exceeds the snapshot limit");
  return payload;
}
