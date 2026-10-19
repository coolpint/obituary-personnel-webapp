import { test, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { Coordinator, eventSlot, healthProblem } from './worker.mjs';

const originalFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = originalFetch; });
const slot = '2026-10-19T10:07+09:00';
function fixture() {
  const memory = new Map();
  let tail = Promise.resolve();
  const ctx = {
    storage: { async get(k) { return memory.get(k); }, async put(k, v) { memory.set(k, v); } },
    blockConcurrencyWhile(callback) {
      const result = tail.then(callback);
      tail = result.catch(() => {});
      return result;
    },
  };
  const env = { ENABLED: 'true', GITHUB_REPOSITORY: 'coolpint/obituary-personnel-webapp',
    GITHUB_WORKFLOW: 'yna-people-alert.yml', STATE_BRANCH: 'alert-state',
    GITHUB_DISPATCH_TOKEN: 'fake-token-never-log', SLACK_WEBHOOK_URL: 'https://example.invalid/slack' };
  return { memory, env, coordinator: new Coordinator(ctx, env) };
}
function event(cron, time) {
  return new Request('https://internal/scheduled', { method: 'POST',
    body: JSON.stringify({ cron, time: Date.parse(time) }) });
}

test('collection and watchdog use the same explicit KST date and slot', () => {
  assert.equal(eventSlot(Date.parse('2026-10-19T01:07:00Z'), '7 1,9 * * *'), slot);
  assert.equal(eventSlot(Date.parse('2026-10-19T01:27:00Z'), '27 1,9 * * *'), slot);
  assert.equal(eventSlot(Date.parse('2026-10-19T09:27:00Z'), '27 1,9 * * *'), '2026-10-19T18:07+09:00');
  assert.throws(() => eventSlot(Date.now(), '* * * * *'));
});
test('watchdog distinguishes no run, incomplete, failed, and healthy completion', () => {
  assert.match(healthProblem({ slots: [] }, slot), /없음/);
  for (const status of ['running', 'failed']) assert.match(healthProblem({ slots: [{ slot, status }] }, slot), /미완료/);
  assert.equal(healthProblem({ slots: [{ slot, status: 'succeeded' }], delivery_problems: 0 }, slot), null);
  assert.match(healthProblem({ slots: [{ slot, status: 'succeeded' }], delivery_problems: 1 }, slot), /전송/);
});
test('concurrent repeated dispatches submit one request to main with the nominal slot', async () => {
  const { coordinator, memory } = fixture(); let calls = 0;
  globalThis.fetch = async (url, options) => {
    calls++;
    assert.match(url, /actions\/workflows\/yna-people-alert.yml\/dispatches$/);
    assert.deepEqual(JSON.parse(options.body), { ref: 'main', inputs: { slot } });
    await Promise.resolve();
    return new Response(null, { status: 204 });
  };
  await Promise.all([1, 2, 3].map(() => coordinator.fetch(event('7 1,9 * * *', '2026-10-19T01:07:00Z'))));
  assert.equal(calls, 1); assert.equal(memory.get('dispatch:' + slot), 'accepted');
});
test('no-run watchdog warns independently and once across retries', async () => {
  const { coordinator, memory } = fixture(); let warnings = 0;
  globalThis.fetch = async (url, options) => {
    if (url.includes('api.github.com')) return Response.json({ slots: [] });
    warnings++; assert.match(JSON.parse(options.body).text, /실행 기록 없음/);
    return new Response('ok');
  };
  await coordinator.watch(slot); await coordinator.watch(slot);
  assert.equal(warnings, 1); assert.equal(memory.get('warning:' + slot), 'sent');
});
test('healthy completion does not send a watchdog warning', async () => {
  const { coordinator } = fixture(); let calls = 0;
  globalThis.fetch = async url => {
    calls++; assert.match(url, /health.json/);
    return Response.json({ slots: [{ slot, status: 'succeeded' }] });
  };
  await coordinator.watch(slot); assert.equal(calls, 1);
});
test('GitHub outage or expired auth still produces an independent Slack warning', async () => {
  const { coordinator } = fixture(); let warnings = 0;
  globalThis.fetch = async url => {
    if (url.includes('api.github.com')) return new Response('', { status: 401 });
    warnings++; return new Response('ok');
  };
  await coordinator.watch(slot); assert.equal(warnings, 1);
});
test('uncertain dispatch can retry safely; only one warning is emitted', async () => {
  const { coordinator, memory } = fixture(); let warnings = 0, dispatches = 0;
  globalThis.fetch = async url => {
    if (url.includes('api.github.com')) { dispatches++; throw new Error('fake-secret-url'); }
    warnings++; return new Response('ok');
  };
  await assert.rejects(coordinator.dispatch(slot), /Dispatch failed/);
  await assert.rejects(coordinator.dispatch(slot), /Dispatch failed/);
  assert.equal(warnings, 1); assert.equal(memory.get('dispatch:' + slot), undefined);
  globalThis.fetch = async () => new Response(null, { status: 204 });
  await coordinator.dispatch(slot); assert.equal(dispatches, 2);
  assert.equal(memory.get('dispatch:' + slot), 'accepted');
});
test('uncertain warning is held, never blindly resent', async () => {
  const { coordinator, memory } = fixture(); let calls = 0;
  globalThis.fetch = async () => { calls++; throw new Error('secret URL must not surface'); };
  await assert.rejects(coordinator.warn(slot, 'test'), /manual check required/);
  await coordinator.warn(slot, 'test');
  assert.equal(calls, 1); assert.equal(memory.get('warning:' + slot), 'uncertain');
});
test('disabled scheduler never dispatches or sends', async () => {
  const { coordinator, env } = fixture(); env.ENABLED = 'false';
  globalThis.fetch = async () => { assert.fail('real HTTP forbidden'); };
  assert.equal(await (await coordinator.fetch(event('7 1,9 * * *', '2026-10-19T01:07:00Z'))).text(), 'disabled');
});
test('token is not sent to arbitrary repository configuration', async () => {
  const { coordinator, env } = fixture(); env.GITHUB_REPOSITORY = 'other/repository';
  globalThis.fetch = async () => { assert.fail('must not send token'); };
  await assert.rejects(coordinator.github('anything'), /Repository mismatch/);
});
