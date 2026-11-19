// Offline fixture for Docker wire contracts, not a replacement for Pi tests.
const fs = require('node:fs');
const net = require('node:net');
const socket = '/tmp/nautionette-chat.sock';
const credential = '/tmp/nautionette-git-credentials/current.json';
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const frame = (value) => process.stdout.write(JSON.stringify(value) + '\n');
const readCredential = () => JSON.parse(fs.readFileSync(credential))[0].token;
const projectId = 'a'.repeat(32);
const chat = 'c'.repeat(12);
assert.equal(process.getuid(), 10001);
assert.equal(readCredential(), 'offline-fixture-initial');
assert.throws(() => fs.writeFileSync(credential, '[]'));
assert.equal(fs.readFileSync(`/projects/.sessions/${projectId}/${chat}/keep.txt`, 'utf8'), 'keep my changes');
assert.ok(fs.statSync(`/project-repositories/${projectId}`).isDirectory());
assert.ok(!process.env.INTERNAL_TOKEN && !process.env.BACKEND_URL);
fs.writeFileSync('/chat-session/session-fixture.txt', 'persistent');

const consumed = new Set();
let gitEnvironment;
const gitCredential = () => execFileSync('git', ['credential', 'fill'], {
  env: gitEnvironment, input: 'url=https://github.com/owner/repo.git\n\n', stdio: 'pipe', encoding: 'utf8',
});
const server = net.createServer((connection) => {
  let input = '';
  connection.on('data', (data) => {
    input += data.toString();
    if (!input.endsWith('\n')) return;
    try {
      const command = JSON.parse(input);
      assert.equal(command.type, 'steer');
      assert.equal(command.text, 'check renewed credential');
      assert.equal(readCredential(), 'offline-fixture-renewed');
      assert.ok(gitCredential().includes('password=offline-fixture-renewed'));
      if (!consumed.has(command.id)) {
        consumed.add(command.id);
        frame({type: 'input_consumed', id: command.id});
      }
      connection.end('{"ok":true}\n');
    } catch {
      connection.end('{"ok":false}\n');
    }
  });
});
import('/usr/local/lib/nautionette/project-git.mjs').then(({ projectEnvironment }) => {
  gitEnvironment = {...process.env, ...projectEnvironment({project_ids: [projectId], chat_id: chat,
    project_remotes: {[projectId]: 'owner/repo'}})};
  // The initial token has expired. The actual shipped helper must refuse it,
  // then observe the atomic broker renewal in an already running process.
  assert.throws(gitCredential);
  server.listen(socket, () => frame({type: 'fixture_ready'}));
}).catch((error) => { console.error(error); process.exitCode = 1; });
