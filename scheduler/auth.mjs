// GitHub-signed OIDC only. No shared state password, user input JWT or URL key.
const ISSUER = 'https://token.actions.githubusercontent.com';
const REPO = 'coolpint/obituary-personnel-webapp';
const WORKFLOWS = ['yna-people-alert.yml', 'yna-state-migration.yml'];
let cachedKeys, keysExpire = 0;
function decode(part) {
  return Uint8Array.from(atob(part.replace(/-/g, '+').replace(/_/g, '/')), c => c.charCodeAt(0));
}
export async function verifyOidc(token, audience, { now = Date.now(), getKeys } = {}) {
  if (!audience || token.length > 16000) throw new Error('Unauthorized');
  const parts = token.split('.');
  if (parts.length !== 3) throw new Error('Unauthorized');
  const header = JSON.parse(new TextDecoder().decode(decode(parts[0])));
  const claims = JSON.parse(new TextDecoder().decode(decode(parts[1])));
  const workflow = WORKFLOWS.find(name => claims.workflow_ref === `${REPO}/.github/workflows/${name}@refs/heads/main`);
  if (header.alg !== 'RS256' || typeof header.kid !== 'string' || claims.iss !== ISSUER || claims.aud !== audience ||
      claims.repository !== REPO || claims.repository_id !== '857560108' || claims.ref !== 'refs/heads/main' ||
      claims.sub !== `repo:${REPO}:ref:refs/heads/main` || !workflow ||
      !['schedule', 'workflow_dispatch'].includes(claims.event_name) ||
      !/^\d+$/.test(String(claims.run_id)) || !/^\d+$/.test(String(claims.run_attempt)) ||
      !Number.isFinite(claims.exp) || claims.exp * 1000 <= now ||
      !Number.isFinite(claims.nbf) || claims.nbf * 1000 > now + 30000 ||
      !Number.isFinite(claims.iat) || claims.iat * 1000 > now + 30000 || now - claims.iat * 1000 > 10 * 60000) {
    throw new Error('Unauthorized');
  }
  let keys;
  if (getKeys) keys = await getKeys();
  else {
    if (!cachedKeys || keysExpire <= now) {
      const response = await fetch(`${ISSUER}/.well-known/jwks`, { redirect: 'error', signal: AbortSignal.timeout(5000) });
      if (!response.ok) throw new Error('Unauthorized');
      cachedKeys = (await response.json()).keys;
      keysExpire = now + 5 * 60000;
    }
    keys = cachedKeys;
  }
  const jwk = keys.find(key => key.kid === header.kid && key.kty === 'RSA' && key.use === 'sig');
  if (!jwk) throw new Error('Unauthorized');
  const key = await crypto.subtle.importKey('jwk', jwk, { name: 'RSASSA-PKCS1-v1_5', hash: 'SHA-256' }, false, ['verify']);
  const valid = await crypto.subtle.verify('RSASSA-PKCS1-v1_5', key, decode(parts[2]), new TextEncoder().encode(parts[0] + '.' + parts[1]));
  if (!valid) throw new Error('Unauthorized');
  return { run_id: String(claims.run_id), run_attempt: String(claims.run_attempt),
    role: workflow === 'yna-state-migration.yml' ? 'migration' : 'collector' };
}
