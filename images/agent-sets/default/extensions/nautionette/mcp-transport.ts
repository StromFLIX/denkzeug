/** Recover the shared MCP session without blindly repeating a mutation. */
import { MCP_DISCOVERY_TIMEOUT_SECONDS, MCP_TIMEOUT_PRESETS, validateMcpTimeout, withMcpDeadline } from "./mcp-timeout.ts";

// Only fixed gateway diagnostics may be surfaced. Upstream bodies can contain
// credentials or private content; never echo a free-form error message.
const SAFE_REASONS = new Map([
  ["failed to send message", "gateway_send_failed"],
  ["service unavailable", "service_unavailable"],
  ["upstream error", "upstream_http_error"],
  ["failed to create session", "session_creation_failed"],
  ["request body too large", "request_too_large"],
]);
async function httpReason(response: Response): Promise<string | undefined> {
  const reader = response.body?.getReader();
  if (!reader) return;
  try {
    const chunks: Uint8Array[] = [];
    let size = 0;
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > 4096) return;
      chunks.push(value);
    }
    const bytes = new Uint8Array(size);
    let offset = 0;
    for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
    const payload = JSON.parse(new TextDecoder().decode(bytes));
    return SAFE_REASONS.get(payload?.error?.message);
  } catch { return; }
  finally { await reader.cancel().catch(() => {}); }
}

export function createMcpTransport(url: string, fetcher: typeof fetch = fetch, selection: readonly string[] | null = null) {
  // Immutable for the entire MCP session, including recovery and post-login calls.
  const targets = selection === null ? null : [...new Set(selection)].sort();
  if (targets && (targets.some(name => !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(name)) || targets.join(",").length > 8000))
    throw new Error("Invalid MCP target selection");
  const scopeHeaders = targets === null ? {} : { "x-nautionette-mcp-targets": targets.join(",") };
  let messageId = 0;
  let sessionId: string | undefined;
  let connected = false;
  let connecting: Promise<void> | undefined;
  let readonlyTools = new Set<string>();

  class HttpFailure extends Error {
    status: number;
    session: string | undefined;
    constructor(status: number, session: string | undefined, label: string, reason?: string) {
      const classification = status === 504 ? "gateway/upstream timeout reported" : reason;
      super(`${label} failed: MCP transport -> HTTP ${status}${classification ? ` (${classification})` : ""}. ` +
        "No automatic retry. The operation's outcome may be unknown; inspect it before retrying a mutation.");
      this.status = status;
      this.session = session;
    }
  }

  async function send(method: string, params: Record<string, unknown>, signal?: AbortSignal) {
    signal?.throwIfAborted();
    const notification = method.startsWith("notifications/");
    const body = { jsonrpc: "2.0", method, params, ...(notification ? {} : { id: ++messageId }) };
    const headers: Record<string, string> = {
      "content-type": "application/json",
      accept: "application/json, text/event-stream",
      "mcp-protocol-version": "2025-06-18",
      ...scopeHeaders,
    };
    const requestSession = sessionId;
    if (requestSession) headers["mcp-session-id"] = requestSession;
    const label = method === "tools/call" ? `MCP tool ${params.name}` : `MCP ${method}`;
    const response = await fetcher(url, { method: "POST", headers, body: JSON.stringify(body), signal });
    if (signal?.aborted) { await response.body?.cancel(); signal.throwIfAborted(); }
    if (!response.ok) {
      // Do not infer that a failed mutation did not run, or label an opaque 500
      // as a timeout: v1.7 collapses multiple upstream failures into SendError.
      throw new HttpFailure(response.status, requestSession, label, await httpReason(response));
    }
    const returned = response.headers.get("mcp-session-id");
    if (returned) sessionId = returned;
    if (notification) { await response.body?.cancel(); return {}; }
    const raw = await response.text();
    signal?.throwIfAborted();
    const payload = raw.includes("event:") || raw.startsWith("data:")
      ? raw.split("\n").filter(line => line.startsWith("data:")).map(line => line.slice(5).trim()).join("")
      : raw;
    const parsed = JSON.parse(payload);
    if (parsed.error) {
      const code = Number.isSafeInteger(parsed.error.code) ? ` ${parsed.error.code}` : "";
      const reason = SAFE_REASONS.get(parsed.error.message);
      throw new Error(`${label} failed: JSON-RPC${code}${reason ? ` (${reason})` : ""}. ` +
        "No automatic retry; inspect the operation's outcome before retrying a mutation.");
    }
    return parsed.result ?? {};
  }

  async function connect(signal?: AbortSignal): Promise<void> {
    signal?.throwIfAborted();
    if (connecting) return connecting;
    if (connected) return;
    connecting = (async () => {
      sessionId = undefined;
      try {
        await withMcpDeadline(async requestSignal => {
          await send("initialize", {
            protocolVersion: "2025-06-18", capabilities: {},
            clientInfo: { name: "nautionette-agent-set", version: "1.0.0" },
          }, requestSignal);
          await send("notifications/initialized", {}, requestSignal);
          connected = true;
        }, MCP_DISCOVERY_TIMEOUT_SECONDS, "MCP initialization", signal);
      } catch (error) {
        sessionId = undefined;
        connected = false;
        throw error;
      }
    })();
    try { await connecting; } finally { connecting = undefined; }
  }

  async function rpc(method: string, params: Record<string, any> = {}, signal?: AbortSignal, timeoutSeconds?: number): Promise<Record<string, any>> {
    const seconds = validateMcpTimeout(timeoutSeconds ?? (method === "tools/call" ? MCP_TIMEOUT_PRESETS.normal : MCP_DISCOVERY_TIMEOUT_SECONDS));
    await connect(signal);
    return withMcpDeadline(async requestSignal => {
      try { return await send(method, params, requestSignal); }
      catch (error) {
        // Re-handshake stale sessions once. Timeout, network, auth and RPC
        // errors are never replayed, even for an explicitly read-only tool.
        if (!(error instanceof HttpFailure) || !error.session || ![400, 404].includes(error.status)) throw error;
        if (sessionId === error.session) connected = false;
        const safe = method === "tools/list" || (method === "tools/call" && readonlyTools.has(params.name));
        try { await connect(requestSignal); }
        catch {
          requestSignal.throwIfAborted();
          throw new Error(`${method} -> HTTP ${error.status}; MCP session recovery failed. Original operation was not replayed.`);
        }
        if (!safe) {
          throw new Error(`${method} -> HTTP ${error.status}; MCP session refreshed. Original operation was NOT replayed; inspect its outcome before retrying.`);
        }
        // Share the original deadline, not another full timeout after recovery.
        return await send(method, params, requestSignal);
      }
    }, seconds, method === "tools/call" ? `MCP tool ${params.name}` : `MCP ${method}`, signal);
  }

  async function close() {
    if (!sessionId) return;
    try {
      const response = await fetcher(url, { method: "DELETE", headers: {
        accept: "application/json, text/event-stream",
        "mcp-session-id": sessionId, "mcp-protocol-version": "2025-06-18", ...scopeHeaders,
      }, signal: AbortSignal.timeout(5000) });
      await response.body?.cancel();
    } catch { /* Best effort; cleanup must not hide an operation's outcome. */ }
  }

  async function rpcFresh(method: string, params: Record<string, any>, signal?: AbortSignal, timeoutSeconds?: number) {
    // Never dispatch authenticated calls through a pre-login snapshot or reset
    // a shared session underneath concurrent tool calls.
    const fresh = createMcpTransport(url, fetcher, targets);
    try { return await fresh.rpc(method, params, signal, timeoutSeconds); }
    finally { await fresh.close(); }
  }

  return {
    rpc, rpcFresh, connect, close,
    setReadOnlyTools(names: string[]) { readonlyTools = new Set(names); },
  };
}
