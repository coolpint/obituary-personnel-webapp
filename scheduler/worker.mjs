// No public HTTP endpoint. Only scheduled events reach the named coordinator.
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
      const { time, cron } = await request.json();
      if (this.env.ENABLED !== "true") return new Response("disabled");
      const slot = eventSlot(time, cron);
      if (cron.startsWith("27 ")) await this.watch(slot);
      else await this.dispatch(slot);
      return new Response("ok");
    });
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
      const response = await this.github(`contents/health.json?ref=${encodeURIComponent(this.env.STATE_BRANCH)}`, {
        headers: { Accept: "application/vnd.github.raw+json" },
      });
      problem = healthProblem(await response.json(), slot);
    } catch {
      problem = "완료 기록 조회 실패 (GitHub 장애·인증·상태 저장 확인 필요)";
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
