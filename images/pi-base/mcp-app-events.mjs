/** One-shot UI side channel: native/model-retrievable sessions never hold UI secrets. */
import { lstatSync, readFileSync, unlinkSync } from 'node:fs'

export function appEventFields (result) {
  const path = result?.details?.mcpAppRef
  const failure = result?.details?.mcpAppError
  if (!path) return failure ? { app_error: failure } : {}
  if (typeof path !== 'string' || !/^\/tmp\/denkzeug-mcp-apps\/[a-f0-9-]{36}\.json$/.test(path)) return { app_error: 'Invalid app snapshot reference.' }
  try {
    const info = lstatSync(path)
    if (!info.isFile() || info.isSymbolicLink() || info.size > 2097152) throw new Error()
    return { mcp_app: JSON.parse(readFileSync(path, 'utf8')) }
  } catch { return { app_error: 'The app snapshot could not be read. The original tool result is unchanged.' } }
  finally { try { unlinkSync(path) } catch { /* Container cleanup removes abandoned snapshots. */ } }
}

/** Legacy native entries may contain UI details; project BEFORE search/pagination. */
export function modelHistoryEntry (entry) {
  if (entry?.type !== 'message' || entry.message?.role !== 'toolResult' || !entry.message.details) return entry
  const { details, ...message } = entry.message
  // Details are a tool's out-of-band UI channel, not model-visible content.
  // Keep the original public content and tool-call identifiers unchanged.
  return { ...entry, message }
}
