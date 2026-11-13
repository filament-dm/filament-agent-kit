// Filament work.available -> mail to your own Tab address; ids only, never message text.
// Configure the environment, or replace these placeholders in a PRIVATE published copy.
const DEFAULTS = {
  HOOK_PATH: "/hook/<random>",
  MAIL_TO: "<handle>@mail.tab.bot",
  WEBHOOK_SECRET: "whsec_<base64-32-bytes>",
  EMAIL_API_KEY: "<email-api-key>",
};
const MAIL_FROM = "Filament wake relay <onboarding@resend.dev>";
const SEEN_TTL = 24 * 60 * 60;
const json = (o, status = 200) => new Response(JSON.stringify(o), {
  status, headers: { "content-type": "application/json" },
});
const b64 = (s) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));

async function verify(headers, raw, secret) {
  const id = headers.get("webhook-id"), ts = headers.get("webhook-timestamp");
  const signature = headers.get("webhook-signature");
  if (!id || !ts || !signature || !/^\d+$/.test(ts)) return false;
  if (Math.abs(Date.now() / 1000 - Number(ts)) > 600) return false;
  try {
    const key = await crypto.subtle.importKey("raw", b64(secret.replace(/^whsec_/, "")),
      { name: "HMAC", hash: "SHA-256" }, false, ["verify"]);
    const data = new TextEncoder().encode(`${id}.${ts}.${raw}`);
    for (const part of signature.split(" ")) {
      const [version, mac] = part.split(",");
      if (version !== "v1" || !mac) continue;
      try { if (await crypto.subtle.verify("HMAC", key, b64(mac), data)) return true; } catch {}
    }
  } catch {}
  return false;
}

export default {
  async fetch(req, env = {}) {
    const config = Object.fromEntries(Object.entries(DEFAULTS).map(([key, value]) =>
      [key, env[key] ?? value]));
    if (Object.values(config).some((v) => typeof v !== "string" || !v.trim() || /[<>]/.test(v)) ||
        !env.SEEN || typeof env.SEEN.get !== "function" || typeof env.SEEN.put !== "function") {
      return new Response("relay not configured", { status: 500 });
    }
    if (new URL(req.url).pathname !== config.HOOK_PATH || req.method !== "POST") {
      return new Response("not found", { status: 404 });
    }
    const raw = await req.text();
    if (!await verify(req.headers, raw, config.WEBHOOK_SECRET)) {
      return json({ ok: false, error: "invalid or stale signature" }, 401);
    }
    let body;
    try { body = JSON.parse(raw); } catch { return json({ ok: false, error: "invalid JSON" }, 400); }
    if (!body || typeof body !== "object") return json({ ok: false, error: "invalid body" }, 400);
    if (body.type === "verification") return json({ challenge: body.challenge });

    const d = body.data || body.payload || {};
    const kind = String(d.kind || "event");
    const items = Array.isArray(d.work) ? d.work.length : null;
    if (items === 0 && kind !== "invite" && kind !== "vouch") {
      return json({ ok: true, skipped: "items=0" });
    }
    const id = String(d.event_id || body.eventId || req.headers.get("webhook-id"));
    const seenKey = "event:" + id;
    try {
      if (await env.SEEN.get(seenKey)) return json({ ok: true, duplicate: true });
      const selftest = kind === "selftest";
      const subject = selftest ? `filament relay selftest (ignore): ${id}` :
        `filament wake: ${kind}${d.is_backchannel ? " (backchannel)" : ""} ${id}`;
      const text = (selftest ? "Relay self-test; end quietly." : "Filament work.available. Run the listener.") +
        `\nkind=${kind}\nbackchannel=${!!d.is_backchannel}\nitems=${items}` +
        `\nevent_id=${id}\nchannel_id=${d.channel_id || ""}`;
      // The same idempotency key also limits concurrent/retried sends while KV catches up.
      const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(id));
      const mailId = Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
      const response = await fetch("https://api.resend.com/emails", {
        method: "POST",
        headers: { Authorization: "Bearer " + config.EMAIL_API_KEY, "Content-Type": "application/json",
          "Idempotency-Key": "filament-" + mailId },
        body: JSON.stringify({ from: MAIL_FROM, to: [config.MAIL_TO], subject, text }),
      });
      if (!response.ok) return json({ ok: false, error: "mail failed" }, 502);
      // Record only accepted mail: a failed send must remain retryable.
      await env.SEEN.put(seenKey, "1", { expirationTtl: SEEN_TTL });
      return json({ ok: true });
    } catch {
      return json({ ok: false, error: "relay delivery failed" }, 502);
    }
  },
};
