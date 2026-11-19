// Adapt the original broker's unchanged offline Pi steering fixtures to the
// Rust broker's production workspace tmpfs and PI_CODING_AGENT_DIR.
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const agent = '/usr/local/lib/nautionette/agent-run.mjs';
const home = process.env.PI_CODING_AGENT_DIR;
if (process.getuid() !== 10001 || home !== '/workspace/.pi-agent') {
  throw new Error('test must exercise the unprivileged chat container');
}
fs.mkdirSync(path.join(home, 'extensions'), { recursive: true });
fs.copyFileSync('/fixture/chat-provider.ts', path.join(home, 'extensions/chat-provider.ts'));
fs.symlinkSync(agent, '/workspace/agent-run.mjs');
import(pathToFileURL('/fixture/chat-model.mjs'));
