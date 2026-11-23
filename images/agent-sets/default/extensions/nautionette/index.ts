/**
 * The default agent set.
 *
 * Two jobs, both of them about openness:
 *  1. point Pi at agentgateway instead of a provider, so no model key ever
 *     enters this container;
 *  2. bridge whatever MCP tools the gateway federates into Pi tools, so adding
 *     a tool to the gateway is enough to give the agent a new capability.
 */
import { writeFileSync } from "node:fs";
import brand from "./brand.json" with { type: "json" };
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { modelApi } from "./model-api.ts";
import { createMcpTransport } from "./mcp-transport.ts";
import { modelVisible, modelResult, snapshotApp, storeAppSnapshot } from "./mcp-apps.ts";
import { appAuthoringSchema, appAuthoringInput, appAuthoringResult } from "./app-authoring.ts";
import { MCP_TIMEOUT_TOOL, currentMcpTimeout, registerMcpTimeoutTool } from "./mcp-timeout.ts";
import { AUTH_TRANSPORT_TOOLS, authControl, waitForMcpAuth } from "./mcp-auth.ts";
import { modelLimits } from "./model-limits.ts";
import { reasoningConfig, reasoningPayload } from "./reasoning.ts";
import { allowedTools } from "./tool-selection.ts";
import { registerMcpTools } from "./mcp-tools.ts";
import { isOutputTransportTool, loadOutputGrant, registerOutputTools } from "./outputs.ts";
import { CRAFT_TOOL_NAMES, registerCraftTools } from "./artcraft.ts";
import { SUBAGENT_TOOLS, registerSubagentTools } from "./subagents.ts";

const GATEWAY = (process.env.AGENTGATEWAY_URL ?? "http://agentgateway:4000").replace(/\/$/, "");
const MODEL = process.env.AGENT_MODEL ?? "openai/gpt-4o-mini";
const MCP_URL = process.env.MCP_URL ?? `${GATEWAY}/mcp`;
const PROVIDER = "nautionette";
// A null selection means all tools; an empty set deliberately bridges none.
const ALLOWED = allowedTools(process.env.NAUTIONETTE_TOOLS_JSON, process.env.NAUTIONETTE_TOOLS);

const TARGETS = allowedTools(process.env.NAUTIONETTE_MCP_TARGETS_JSON);
const transport = createMcpTransport(MCP_URL, fetch, TARGETS === null ? null : [...TARGETS]);
// First-party control must not retain an external session snapshot from before
// login. New remote targets cannot invalidate this backend/workflows-only scope.
const control = createMcpTransport(MCP_URL, fetch, []);
const rpc = transport.rpc;

const renderToolResult = modelResult;

// Private, fixed-name timing records consumed by the supervisor. No separate
// collector connection from Pi, and no URLs, tool names or payloads in records.
async function timed<T>(stage: string, operation: () => Promise<T>): Promise<T> {
  if (process.env.NAUTIONETTE_PI_STARTUP_TIMING !== "1") return operation();
  const start = performance.now();
  let failed = false;
  try { return await operation(); }
  catch (error) { failed = true; throw error; }
  finally {
    console.error(`denkzeug-pi-startup ${stage} ${performance.timeOrigin + start} ${performance.timeOrigin + performance.now()} ${Number(failed)}`);
  }
}

export default async function (pi: ExtensionAPI) {
  pi.on("session_shutdown", () => Promise.all([transport.close(), control.close()]));
  const api = await timed("model_api", () => modelApi(MODEL, GATEWAY));
  const reasoning = reasoningConfig();
  console.error(`[nautionette] model ${MODEL} uses ${api} through agentgateway`);
  pi.registerProvider(PROVIDER, {
    name: `${brand.name} gateway`,
    baseUrl: `${GATEWAY}/v1`,
    // agentgateway holds the real provider key; this value only has to exist.
    apiKey: "gateway",
    api,
    authHeader: true,
    models: [
      {
        id: MODEL,
        name: `${MODEL} (via agentgateway)`,
        reasoning: reasoning.reasoning,
        thinkingLevelMap: reasoning.thinkingLevelMap,
        compat: {
          supportsDeveloperRole: false,
          supportsReasoningEffort: reasoning.reasoning,
          thinkingFormat: reasoning.format,
        },
        // Unknown catalogs must not cause Pi to silently strip image blocks.
        input: process.env.NAUTIONETTE_MODEL_IMAGES === "false" ? ["text"] : ["text", "image"],
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
        ...modelLimits(),
      },
    ],
  });

  pi.on("session_start", async (_event, ctx) => {
    const model = ctx.modelRegistry.find(PROVIDER, MODEL);
    if (model) await pi.setModel(model);
    pi.setThinkingLevel(reasoning.level);
  });

  pi.on("before_provider_request", (event, ctx) => {
    if (ctx.model?.provider !== PROVIDER || ctx.model.id !== MODEL) return;
    return reasoningPayload(event.payload as Record<string, any>, api, reasoning);
  });

  // Local first-party tools hold a private, turn-scoped capability. The raw
  // transports must never be declared to the model or gated by MCP selection.
  const grant = loadOutputGrant();
  const chatGrant = process.env.NAUTIONETTE_TURN_ID ? grant : null;
  registerOutputTools(pi, control.rpc, chatGrant);
  registerSubagentTools(pi, control.rpc, chatGrant, ALLOWED);
  if (process.env.NAUTIONETTE_MEMBER_MODE !== "true") registerCraftTools(pi);

  // Explicitly no bridged tools: retain native tools and the legacy loadout
  // migration, but don't make any MCP requests until a local wrapper needs one.
  if (ALLOWED?.size === 0) {
    registerMcpTools(pi, []);
    return;
  }

  // Tools are discovered, not hard-coded: federate a new MCP server behind the
  // gateway and this agent set picks it up on its next run.
  try {
    await timed("mcp_connect", () => transport.connect());
    const listed = await timed("mcp_list", () => rpc("tools/list"));
    const all: any[] = listed.tools ?? [];
    const authGates = all.some(tool => tool.name === "backend_mcp_oauth_gate");
    const oauthTools = new Set<string>();
    if (authGates) {
      const saved = await timed("mcp_oauth_catalog", () => authControl(control.rpc, "backend_mcp_oauth_tools", { targets: TARGETS === null ? null : [...TARGETS] }));
      for (const name of saved.oauth_tools || []) oauthTools.add(name);
      const names = new Set(all.map(tool => tool.name));
      for (const tool of saved.tools || []) {
        if (!names.has(tool.name)) { all.push(tool); names.add(tool.name); }
      }
    }
    transport.setReadOnlyTools(all.filter(tool => tool.annotations?.readOnlyHint === true).map(tool => tool.name));
    const tools = all.filter((tool) => modelVisible(tool) && tool.name !== MCP_TIMEOUT_TOOL && !AUTH_TRANSPORT_TOOLS.has(tool.name) && !isOutputTransportTool(tool.name) && !SUBAGENT_TOOLS.has(tool.name) && !CRAFT_TOOL_NAMES.has(tool.name) && (ALLOWED === null || ALLOWED.has(tool.name)));

    // Payload-free provenance for the supervisor's workflow accounting. Tool
    // names come from discovery, never namespace/name heuristics.
    if (process.env.NAUTIONETTE_MCP_CATALOG_FILE) {
      writeFileSync(process.env.NAUTIONETTE_MCP_CATALOG_FILE, JSON.stringify(tools.map(tool => tool.name)), { mode: 0o600 });
    }

    const definitions = tools.map(tool => ({
        name: tool.name,
        namespace: { name: tool.server || tool.name.split("_")[0] || "mcp", description: "Selected MCP capabilities" },
        annotations: tool.annotations,
        label: tool.title ?? tool.name,
        description: tool.description ?? `MCP tool ${tool.name}`,
        promptSnippet: tool.description?.split("\n")[0],
        parameters: appAuthoringSchema(tool, process.env.NAUTIONETTE_MEMBER_MODE === "true") ?? { type: "object", properties: {} },
        async execute(_toolCallId: string, params: unknown, signal?: AbortSignal) {
          if (oauthTools.has(tool.name)) {
            await waitForMcpAuth(control.rpc, grant || {}, tool.name, _toolCallId, signal);
          }
          const dispatch = oauthTools.has(tool.name) ? transport.rpcFresh : rpc;
          // The gateway's service credential is transport-only. A hidden,
          // live turn capability carries this account's authority; it never
          // enters the model-visible schema or persisted tool arguments.
          params = await appAuthoringInput(tool.name, params, process.env.NAUTIONETTE_MEMBER_MODE === "true");
          const argumentsWithActor = (tool.name.startsWith("backend_") || tool.name.startsWith("workflows_")) && grant?.grant
            ? { ...(params as Record<string, unknown> ?? {}), __denkzeug_actor: grant.grant }
            : params ?? {};
          const result = await dispatch("tools/call", { name: tool.name, arguments: argumentsWithActor }, signal, currentMcpTimeout());
          const text = renderToolResult(appAuthoringResult(tool.name, result));
          if (result?.isError) throw new Error(text);
          let mcpAppRef;
          let mcpAppError;
          if (process.env.NAUTIONETTE_TURN_ID) {
            try {
              const snapshot = await snapshotApp(dispatch, tool, params, result, signal);
              if (snapshot) mcpAppRef = storeAppSnapshot(snapshot);
            } catch {
              // The business tool already finished. A failed view fetch must
              // never turn that success into a retryable tool failure.
              mcpAppError = "The MCP App interface could not be loaded. The original tool result is unchanged.";
            }
          }
          // Pi persists details in native history. Keep private result metadata
          // and executable HTML exclusively in the supervisor's one-shot file.
          return { content: [{ type: "text", text }], details: { mcpAppRef, mcpAppError } };
        },
      }));
    registerMcpTools(pi, definitions);
    registerMcpTimeoutTool(pi, definitions.map(tool => tool.name));
    console.error(`[nautionette] bridged ${tools.length} of ${all.length} MCP tool(s) from ${MCP_URL}`);
  } catch (error) {
    // A missing gateway must not cost the agent its built-in tools.
    console.error(`[nautionette] MCP bridge unavailable: ${(error as Error).message}`);
  }
}
