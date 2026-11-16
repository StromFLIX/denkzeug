/** Per-invocation MCP deadlines; never mutate a shared transport or remote schema. */
import { AsyncLocalStorage } from "node:async_hooks";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export const MCP_TIMEOUT_TOOL = "mcp_call_with_timeout";
export const MCP_TIMEOUT_PRESETS = Object.freeze({ fast: 30, normal: 120, long: 600 });
export const MCP_DISCOVERY_TIMEOUT_SECONDS = 30;
export const MCP_MAX_TIMEOUT_SECONDS = 600;
const callTimeout = new AsyncLocalStorage<number>();
export const currentMcpTimeout = () => callTimeout.getStore();

export function validateMcpTimeout(value: unknown): number {
  if (typeof value !== "number" || !Number.isInteger(value) || value < 1 || value > MCP_MAX_TIMEOUT_SECONDS)
    throw new Error("MCP timeout_seconds must be an integer from 1 to 600.");
  return value;
}

export class McpTimeoutError extends Error {
  constructor(label: string, seconds: number) {
    super(`${label} timed out after ${seconds}s (MCP request deadline). No automatic retry. ` +
      "The upstream operation may still be running; inspect its outcome before retrying a mutation. " +
      (label.startsWith("MCP tool ")
        ? `For an intentionally slow tool, use ${MCP_TIMEOUT_TOOL} with a longer deadline (maximum 600s).`
        : "Check server initialization/discovery; per-tool overrides do not extend the discovery budget."));
    this.name = "McpTimeoutError";
  }
}

export async function withMcpDeadline<T>(
  operation: (signal: AbortSignal) => Promise<T>, seconds: number, label: string, caller?: AbortSignal,
): Promise<T> {
  caller?.throwIfAborted();
  const deadline = new AbortController();
  const signal = caller ? AbortSignal.any([caller, deadline.signal]) : deadline.signal;
  const timer = setTimeout(() => deadline.abort(new McpTimeoutError(label, seconds)), seconds * 1000);
  let rejectAbort: () => void = () => {};
  const aborted = new Promise<never>((_resolve, reject) => {
    rejectAbort = () => reject(signal.reason);
    signal.addEventListener("abort", rejectAbort, { once: true });
  });
  try {
    // Bound the caller's wait even if a transport ignores cancellation. The
    // signal also aborts fetch AND response-body consumption in native fetch.
    return await Promise.race([Promise.resolve().then(() => {
      signal.throwIfAborted();
      return operation(signal);
    }), aborted]);
  } finally {
    clearTimeout(timer);
    signal.removeEventListener("abort", rejectAbort);
  }
}

export function registerMcpTimeoutTool(pi: ExtensionAPI, allowedNames: readonly string[]) {
  const allowed = new Set(allowedNames);
  if (!allowed.size) return;
  pi.registerTool({
    name: MCP_TIMEOUT_TOOL,
    label: "MCP call with timeout",
    exposure: "model-only",
    description: "Call an already allowed MCP tool with a per-request deadline. Ordinary MCP calls default to 120s. " +
      "Presets: fast=30s, normal=120s, long=600s; or choose timeout_seconds from 1 to 600. " +
      "The deadline covers the MCP request and response, excluding discovery and interactive sign-in. " +
      "Explicit administrator or upstream limits may be shorter. " +
      "Original argument validation, permissions, account scoping and tool hooks still apply. " +
      "Timeouts do not guarantee that the upstream stopped. Never blindly replay a timed-out mutation.",
    promptSnippet: "Override an allowed MCP call's default 120s deadline using a preset or 1–600 seconds",
    reachesTools: input => typeof input.tool === "string" && allowed.has(input.tool) ? [input.tool] : [],
    parameters: {
      type: "object", additionalProperties: false, required: ["tool", "arguments"],
      properties: {
        tool: { type: "string", description: "Exact name of an already selected MCP tool." },
        arguments: { type: "object", description: "The original tool arguments, unchanged. No timeout field is forwarded." },
        preset: { type: "string", enum: ["fast", "normal", "long"], description: "Request deadline: 30, 120, or 600 seconds. Use preset OR timeout_seconds." },
        timeout_seconds: { type: "integer", minimum: 1, maximum: 600, description: "Explicit request deadline in seconds, instead of preset. Default: normal (120s)." },
      },
    },
    async execute(_id, input: any, signal, onUpdate, ctx) {
      if (!allowed.has(input.tool)) throw new Error("Timeout override can only call a currently selected MCP tool.");
      if (input.preset !== undefined && input.timeout_seconds !== undefined)
        throw new Error("Choose preset OR timeout_seconds, not both.");
      if (input.preset !== undefined && !Object.hasOwn(MCP_TIMEOUT_PRESETS, input.preset))
        throw new Error("MCP timeout preset must be fast, normal, or long.");
      const preset = (input.preset ?? "normal") as keyof typeof MCP_TIMEOUT_PRESETS;
      const seconds = validateMcpTimeout(input.timeout_seconds !== undefined
        ? input.timeout_seconds : MCP_TIMEOUT_PRESETS[preset]);
      return callTimeout.run(seconds, async () => {
        const outcome = await ctx.executeTool(input.tool, input.arguments, { signal, onUpdate });
        // Pi returns an outcome envelope and automatically adds nested usage;
        // preserve the actual result/error without double-counting its usage.
        return {
          content: outcome.result.content, details: outcome.result.details,
          structuredContent: outcome.result.structuredContent, isError: outcome.isError,
          terminate: outcome.result.terminate,
        };
      });
    },
  });
}
