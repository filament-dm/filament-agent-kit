// Run with: node --test filament-tab/tests/relay.test.mjs
import { test, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";
import { createHmac, randomBytes } from "node:crypto";
import relay from "../relay.mjs";

let env, mails, store, originalFetch;
beforeEach(() => {
  mails = [];
  store = new Map();
  env = {
    HOOK_PATH: "/hook/test", MAIL_TO: "test@mail.tab.bot",
    WEBHOOK_SECRET: "whsec_" + randomBytes(32).toString("base64"),
    EMAIL_API_KEY: "test-only",
    SEEN: {
      async get(key) { return store.get(key)?.value; },
      async put(key, value, options) { store.set(key, { value, ...options }); },
    },
  };
  originalFetch = globalThis.fetch;
  globalThis.fetch = async (url, options) => {
    mails.push({ url, headers: options.headers, ...JSON.parse(options.body) });
    return new Response('{"id":"mail-test"}');
  };
});
afterEach(() => { globalThis.fetch = originalFetch; });

const event = (data = {}) => ({ eventId: "delivery-1", name: "work.available", data: {
  event_id: "event-1", kind: "message", channel_id: "channel-1", is_backchannel: true,
  work: [{ body: "PRIVATE message must never be forwarded" }], ...data,
} });
function request(body = event(), { ts = String(Math.floor(Date.now() / 1000)),
  id = "delivery-1", signed = true, secret = env.WEBHOOK_SECRET } = {}) {
  const raw = typeof body === "string" ? body : JSON.stringify(body);
  const headers = { "Content-Type": "application/json" };
  if (signed) {
    headers["webhook-id"] = id;
    headers["webhook-timestamp"] = ts;
    headers["webhook-signature"] = "v1," + createHmac("sha256", Buffer.from(secret.slice(6), "base64"))
      .update(`${id}.${ts}.${raw}`).digest("base64");
  }
  return new Request("https://relay.test/hook/test", { method: "POST", body: raw, headers });
}

test("all unresolved placeholders and a missing SEEN store fail closed", async () => {
  for (const key of ["HOOK_PATH", "MAIL_TO", "WEBHOOK_SECRET", "EMAIL_API_KEY", "SEEN"]) {
    const incomplete = { ...env }; delete incomplete[key];
    const response = await relay.fetch(request(), incomplete);
    assert.equal(response.status, 500, key);
    assert.equal(await response.text(), "relay not configured");
  }
  assert.equal(mails.length, 0);
});

test("unsigned, stale, future, invalid and malformed signatures send no mail", async () => {
  const bad = [request(event(), { signed: false }),
    request(event(), { ts: String(Math.floor(Date.now() / 1000) - 601) }),
    request(event(), { ts: String(Math.floor(Date.now() / 1000) + 601) }),
    request(event(), { ts: "invalid" }),
    request(event(), { secret: "whsec_" + randomBytes(32).toString("base64") })];
  const malformed = request(); malformed.headers.set("webhook-signature", "v1,not-base64!"); bad.push(malformed);
  for (const req of bad) assert.equal((await relay.fetch(req, env)).status, 401);
  assert.equal(mails.length, 0);
  assert.equal(store.size, 0);
});

test("a rotated signature list can verify and echoes the challenge without mail", async () => {
  const req = request({ type: "verification", challenge: "echo-this" });
  req.headers.set("webhook-signature", "v1,bad v2,ignored " + req.headers.get("webhook-signature"));
  const response = await relay.fetch(req, env);
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { challenge: "echo-this" });
  assert.equal(mails.length, 0);
  assert.equal(store.size, 0);
});

test("empty work is dropped except invitations and vouches", async () => {
  const response = await relay.fetch(request(event({ work: [] })), env);
  assert.deepEqual(await response.json(), { ok: true, skipped: "items=0" });
  assert.equal(mails.length, 0);
  for (const kind of ["invite", "vouch"]) {
    assert.equal((await relay.fetch(request(event({ kind, event_id: kind, work: [] })), env)).status, 200);
  }
  assert.equal(mails.length, 2);
});

test("mail contains only routing metadata and uses 24-hour persistent event de-duplication", async () => {
  assert.equal((await relay.fetch(request(), env)).status, 200);
  assert.equal(mails.length, 1);
  assert.equal(mails[0].url, "https://api.resend.com/emails");
  assert.deepEqual(mails[0].to, [env.MAIL_TO]);
  assert.equal(mails[0].subject, "filament wake: message (backchannel) event-1");
  assert.match(mails[0].text, /event_id=event-1\nchannel_id=channel-1$/);
  assert.ok(!JSON.stringify(mails).includes("PRIVATE"));
  assert.equal(store.get("event:event-1").expirationTtl, 86400);
  // A separately loaded worker still consults the shared binding, not process memory.
  const fresh = (await import("../relay.mjs?new-instance")).default;
  const response = await fresh.fetch(request(event(), { id: "different-webhook-id" }), env);
  assert.deepEqual(await response.json(), { ok: true, duplicate: true });
  assert.equal(mails.length, 1);
  store.clear(); // simulate expiry: the next delivery is eligible again
  assert.equal((await relay.fetch(request(), env)).status, 200);
  assert.equal(mails.length, 2);
});

test("selftest and missing work counts send mail, with a separate ignore subject", async () => {
  const response = await relay.fetch(request(event({ kind: "selftest", work: undefined })), env);
  assert.equal(response.status, 200);
  assert.equal(mails[0].subject, "filament relay selftest (ignore): event-1");
  assert.ok(!mails[0].subject.startsWith("filament wake:"));
  assert.match(mails[0].text, /items=null/);
});

test("failed mail stays retryable and does not enter SEEN", async () => {
  globalThis.fetch = async () => new Response("unavailable", { status: 503 });
  assert.equal((await relay.fetch(request(), env)).status, 502);
  assert.equal(store.size, 0);
  globalThis.fetch = async () => { throw new Error("network failure"); };
  assert.equal((await relay.fetch(request(), env)).status, 502);
  assert.equal(store.size, 0);
});

test("wrong paths and malformed signed JSON send no mail", async () => {
  const response = await relay.fetch(new Request("https://relay.test/"), env);
  assert.equal(response.status, 404);
  for (const body of ["{", "null"]) assert.equal((await relay.fetch(request(body), env)).status, 400);
  assert.equal(mails.length, 0);
});
