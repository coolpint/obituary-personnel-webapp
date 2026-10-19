import { verifyOidc } from './auth.mjs';

export function validateSnapshot(value) {
  if (!value || value.version !== 1 ||
      Object.keys(value).sort().join(',') !== 'active,history,notifications,slots,version' ||
      !['history','active','slots','notifications'].every(key => Array.isArray(value[key])) ||
      value.slots.length > 60 || value.notifications.length > 120 ||
      new TextEncoder().encode(JSON.stringify(value)).length > 1024 * 1024) throw new Error('Invalid state');
  for (const row of value.history) {
    if (!Array.isArray(row) || row.length !== 3 || !/^[a-f0-9]{64}$/.test(row[0]) ||
        !['slack','telegram','console'].includes(row[1]) || !['sent','skipped_legacy'].includes(row[2])) throw new Error('Invalid history');
  }
  for (const row of value.active) {
    if (!row.item || !row.classification || !['slack','telegram','console'].includes(row.channel) ||
        !['pending','failed','in_flight','uncertain'].includes(row.status) ||
        Object.keys(row).sort().join(',') !== 'channel,classification,error,item,sent_at,status' ||
        Object.keys(row.item).sort().join(',') !== 'guid,link,published_at,summary,title' ||
        Object.keys(row.classification).sort().join(',') !== 'category,is_media_related,matched_category_keywords,matched_outlets,matched_roles,media_score') throw new Error('Invalid active state');
  }
  for (const row of value.slots) {
    if (Object.keys(row).sort().join(',') !== 'completed_at,error,run_id,slot,started_at,status' ||
        !['running','failed','succeeded'].includes(row.status)) throw new Error('Invalid slot state');
  }
  for (const row of value.notifications) {
    if (Object.keys(row).sort().join(',') !== 'name,status' ||
        !['in_flight','sent'].includes(row.status)) throw new Error('Invalid notification state');
  }
  return value;
}
export function eventSlot(time, cron) {
  if (!["7 1,9 * * *", "27 1,9 * * *"].includes(cron)) throw new Error("Unknown cron");
  const date = new Date(time);
  if (![1, 9].includes(date.getUTCHours()) ||
      date.getUTCMinutes() !== (cron.startsWith("27 ") ? 27 : 7)) throw new Error("Invalid event time");
  date.setUTCMinutes(7, 0, 0);
  const kst = new Date(date.getTime() + 9 * 3600000);
  return kst.toISOString().slice(0, 16) + "+09:00";
}

export function healthProblem(health, slot) {
  if (!health || !Array.isArray(health.slots)) return "완료 기록을 읽을 수 없음";
  const record = health.slots.find(row => row.slot === slot);
  if (!record) return "실행 기록 없음";
  if (record.status !== "succeeded") return `회차 미완료 (${record.status})`;
  if (health.delivery_problems) return "전송 확인 필요";
  return null;
}

export class Coordinator {
  constructor(ctx, env) { this.ctx = ctx; this.env = env; }

  // Explicit serialization includes awaits to external APIs. DO's ordinary
  // event ordering alone would allow duplicate requests during those awaits.
  async fetch(request) {
    return this.ctx.blockConcurrencyWhile(async () => {
      if (new URL(request.url).pathname === '/state') {
        // Only the authenticated outer Worker can call this private binding.
        const claims = { run_id: request.headers.get('X-Run-ID'), run_attempt: request.headers.get('X-Run-Attempt'),
          role: request.headers.get('X-Role') };
        const operation = await request.json();
        return this.stateOperation(claims, operation);
      }
      const { time, cron } = await request.json();
      if (this.env.ENABLED !== "true") return new Response("disabled");
      const slot = eventSlot(time, cron);
      if (cron.startsWith("27 ")) await this.watch(slot);
      else await this.dispatch(slot);
      return new Response("ok");
    });
  }

  async stateOperation(claims, operation) {
    const reply = (value, status = 200) => Response.json(value, { status, headers: { 'Cache-Control': 'no-store' } });
    const current = await this.ctx.storage.get('state');
    const owner = `${claims.run_id}:${claims.run_attempt}`;
    if (operation.action === 'initialize') {
      if (claims.role !== 'migration' || this.env.ALLOW_INITIALIZE !== 'true' || current) return reply({}, 409);
      if (!operation.source || Object.keys(operation.source).sort().join(',') !== 'cache_key,sha256' ||
          !/^yna-people-alert-data-\d+$/.test(operation.source.cache_key) || !/^[a-f0-9]{64}$/.test(operation.source.sha256)) return reply({}, 400);
      const initial = { revision: 1, snapshot: validateSnapshot(operation.snapshot) };
      await this.ctx.storage.put({ state: initial, source: operation.source });
      return reply({ revision: 1 });
    }
    if (claims.role !== 'collector' || !current) return reply({}, 409);
    let lease = await this.ctx.storage.get('lease');
    if (operation.action === 'acquire') {
      if (lease && lease.owner !== owner) {
        // Never expire a lease based on wall time: a paused runner could still
        // send after takeover. Confirm the prior Actions run has ended first.
        const response = await this.github(`actions/runs/${lease.run_id}/attempts/${lease.run_attempt}`);
        if ((await response.json()).status !== 'completed') return reply({}, 409);
        lease = null;
      }
      await this.ctx.storage.put('lease', { owner, run_id: claims.run_id, run_attempt: claims.run_attempt });
      return reply(current);
    }
    if (!lease || lease.owner !== owner) return reply({}, 409);
    if (operation.action === 'release') {
      await this.ctx.storage.delete('lease');
      return reply({ released: true });
    }
    if (operation.action === 'checkpoint' && Number.isInteger(operation.revision) && operation.revision === current.revision) {
      const next = { revision: current.revision + 1, snapshot: validateSnapshot(operation.snapshot) };
      const history = new Map(next.snapshot.history.map(row => [row[0] + ':' + row[1], row[2]]));
      if (current.snapshot.history.some(row => history.get(row[0] + ':' + row[1]) !== row[2])) return reply({}, 409);
      await this.ctx.storage.put('state', next);
      return reply({ revision: next.revision });
    }
    return reply({}, 409);
  }

  async github(path, options = {}) {
    // Prevent an accidentally configured arbitrary credential destination.
    if (this.env.GITHUB_REPOSITORY !== "coolpint/obituary-personnel-webapp") throw new Error("Repository mismatch");
    const response = await fetch(`https://api.github.com/repos/${this.env.GITHUB_REPOSITORY}/${path}`, {
      ...options,
      headers: {
        Authorization: `Bearer ${this.env.GITHUB_DISPATCH_TOKEN}`,
        Accept: "application/vnd.github+json", "User-Agent": "yna-scheduler",
        "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json",
        ...options.headers,
      },
      redirect: "error", signal: AbortSignal.timeout(10000),
    });
    // Never print bodies, request objects, URLs or exception messages.
    if (!response.ok) throw new Error(`GitHub HTTP ${response.status}`);
    return response;
  }

  async dispatch(slot) {
    const key = "dispatch:" + slot;
    if (await this.ctx.storage.get(key) === "accepted") return;
    try {
      await this.github(`actions/workflows/${encodeURIComponent(this.env.GITHUB_WORKFLOW)}/dispatches`, {
        method: "POST", body: JSON.stringify({ ref: "main", inputs: { slot } }),
      });
      await this.ctx.storage.put(key, "accepted");
    } catch {
      // A timeout may already have created the run. Retrying dispatch is safe
      // because the collector, not dispatch acceptance, owns slot idempotency.
      await this.warn(slot, "예약 실행 요청 결과를 확인할 수 없음");
      throw new Error("Dispatch failed; check scheduler and Actions");
    }
  }

  async watch(slot) {
    let problem;
    try {
      const state = await this.ctx.storage.get('state');
      const health = state?.snapshot;
      problem = healthProblem(health && { slots: health.slots, delivery_problems: health.active.length }, slot);
    } catch {
      problem = "비공개 완료 기록 조회 실패 (상태 저장 확인 필요)";
    }
    if (problem) await this.warn(slot, problem);
    await this.ctx.storage.put("watch:" + slot, { problem, checked: true });
  }

  async warn(slot, reason) {
    const key = "warning:" + slot;
    if (await this.ctx.storage.get(key)) return;
    // Save intent first. A crash/timeout must not replay an operational alert.
    // An uncertain send needs manual reconciliation, visible in DO storage/logs.
    await this.ctx.storage.put(key, "in_flight");
    try {
      const response = await fetch(this.env.SLACK_WEBHOOK_URL, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: `[경고] YNA People Alert 예약 실행 확인 필요\n회차: ${slot}\n${reason}\nGitHub Actions와 외부 예약기 로그를 확인하세요.` }),
        redirect: "error", signal: AbortSignal.timeout(10000),
      });
      if (!response.ok) throw new Error("Warning delivery failed");
      await this.ctx.storage.put(key, "sent");
    } catch {
      await this.ctx.storage.put(key, "uncertain");
      throw new Error("Watchdog notification uncertain; manual check required");
    }
  }
}

export default {
  async fetch(request, env) {
    if (new URL(request.url).pathname !== '/state' || request.method !== 'POST') return new Response('', { status: 404 });
    if (env.STATE_API_ENABLED !== 'true') return new Response('', { status: 503 });
    try {
      if (Number(request.headers.get('Content-Length') || 0) > 1100000) return new Response('', { status: 413 });
      const auth = request.headers.get('Authorization') || '';
      if (!auth.startsWith('Bearer ')) return new Response('', { status: 403 });
      const claims = await verifyOidc(auth.slice(7), env.STATE_ENDPOINT);
      const body = await request.arrayBuffer();
      if (body.byteLength > 1100000) return new Response('', { status: 413 });
      const id = env.COORDINATOR.idFromName('yna-scheduler-v1');
      return await env.COORDINATOR.get(id).fetch(new Request('https://internal/state', {
        method: 'POST', body,
        headers: { 'X-Run-ID': claims.run_id, 'X-Run-Attempt': claims.run_attempt, 'X-Role': claims.role },
      }));
    } catch {
      return new Response('', { status: 403, headers: { 'Cache-Control': 'no-store' } });
    }
  },
  async scheduled(controller, env, ctx) {
    const id = env.COORDINATOR.idFromName("yna-scheduler-v1");
    ctx.waitUntil((async () => {
      const response = await env.COORDINATOR.get(id).fetch(new Request("https://internal/scheduled", {
        method: "POST", body: JSON.stringify({ time: controller.scheduledTime, cron: controller.cron }),
      }));
      if (!response.ok) throw new Error("Coordinator failed");
    })());
  },
};
