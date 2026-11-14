import { test } from 'node:test'
import { execFileSync } from 'node:child_process'
import assert from 'node:assert/strict'

test('native login, bearer requests, refresh rotation and logout use only the secure bridge', () => {
  const script = `
    import assert from 'node:assert/strict';
    const stored = new Map(), secrets = new Map(), calls = [];
    globalThis.localStorage = { getItem: key => stored.get(key), setItem: (key, value) => stored.set(key, value), removeItem: key => stored.delete(key) };
    stored.set('nautionette.token', 'obsolete-instance-secret');
    globalThis.denkzeugDesktop = { accountStore: {
      async get({origin}) { return {value: secrets.get(origin) || ''} },
      async set({origin, value}) { secrets.set(origin, value) },
      async remove({origin}) { secrets.delete(origin) }
    }};
    const { accountApi } = await import('./account.js');
    const { account, auth, clearAccount } = await import('./auth-session.js');
    const { request, server } = await import('./api.js');
    server.url = 'https://workspace.test';
    let expired = false;
    const me = {user:{id:'alice',username:'alice',role:'member',status:'active'},session_id:'one',csrf_token:''};
    globalThis.fetch = async (url, options) => {
      calls.push({url, ...options});
      if (url.endsWith('/login')) return Response.json({...me,access_token:'access-one',refresh_token:'refresh-one'});
      if (url.endsWith('/refresh')) {
        assert.equal(JSON.parse(options.body).refresh_token, 'refresh-one');
        expired = false;
        return Response.json({...me,access_token:'access-two',refresh_token:'refresh-two'});
      }
      if (url.endsWith('/logout')) { assert.equal(options.headers.Authorization, 'Bearer access-two'); return Response.json({}); }
      if (expired) return Response.json({detail:'Expired'}, {status:401});
      return Response.json({chats:[]});
    };
    await accountApi.login('alice', 'long-password');
    assert.equal(JSON.parse(calls[0].body).native, true);
    assert.equal(secrets.get('https://workspace.test'), 'refresh-one');
    assert.equal(auth.token, 'access-one');
    assert.equal(stored.has('nautionette.token'), false);
    assert.equal(JSON.stringify([...stored]).includes('access-one'), false);
    expired = true;
    await request('/api/chats');
    assert.equal(auth.token, 'access-two');
    assert.equal(secrets.get('https://workspace.test'), 'refresh-two');
    assert.equal(calls.at(-1).headers.Authorization, 'Bearer access-two');
    assert.ok(calls.every(call => !call.url.includes('token=')));
    assert.ok(calls.every(call => call.credentials === 'omit'));
    await accountApi.logout();
    assert.equal(secrets.size, 0); assert.equal(account.user, null); assert.equal(auth.token, '');
    assert.throws(() => { server.url = 'http://workspace.test' }, /HTTPS/);
    clearAccount();
  `
  assert.doesNotThrow(() => execFileSync(process.execPath, ['--input-type=module', '-e', script], { cwd: new URL('.', import.meta.url), stdio: 'pipe' }))
})
