import { test } from 'node:test';
import assert from 'node:assert/strict';
import { verifyOidc } from './auth.mjs';
import handler from './worker.mjs';

const now = Date.now();
const keypair = await crypto.subtle.generateKey({ name: 'RSASSA-PKCS1-v1_5', modulusLength: 2048,
  publicExponent: new Uint8Array([1,0,1]), hash: 'SHA-256' }, true, ['sign','verify']);
const jwk = { ...await crypto.subtle.exportKey('jwk', keypair.publicKey), kid: 'test-only', use: 'sig' };
const audience = 'https://yna.example.workers.dev/state';
const claims = { iss: 'https://token.actions.githubusercontent.com', aud: audience,
  repository: 'coolpint/obituary-personnel-webapp', repository_id: '857560108', ref: 'refs/heads/main',
  sub: 'repo:coolpint/obituary-personnel-webapp:ref:refs/heads/main',
  workflow_ref: 'coolpint/obituary-personnel-webapp/.github/workflows/yna-people-alert.yml@refs/heads/main',
  run_id: '123', run_attempt: '1', event_name: 'workflow_dispatch',
  iat: Math.floor(now/1000), nbf: Math.floor(now/1000)-1, exp: Math.floor(now/1000)+300 };
const b64 = value => Buffer.from(value).toString('base64url');
async function sign(overrides = {}, header = {}) {
  const unsigned = b64(JSON.stringify({ alg: 'RS256', kid: 'test-only', ...header })) + '.' + b64(JSON.stringify({ ...claims, ...overrides }));
  const signature = await crypto.subtle.sign('RSASSA-PKCS1-v1_5', keypair.privateKey, new TextEncoder().encode(unsigned));
  return unsigned + '.' + b64(signature);
}
const opts = { now, getKeys: async () => [jwk] };
test('valid signature and exact repository/main/workflow/audience yield collector identity', async () => {
  assert.deepEqual(await verifyOidc(await sign(), audience, opts), { run_id: '123', run_attempt: '1', role: 'collector' });
});
test('migration workflow receives separate initialize-only role', async () => {
  const token = await sign({ workflow_ref: claims.workflow_ref.replace('yna-people-alert.yml', 'yna-state-migration.yml') });
  assert.equal((await verifyOidc(token, audience, opts)).role, 'migration');
});
test('wrong repo ID, fork, ref, workflow, audience, subject and trigger fail even with valid signature', async () => {
  for (const override of [{ repository_id: '1' }, { repository: 'attacker/fork' }, { ref: 'refs/pull/1/merge' },
    { workflow_ref: claims.workflow_ref.replace('yna-people-alert.yml', 'other.yml') }, { aud: 'other' },
    { sub: 'repo:attacker/fork:ref:refs/heads/main' }, { event_name: 'pull_request' }, { iss: 'https://evil.invalid' }]) {
    await assert.rejects(verifyOidc(await sign(override), audience, opts));
  }
});
test('expired, future, old and incomplete tokens fail', async () => {
  for (const override of [{ exp: Math.floor(now/1000)-1 }, { nbf: Math.floor(now/1000)+60 },
    { iat: Math.floor(now/1000)-1000 }, { iat: null }, { exp: null }, { run_id: 'unsafe/path' }]) {
    await assert.rejects(verifyOidc(await sign(override), audience, opts));
  }
});
test('unsigned, tampered, wrong algorithm and unknown key tokens fail', async () => {
  const token = await sign();
  const parts = token.split('.');
  parts[1] = b64(JSON.stringify({ ...claims, run_id: '999' }));
  await assert.rejects(verifyOidc(parts.join('.'), audience, opts));
  await assert.rejects(verifyOidc(await sign({}, { alg: 'none' }), audience, opts));
  await assert.rejects(verifyOidc(await sign({}, { kid: 'other' }), audience, opts));
  await assert.rejects(verifyOidc('not-a-jwt', audience, opts));
});
test('authenticated HTTP forwards only verified identity to the private binding, not bearer credentials', async () => {
  const originalFetch = globalThis.fetch;
  let forwarded = 0;
  globalThis.fetch = async url => {
    assert.equal(url, 'https://token.actions.githubusercontent.com/.well-known/jwks');
    return Response.json({ keys: [jwk] });
  };
  const env = { STATE_API_ENABLED: 'true', STATE_ENDPOINT: audience,
    COORDINATOR: { idFromName: name => name, get: () => ({ async fetch(request) {
      forwarded++;
      assert.equal(request.headers.get('Authorization'), null);
      assert.equal(request.headers.get('X-Role'), 'collector');
      assert.equal(request.headers.get('X-Run-ID'), '123');
      assert.deepEqual(await request.json(), { action: 'acquire' });
      return Response.json({ revision: 1 });
    } }) } };
  try {
    const response = await handler.fetch(new Request(audience, { method:'POST', body:'{"action":"acquire"}',
      headers: { Authorization: 'Bearer ' + await sign(), 'X-Role': 'migration', 'X-Run-ID': 'attacker' } }), env);
    assert.equal(response.status, 200);
    assert.equal(forwarded, 1);
  } finally { globalThis.fetch = originalFetch; }
});
