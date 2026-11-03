/**
 * Clips Kitty Meta relay: the small Cloudflare Worker that sits between a
 * creator's phone and the Clips Kitty PC that renders their highlights.
 * New for the Meta hackathon edition.
 *
 * Why it exists. WhatsApp and Meta call webhooks and OAuth redirects on a
 * public HTTPS address, and Meta's app secret has to stay on a server. The
 * PC must stay closed to the internet. So the PC talks OUT to this worker
 * (signed requests, polling), and this worker is the only thing Meta ever
 * talks to. It holds:
 *
 *   - the WhatsApp Cloud API token and the Meta app secret
 *     (wrangler secrets, never sent anywhere);
 *   - the Instagram and Facebook Page tokens the creator connected
 *     (sealed with TOKEN_KEY in D1, never sent to the PC);
 *   - offers of clips, behind single-purpose links that expire, and the
 *     decisions made on them, until the PC collects them;
 *   - a small preview video of each offered clip, when the PC sends one,
 *     until the offer's link expires (KV).
 *
 * What it can do is narrow on purpose. It cannot read the PC's files or
 * call its local API: it can only answer the PC's own requests. It can
 * only publish a clip the phone approved, to a platform the phone ticked,
 * under the command the PC accepted, within a day of the approval, and
 * only bytes the PC chooses to send it.
 *
 * The creator's Muse agent reads offers here too, as a custom connector
 * (lib/muse.js), with a key the PC made. It can hand out the approval link,
 * defer and cancel; it can never approve or publish.
 *
 * Routes
 *   GET  /health
 *   GET  /webhook                 WhatsApp verification (hub.challenge)
 *   POST /webhook                 WhatsApp events, X-Hub-Signature-256 checked
 *   GET  /a/:token                the approval page (read-only on GET)
 *   POST /a/:token                approve (PIN + confirm), remind me later, cancel
 *   GET  /a/:token/thumb/:clip    a clip's thumbnail
 *   GET  /a/:token/preview/:clip  a clip's preview video, in byte ranges
 *   GET  /oauth/facebook/callback, POST /oauth/facebook/choose
 *   POST /mcp, /muse/v1/...       the Muse connector (a Muse key, lib/muse.js)
 *   GET  /muse, /muse/openapi.json  how to connect Muse, and the REST routes described
 *   POST /v1/devices              register a PC (ENROLL_KEY bearer)
 *   *    /v1/...                  everything else the PC does, HMAC-signed
 *
 * Secrets (npx wrangler secret put NAME): ENROLL_KEY, TOKEN_KEY (32 random
 * bytes, base64), WHATSAPP_TOKEN, WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_WABA_ID,
 * WEBHOOK_VERIFY_TOKEN, META_APP_ID, META_APP_SECRET. Vars (wrangler.toml):
 * PUBLIC_URL, WHATSAPP_DISPLAY_NUMBER.
 * D1 binding: DB (schema.sql). KV binding: PREVIEWS, optional (without it the
 * page shows thumbnails only). Cron: every 10 minutes (expiry, reminders).
 */

import { b64url, canonical, hmacHex, hmacVerify, pinHash, randomId, randomToken, safeEqual, seal, sha256Hex, unseal } from "./lib/crypto.js";
import { addCommand, all, audit, iso, now, one, parse, pinBlocked, prefsOf, run } from "./lib/db.js";
import * as meta from "./lib/meta.js";
import * as muse from "./lib/muse.js";
import * as page from "./lib/page.js";
import * as wa from "./lib/whatsapp.js";

export const VERSION = "1.1.0";
const SKEW = 300; // seconds a signed request's clock may be off
const NONCE_TTL = 900;
const PAIR_TTL = 600;
const MAX_OFFER_HOURS = 72;
const PUBLISH_WINDOW = 24 * 3600; // after an approval, its publish steps are allowed this long
const STATUS_TTL = 7 * 86400; // the page keeps showing results this long
const PIN_MAX_FAILS = 5;
const PIN_LOCK = 15 * 60;
const MAX_JSON = 1_500_000; // an offer with ten thumbnails
const MAX_UPLOAD = 100 * 1024 * 1024;
const MAX_PREVIEW = 4 * 1024 * 1024; // one clip's preview video
const PLATFORMS = ["instagram", "facebook"];
const CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789";

export default {
  async fetch(request, env, ctx) {
    try {
      return await route(request, env, ctx);
    } catch (e) {
      if (e instanceof HttpError) return json({ code: e.code, error: e.message, ...e.extra }, e.status);
      console.error("relay error:", e && e.name, e && e.code);
      return json({ code: "relay_error", error: "The relay hit an error. Nothing was published." }, 500);
    }
  },
  async scheduled(event, env, ctx) {
    ctx.waitUntil(cron(env));
  },
};

class HttpError extends Error {
  constructor(status, code, message, extra = {}) {
    super(message);
    this.status = status;
    this.code = code;
    this.extra = extra;
  }
}

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff" },
  });
}

function html(body, status = 200) {
  return new Response(body, {
    status,
    headers: {
      "Content-Type": "text/html; charset=utf-8",
      "Cache-Control": "no-store",
      // same-origin, not no-referrer: under no-referrer a browser posts the
      // approval page's own form with Origin: null, which decide() can accept
      // only with Sec-Fetch-Site: same-origin, and Safari before 16.4 and
      // Firefox before 90 send no Sec-Fetch-Site. Either way nothing, the
      // link's token included, goes to another site.
      "Referrer-Policy": "same-origin",
      "X-Content-Type-Options": "nosniff",
      "X-Frame-Options": "DENY",
      "Content-Security-Policy": "default-src 'none'; img-src 'self'; media-src 'self'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
    },
  });
}

function redirect(to) {
  return new Response(null, { status: 303, headers: { Location: to, "Cache-Control": "no-store", "Referrer-Policy": "same-origin" } });
}

// ---- routing ---------------------------------------------------------------

async function route(request, env, ctx) {
  const url = new URL(request.url);
  const path = url.pathname;
  const method = request.method;

  if (path === "/health") return json({ ok: true, version: VERSION, previews: Boolean(env.PREVIEWS) });
  if (path === "/webhook") {
    if (method === "GET") return verifyWebhook(url, env);
    if (method === "POST") return webhook(request, env, ctx);
    return json({ error: "not found" }, 404);
  }
  let m = path.match(/^\/a\/([A-Za-z0-9_-]{30,100})\/thumb\/(\d{1,12})$/);
  if (m && method === "GET") return thumb(env, m[1], Number(m[2]));
  m = path.match(/^\/a\/([A-Za-z0-9_-]{30,100})\/preview\/(\d{1,12})$/);
  if (m && method === "GET") return preview(request, env, m[1], Number(m[2]));
  m = path.match(/^\/a\/([A-Za-z0-9_-]{30,100})$/);
  if (m && method === "GET") return showOffer(env, m[1]);
  if (m && method === "POST") return decide(request, env, m[1]);
  if (path === "/oauth/facebook/callback" && method === "GET") return facebookCallback(env, url);
  if (path === "/oauth/facebook/choose" && method === "POST") return facebookPagePicked(request, env);
  if (muse.handles(path)) return muse.route(request, env, url);
  if (path.startsWith("/v1/") && /^Bearer\s+ckm_/i.test(request.headers.get("Authorization") || "")) {
    // A Muse key reads offers through /mcp and /muse/v1 only. It never stands in for the PC.
    throw new HttpError(401, "forbidden", "A Muse key can't call this PC's routes.");
  }
  if (path === "/v1/devices" && method === "POST") return enroll(request, env);
  if (path.startsWith("/v1/")) {
    const auth = await authenticate(request, env, url);
    return deviceRoute(request, env, url, auth);
  }
  return json({ error: "not found" }, 404);
}

async function deviceRoute(request, env, url, auth) {
  const { device } = auth;
  const path = url.pathname;
  const method = request.method;
  const body = auth.raw ? parse(auth.raw, null) : {};
  if (auth.raw && (body === null || typeof body !== "object" || Array.isArray(body))) throw new HttpError(400, "bad_request", "The request body must be a JSON object.");

  if (path === "/v1/status" && method === "GET") return json(await status(env, device));
  if (path === "/v1/preferences" && method === "POST") return json(await preferences(env, device, body));
  if (path === "/v1/pair/start" && method === "POST") return json(await pairStart(env, device));
  if (path === "/v1/pair/confirm" && method === "POST") return json(await pairConfirm(env, device, body));
  if (path === "/v1/pin" && method === "POST") return json(await setPin(env, device, body));
  if (path === "/v1/disconnect" && method === "POST") return json(await unpair(env, device));
  if (path === "/v1/muse/token" && method === "POST") return json(await museToken(env, device));
  if (path === "/v1/muse/revoke" && method === "POST") return json(await museRevoke(env, device));
  if (path === "/v1/notify" && method === "POST") return json(await notify(env, device, body));
  if (path === "/v1/commands" && method === "GET") return json(await pendingCommands(env, device));
  let m = path.match(/^\/v1\/commands\/(cmd_[0-9a-f]{24})\/ack$/);
  if (m && method === "POST") return json(await ack(env, device, m[1], body));
  m = path.match(/^\/v1\/approvals\/(apr_[0-9a-f]{24})\/cancel$/);
  if (m && method === "POST") return json(await cancelFromPc(env, device, m[1]));
  if (path === "/v1/templates") {
    if (method === "GET") return json({ templates: await safeTemplates(env, false) });
    if (method === "POST") return json({ templates: await safeTemplates(env, true) });
  }
  if (path === "/v1/accounts/connect" && method === "POST") {
    try {
      return json({ url: await meta.startConnect(env, device.device_id, String(body.platform || "")) });
    } catch (e) {
      throw metaHttp(e);
    }
  }
  if (path === "/v1/accounts/disconnect" && method === "POST") {
    const platform = String(body.platform || "");
    if (!["facebook", "instagram"].includes(platform)) throw new HttpError(400, "bad_request", "Unknown platform.");
    await meta.disconnect(env, device.device_id, platform);
    await audit(env, { device: device.device_id, actor: "pc", action: "account_disconnected", detail: platform });
    return json({ accounts: await meta.accountsFor(env, device.device_id) });
  }
  if (path === "/v1/meta/upload" && method === "POST") return json(await metaUpload(request, env, device, url));
  if (path === "/v1/previews" && method === "POST") return json(await uploadPreview(request, env, device, url));
  m = path.match(/^\/v1\/meta\/([a-z_]{6,20})$/);
  if (m && method === "POST") return json(await metaStep(env, device, m[1], body));
  throw new HttpError(404, "not_found", "Unknown relay route.");
}

// ---- the PC: enrolment and signed requests ----------------------------------

async function enroll(request, env) {
  const given = (request.headers.get("Authorization") || "").replace(/^Bearer\s+/i, "");
  if (!env.ENROLL_KEY || !given || !(await safeEqual(given, env.ENROLL_KEY))) {
    throw new HttpError(401, "forbidden", "That enrolment key is not this relay's.");
  }
  const body = parse(await readText(request, 4096), {});
  const id = String(body.device_id || "");
  const secret = String(body.device_secret || "");
  if (!/^dev_[0-9a-f]{16}$/.test(id) || secret.length < 32 || secret.length > 128) {
    throw new HttpError(400, "bad_request", "The device id or secret is malformed.");
  }
  await run(env, `INSERT INTO devices (device_id, secret_enc, name, created_at) VALUES (?, ?, ?, ?)
    ON CONFLICT(device_id) DO UPDATE SET secret_enc = excluded.secret_enc, name = excluded.name`,
  id, await seal(env, secret), String(body.name || "").slice(0, 60), now());
  await audit(env, { device: id, actor: "pc", action: "enrolled" });
  return json({ ok: true, device_id: id });
}

async function authenticate(request, env, url) {
  const id = request.headers.get("X-CK-Device") || "";
  const ts = Number(request.headers.get("X-CK-Timestamp") || 0);
  const nonce = request.headers.get("X-CK-Nonce") || "";
  const sig = request.headers.get("X-CK-Signature") || "";
  if (!id || !nonce || !sig || !/^[0-9a-f]{16,64}$/.test(nonce)) throw new HttpError(401, "unsigned", "This request is not signed.");
  if (Math.abs(now() - ts) > SKEW) throw new HttpError(401, "clock", "This PC's clock is more than five minutes off. Fix the time and try again.");
  const device = await one(env, "SELECT * FROM devices WHERE device_id = ?", id);
  if (!device) throw new HttpError(401, "not_registered", "This PC is not registered with the relay.");
  const secret = await unseal(env, device.secret_enc);
  // Video bytes (a chunk to publish, a clip's preview) are not hashed: the signed query says what they are.
  const streaming = url.pathname === "/v1/meta/upload" || url.pathname === "/v1/previews";
  const raw = streaming ? "" : await readText(request, MAX_JSON);
  const bodyHash = streaming ? "UNSIGNED-PAYLOAD" : await sha256Hex(raw);
  const message = [request.method.toUpperCase(), url.pathname + url.search, String(ts), nonce, bodyHash].join("\n");
  if (!(await hmacVerify(secret, message, sig))) throw new HttpError(401, "bad_signature", "The request's signature did not check out.");
  const fresh = await run(env, "INSERT OR IGNORE INTO nonces (device_id, nonce, at) VALUES (?, ?, ?)", id, nonce, now());
  if (!fresh) throw new HttpError(401, "replay", "This request was already used.");
  await run(env, "UPDATE devices SET last_seen = ? WHERE device_id = ?", now(), id);
  return { device: { ...device, secret }, raw };
}

async function readText(request, limit) {
  const declared = Number(request.headers.get("Content-Length") || 0);
  if (declared > limit) throw new HttpError(413, "too_large", "The request is too large.");
  const text = await request.text();
  if (text.length > limit) throw new HttpError(413, "too_large", "The request is too large.");
  return text;
}

// ---- status and preferences ---------------------------------------------------

async function status(env, device) {
  const r = await one(env, "SELECT * FROM recipients WHERE device_id = ?", device.device_id);
  const templates = {};
  for (const t of await all(env, "SELECT name, status FROM templates")) templates[t.name] = t.status;
  const open = await one(env, "SELECT COUNT(*) AS n FROM approvals WHERE device_id = ? AND state IN ('open', 'deferred')", device.device_id);
  return {
    version: VERSION,
    device: device.device_id,
    pairing: r ? { state: r.state, tail: tail(r), at: r.updated_at } : { state: "none" },
    pin_set: Boolean(device.pin_hash),
    accounts: await meta.accountsFor(env, device.device_id),
    templates,
    whatsapp: { configured: Boolean(env.WHATSAPP_TOKEN && env.WHATSAPP_PHONE_NUMBER_ID) },
    logins: { facebook: Boolean(env.META_APP_ID && env.META_APP_SECRET) },
    open_approvals: open ? open.n : 0,
    muse: await muse.keyStatus(env, device.device_id),
  };
}

function tail(recipient) {
  return recipient.wa_id ? recipient.wa_id.slice(-4) : "";
}

const PREF_KEYS = ["notify_on_finish", "notify_on_fail", "notify_on_publish", "remote_approvals"];

async function preferences(env, device, body) {
  const prefs = {};
  for (const k of PREF_KEYS) prefs[k] = Boolean(body[k]);
  // Off means offers are kept for Muse and nothing goes to WhatsApp. A PC
  // from before this setting never sends it, and only ever sent anything
  // with WhatsApp on.
  prefs.whatsapp_enabled = body.whatsapp_enabled === undefined ? true : Boolean(body.whatsapp_enabled);
  prefs.preferred = (Array.isArray(body.preferred) ? body.preferred : []).filter((p) => PLATFORMS.includes(p));
  prefs.remind_minutes = Math.max(15, Math.min(1440, Number(body.remind_minutes) || 120));
  await run(env, "UPDATE devices SET prefs = ? WHERE device_id = ?", JSON.stringify(prefs), device.device_id);
  return { prefs };
}

// ---- pairing a WhatsApp number --------------------------------------------------

// 31 letters don't divide 256, so `byte % 31` would favour the first few.
// Take five bits at a time instead and skip the one value with no letter.
function randomCode(length) {
  let out = "";
  while (out.length < length) {
    for (const b of crypto.getRandomValues(new Uint8Array(length))) {
      const i = b & 31;
      if (i < CODE_ALPHABET.length && out.length < length) out += CODE_ALPHABET[i];
    }
  }
  return out;
}

async function pairStart(env, device) {
  let code = "";
  for (let i = 0; i < 5 && !code; i++) {
    const candidate = randomCode(6);
    const clash = await one(env, "SELECT device_id FROM recipients WHERE code_hash = ? AND code_expires > ?", await sha256Hex(candidate), now());
    if (!clash) code = candidate;
  }
  if (!code) throw new HttpError(503, "busy", "Couldn't make a pairing code. Try again.");
  // Starting again replaces any earlier number: the new one must be confirmed.
  await run(env, `INSERT INTO recipients (device_id, state, code_hash, code_expires, created_at, updated_at) VALUES (?, 'pending', ?, ?, ?, ?)
    ON CONFLICT(device_id) DO UPDATE SET wa_id = '', user_id = '', state = 'pending', code_hash = excluded.code_hash,
    code_expires = excluded.code_expires, updated_at = excluded.updated_at`,
  device.device_id, await sha256Hex(code), now() + PAIR_TTL, now(), now());
  await audit(env, { device: device.device_id, actor: "pc", action: "pairing_started" });
  const number = String(env.WHATSAPP_DISPLAY_NUMBER || "").replace(/\D/g, "");
  return {
    code,
    text: `LINK ${code}`,
    link: number ? `https://wa.me/${number}?text=${encodeURIComponent(`LINK ${code}`)}` : "",
    expires_at: now() + PAIR_TTL,
  };
}

async function pairConfirm(env, device, body) {
  const r = await one(env, "SELECT * FROM recipients WHERE device_id = ?", device.device_id);
  if (!r || r.state !== "awaiting_confirm") throw new HttpError(409, "nothing_to_confirm", "No number is waiting to be confirmed. Send the pairing code from your phone first.");
  if (!body.confirm) {
    await run(env, "DELETE FROM recipients WHERE device_id = ?", device.device_id);
    await audit(env, { device: device.device_id, actor: "pc", action: "pairing_refused" });
    return { pairing: { state: "none" } };
  }
  await run(env, "UPDATE recipients SET state = 'confirmed', updated_at = ? WHERE device_id = ?", now(), device.device_id);
  await audit(env, { device: device.device_id, actor: "pc", action: "pairing_confirmed", detail: `••${tail(r)}` });
  try {
    await wa.sendText(env, r, "WhatsApp notifications from Clips Kitty are on. You'll get a message when a video's highlights are ready. Reply STOP at any time to turn them off.");
  } catch (e) {
    console.error("welcome not sent", e.code);
  }
  return { pairing: { state: "confirmed", tail: tail(r) } };
}

async function setPin(env, device, body) {
  const pin = String(body.pin || "");
  if (!/^\d{4,8}$/.test(pin)) throw new HttpError(400, "bad_request", "The PIN must be 4 to 8 digits.");
  if (/^(\d)\1+$/.test(pin) || "0123456789".includes(pin) || "9876543210".includes(pin)) {
    throw new HttpError(400, "bad_request", "Choose a PIN that isn't one digit repeated or a run like 1234.");
  }
  const salt = b64url(crypto.getRandomValues(new Uint8Array(16)));
  await run(env, "UPDATE devices SET pin_hash = ?, pin_salt = ?, pin_fails = 0, pin_locked_until = 0 WHERE device_id = ?",
    await pinHash(pin, salt), salt, device.device_id);
  // A new PIN from the PC is what lifts a block on approving from the phone.
  await run(env, "DELETE FROM pin_lockouts WHERE device_id = ?", device.device_id);
  await audit(env, { device: device.device_id, actor: "pc", action: "pin_set" });
  return { pin_set: true };
}

async function unpair(env, device) {
  await run(env, "DELETE FROM recipients WHERE device_id = ?", device.device_id);
  await run(env, "UPDATE approvals SET state = 'cancelled', updated_at = ? WHERE device_id = ? AND state IN ('open', 'deferred')", now(), device.device_id);
  await muse.revokeKeys(env, device.device_id);
  await audit(env, { device: device.device_id, actor: "pc", action: "whatsapp_disconnected" });
  return { pairing: { state: "none" } };
}

// ---- the creator's Muse agent ---------------------------------------------------

async function museToken(env, device) {
  // The key goes back once, in this answer, for Settings to show. Only its
  // hash is kept (lib/muse.js), and any older key stops working.
  const key = await muse.mintKey(env, device.device_id);
  await audit(env, { device: device.device_id, actor: "pc", action: "muse_key_made" });
  return { key, muse: await muse.keyStatus(env, device.device_id) };
}

async function museRevoke(env, device) {
  const revoked = await muse.revokeKeys(env, device.device_id);
  if (revoked) await audit(env, { device: device.device_id, actor: "pc", action: "muse_key_revoked" });
  return { revoked: revoked > 0, muse: await muse.keyStatus(env, device.device_id) };
}

async function recipientFor(env, device) {
  const r = await one(env, "SELECT * FROM recipients WHERE device_id = ?", device.device_id);
  if (!r || r.state === "pending") throw new HttpError(409, "not_paired", "No WhatsApp number is paired. Pair one in Settings.");
  if (r.state === "awaiting_confirm") throw new HttpError(409, "not_confirmed", "Confirm your WhatsApp number in Settings first.");
  if (r.state === "opted_out") throw new HttpError(409, "opted_out", "This number replied STOP. Messages resume if it sends START.");
  return r;
}

// ---- notifications from the PC ----------------------------------------------------

async function notify(env, device, body) {
  const key = String(body.event_key || "");
  if (!key || key.length > 120) throw new HttpError(400, "bad_request", "Each notification needs an event key.");
  const seen = await one(env, "SELECT response FROM events WHERE device_id = ? AND event_key = ?", device.device_id, key);
  if (seen) return { ...parse(seen.response, {}), duplicate: true };
  const prefs = prefsOf(device);
  const kind = String(body.kind || "");
  let out;
  if (kind === "ready") out = await notifyReady(env, device, prefs, body);
  else if (kind === "failed") out = prefs.notify_on_fail ? await notifyFailed(env, device) : { channel: "", skipped: true };
  else if (kind === "result") out = await notifyResult(env, device, prefs, body);
  else if (kind === "test") out = await notifyTest(env, device);
  else throw new HttpError(400, "bad_request", "Unknown notification kind.");
  await run(env, "INSERT OR IGNORE INTO events (device_id, event_key, response, at) VALUES (?, ?, ?, ?)", device.device_id, key, JSON.stringify(out), now());
  return out;
}

function cleanClips(list) {
  if (!Array.isArray(list) || !list.length || list.length > 25) throw new HttpError(400, "bad_request", "An offer needs 1 to 25 clips.");
  return list.map((c) => {
    const id = Number(c.clip_id);
    if (!Number.isInteger(id) || id <= 0) throw new HttpError(400, "bad_request", "A clip id is malformed.");
    const texts = c.texts || {};
    return {
      clip_id: id,
      title: String(c.title || "").slice(0, 100),
      label: String(c.label || "").slice(0, 60),
      category: String(c.category || "").slice(0, 30),
      duration: Math.round(Number(c.duration) || 0),
      // Where the PC says this clip is already posted: shown on the page,
      // and the PC does not post it there again.
      published: PLATFORMS.filter((p) => Array.isArray(c.published) && c.published.includes(p)),
      text_source: c.text_source === "muse_spark" ? "muse_spark" : "clip",
      texts: {
        instagram: String(texts.instagram || "").slice(0, 2200),
        facebook: String(texts.facebook || "").slice(0, 2000),
      },
    };
  });
}

/**
 * Who hears about a new offer: the paired WhatsApp number, when the PC has
 * WhatsApp messages on. Otherwise null, when a Muse key is active: the offer
 * is only kept, for the creator's Muse agent to read. Neither: the reason.
 */
async function readyRecipient(env, device, prefs) {
  let why = new HttpError(409, "no_channel", "WhatsApp messages are off on your PC and Muse is not connected, so nothing can tell you about this offer.");
  if (prefs.whatsapp_enabled) {
    try {
      return await recipientFor(env, device);
    } catch (e) {
      if (!(e instanceof HttpError)) throw e;
      why = e;
    }
  }
  if (await muse.activeKey(env, device.device_id)) return null;
  throw why;
}

async function notifyReady(env, device, prefs, body) {
  const recipient = await readyRecipient(env, device, prefs);
  const clips = cleanClips(body.clips);
  const ids = new Set(clips.map((c) => c.clip_id));
  if (ids.size !== clips.length) throw new HttpError(400, "bad_request", "A clip appears twice.");
  const wanted = Date.parse(String(body.expires_at || "")) / 1000;
  const expires = Math.min(Number.isFinite(wanted) ? Math.floor(wanted) : 0, now() + MAX_OFFER_HOURS * 3600);
  if (expires <= now() + 60) throw new HttpError(400, "bad_request", "This offer has already expired.");
  const reviewRef = Number(body.review_ref);
  if (!Number.isInteger(reviewRef) || reviewRef <= 0) throw new HttpError(400, "bad_request", "The offer has no reference.");
  const payload = {
    title: String(body.title || "your video").slice(0, 80),
    summary_text: String(body.summary_text || "").slice(0, 300),
    kind: ["basketball", "gaming", "video"].includes(body.video_kind) ? body.video_kind : "",
    muse_line: String(body.muse_line || "").slice(0, 160),
    preferred: (Array.isArray(body.preferred) ? body.preferred : []).filter((p) => PLATFORMS.includes(p)),
    remind_minutes: Math.max(15, Math.min(1440, Number(body.remind_minutes) || prefs.remind_minutes)),
    // The PC sends each clip's preview video next (uploadPreview), but only
    // when the answer says this relay keeps them (KV). Either side from
    // before previews sends or reads neither, and the page shows thumbnails.
    previews: Boolean(body.previews) && Boolean(env.PREVIEWS),
    clips,
  };
  const id = randomId("apr");
  const token = randomToken(32);
  await run(env, `INSERT INTO approvals (id, device_id, review_ref, token_hash, token_enc, fingerprint, state, payload, remote_ok, expires_at, created_at, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?)`,
  id, device.device_id, reviewRef, await sha256Hex(token), await seal(env, token), String(body.fingerprint || "").slice(0, 64),
  JSON.stringify(payload), body.remote_approvals && prefs.remote_approvals ? 1 : 0, expires, now(), now());
  for (const c of Array.isArray(body.clips) ? body.clips : []) {
    const jpeg = String(c.thumb || "");
    if (jpeg && jpeg.length < 82000 && /^[A-Za-z0-9+/=]+$/.test(jpeg) && ids.has(Number(c.clip_id))) {
      await run(env, "INSERT OR REPLACE INTO thumbs (approval_id, clip_id, jpeg) VALUES (?, ?, ?)", id, Number(c.clip_id), jpeg);
    }
  }
  if (!recipient) {
    // Kept for Muse, and nothing sent. Marked sent all the same, so opening
    // the page from the link Muse gives tells the PC it was seen.
    await run(env, "UPDATE approvals SET delivery = 'sent' WHERE id = ?", id);
    await audit(env, { device: device.device_id, approval: id, actor: "relay", action: "offer_kept", detail: `${clips.length} clips for Muse` });
    return { approval_id: id, channel: "muse", message_id: "", expires_at: new Date(expires * 1000).toISOString(), previews: Boolean(env.PREVIEWS) };
  }
  let sent;
  try {
    sent = await sendReady(env, recipient, { id, payload }, token);
  } catch (e) {
    await run(env, "DELETE FROM approvals WHERE id = ?", id);
    await run(env, "DELETE FROM thumbs WHERE approval_id = ?", id);
    throw waHttp(e);
  }
  await run(env, "UPDATE approvals SET message_id = ?, delivery = 'sent' WHERE id = ?", sent.wamid, id);
  await audit(env, { device: device.device_id, approval: id, actor: "relay", action: "offer_sent", detail: `${clips.length} clips via ${sent.channel}` });
  return { approval_id: id, channel: sent.channel, message_id: sent.wamid, expires_at: new Date(expires * 1000).toISOString(), previews: Boolean(env.PREVIEWS) };
}

async function sendReady(env, recipient, approval, token) {
  const { payload } = approval;
  const url = `${env.PUBLIC_URL}/a/${token}`;
  try {
    const wamid = await wa.sendTemplate(env, recipient, "ck_highlights_ready", [payload.title, payload.summary_text], [
      { type: "button", sub_type: "url", index: "0", parameters: [{ type: "text", text: token }] },
      { type: "button", sub_type: "quick_reply", index: "1", parameters: [{ type: "payload", payload: `REMIND:${approval.id}` }] },
      { type: "button", sub_type: "quick_reply", index: "2", parameters: [{ type: "payload", payload: `CANCEL:${approval.id}` }] },
    ]);
    await wa.rememberMessage(env, wamid, recipient.device_id, approval.id, "ready");
    return { wamid, channel: "template" };
  } catch (e) {
    if (!fallbackAllowed(e, recipient)) throw e;
  }
  // The template is not usable yet (waiting for review), but the creator
  // wrote to us within a day, so a plain interactive link is allowed.
  const wamid = await wa.sendLink(env, recipient, {
    header: "Highlights ready",
    body: `Clips Kitty finished your video ${wa.clean(payload.title)}. Ready to review: ${wa.clean(payload.summary_text)}. Choose where to publish them before the link expires.`,
    label: "Review clips",
    url,
    footer: "Reply STOP to turn these off",
  });
  await wa.rememberMessage(env, wamid, recipient.device_id, approval.id, "ready");
  return { wamid, channel: "interactive" };
}

function fallbackAllowed(e, recipient) {
  return e instanceof wa.WaError && ["template_missing", "template_not_approved"].includes(e.code) && wa.windowOpen(recipient);
}

async function notifyFailed(env, device) {
  const recipient = await recipientFor(env, device);
  const text = "Clips Kitty could not finish processing your video. Open the app for details.";
  return sendSimple(env, recipient, "ck_processing_failed", [], text, "failed");
}

async function notifyTest(env, device) {
  const recipient = await recipientFor(env, device);
  const text = "This is a test message from Clips Kitty. Your WhatsApp notifications are set up correctly.";
  return sendSimple(env, recipient, "ck_test_message", [], text, "test");
}

async function sendSimple(env, recipient, template, params, text, kind) {
  try {
    const wamid = await wa.sendTemplate(env, recipient, template, params);
    await wa.rememberMessage(env, wamid, recipient.device_id, "", kind);
    return { channel: "template", message_id: wamid };
  } catch (e) {
    if (!fallbackAllowed(e, recipient)) throw waHttp(e);
  }
  try {
    const wamid = await wa.sendText(env, recipient, text);
    await wa.rememberMessage(env, wamid, recipient.device_id, "", kind);
    return { channel: "text", message_id: wamid };
  } catch (e) {
    throw waHttp(e);
  }
}

function cleanResults(results) {
  const clips = (Array.isArray(results && results.clips) ? results.clips : []).slice(0, 25).map((c) => ({
    clip_id: Number(c.clip_id) || 0,
    title: String(c.title || "").slice(0, 100),
    posts: (Array.isArray(c.posts) ? c.posts : []).filter((p) => PLATFORMS.includes(p.platform)).map((p) => {
      const state = ["published", "failed", "unconfirmed", "skipped"].includes(p.state) ? p.state : "failed";
      return { platform: p.platform, state, url: state === "published" ? meta.safeLink(p.url) : "", error: String(p.error || "").slice(0, 200) };
    }),
  }));
  const totals = { published: 0, failed: 0, unconfirmed: 0, skipped: 0 };
  for (const c of clips) for (const p of c.posts) totals[p.state] += 1;
  return { clips, totals };
}

async function notifyResult(env, device, prefs, body) {
  const approval = await one(env, "SELECT * FROM approvals WHERE id = ? AND device_id = ?", String(body.approval_id || ""), device.device_id);
  if (!approval) throw new HttpError(404, "bad_request", "That offer is not on this relay.");
  if (!["approved", "done"].includes(approval.state)) throw new HttpError(409, "bad_request", `That offer is ${approval.state}.`);
  const results = cleanResults(body.results);
  await run(env, "UPDATE approvals SET state = 'done', results = ?, updated_at = ? WHERE id = ?", JSON.stringify(results), now(), approval.id);
  await audit(env, { device: device.device_id, approval: approval.id, actor: "pc", action: "results", detail: JSON.stringify(results.totals) });
  // Kept either way, for the status page and Muse. A message only when the
  // PC wants one and a confirmed number can take it.
  if (!prefs.notify_on_publish || !prefs.whatsapp_enabled) return { channel: "", skipped: true };
  const recipient = await one(env, "SELECT * FROM recipients WHERE device_id = ? AND state = 'confirmed'", device.device_id);
  if (!recipient) return { channel: "", skipped: true };
  const payload = parse(approval.payload, {});
  const token = await unseal(env, approval.token_enc);
  const line = String(body.text || "").slice(0, 200) || `${results.totals.published} posts published`;
  try {
    const wamid = await wa.sendTemplate(env, recipient, "ck_publish_update", [payload.title, line], [
      { type: "button", sub_type: "url", index: "0", parameters: [{ type: "text", text: token }] },
    ]);
    await wa.rememberMessage(env, wamid, device.device_id, approval.id, "result");
    return { channel: "template", message_id: wamid };
  } catch (e) {
    if (!fallbackAllowed(e, recipient)) throw waHttp(e);
  }
  const wamid = await wa.sendLink(env, recipient, {
    header: "Publishing update",
    body: `Publishing update for ${wa.clean(payload.title)}: ${wa.clean(line)}. Open the status page for the confirmed links.`,
    label: "View status",
    url: `${env.PUBLIC_URL}/a/${token}`,
  });
  await wa.rememberMessage(env, wamid, device.device_id, approval.id, "result");
  return { channel: "interactive", message_id: wamid };
}

function waHttp(e) {
  if (e instanceof HttpError) return e;
  if (e instanceof wa.WaError) {
    const retry = ["rate_limited"].includes(e.code);
    return new HttpError(retry ? 503 : 409, e.code, e.message);
  }
  return e;
}

async function safeTemplates(env, create) {
  if (!env.WHATSAPP_TOKEN || !env.WHATSAPP_WABA_ID) throw new HttpError(409, "not_configured", "WhatsApp is not configured on the relay.");
  try {
    return create ? await wa.createTemplates(env) : await wa.refreshTemplates(env);
  } catch (e) {
    throw waHttp(e);
  }
}

// ---- commands for the PC -----------------------------------------------------------

async function pendingCommands(env, device) {
  const rows = await all(env, "SELECT * FROM commands WHERE device_id = ? AND state = 'pending' ORDER BY created_at, id LIMIT 20", device.device_id);
  const commands = [];
  for (const row of rows) {
    const command = { ...parse(row.body, {}), id: row.id, kind: row.kind };
    command.sig = await hmacHex(device.secret, canonical(command));
    commands.push(command);
  }
  return { commands };
}

async function ack(env, device, id, body) {
  const row = await one(env, "SELECT * FROM commands WHERE id = ? AND device_id = ?", id, device.device_id);
  if (!row) throw new HttpError(404, "not_found", "No such command.");
  const outcome = ["accepted", "rejected", "ignored"].includes(body.outcome) ? body.outcome : "rejected";
  const reason = String(body.reason || "").slice(0, 300);
  if (row.state === "pending") {
    await run(env, "UPDATE commands SET state = 'acked', outcome = ?, reason = ?, acked_at = ? WHERE id = ?", outcome, reason, now(), id);
    if (row.kind === "approve") {
      const answer = JSON.stringify({ outcome, reason, at: now() });
      if (outcome === "accepted") {
        await run(env, "UPDATE approvals SET pc_answer = ?, updated_at = ? WHERE id = ?", answer, now(), row.approval_id);
      } else {
        await run(env, "UPDATE approvals SET pc_answer = ?, state = 'rejected', updated_at = ? WHERE id = ? AND state = 'approved'", answer, now(), row.approval_id);
      }
    }
    await audit(env, { device: device.device_id, approval: row.approval_id, actor: "pc", action: `${row.kind}_${outcome}`, detail: reason });
  }
  return { ok: true };
}

async function cancelFromPc(env, device, id) {
  const changed = await run(env, "UPDATE approvals SET state = 'cancelled', updated_at = ? WHERE id = ? AND device_id = ? AND state IN ('open', 'deferred')",
    now(), id, device.device_id);
  if (changed) await audit(env, { device: device.device_id, approval: id, actor: "pc", action: "cancelled" });
  return { cancelled: Boolean(changed) };
}

// ---- the approval page -----------------------------------------------------------------

async function offerFor(env, token) {
  const approval = await one(env, "SELECT * FROM approvals WHERE token_hash = ?", await sha256Hex(token));
  if (!approval || now() > approval.created_at + STATUS_TTL) return null;
  approval.decision = parse(approval.decision, {});
  approval.pc_answer = parse(approval.pc_answer, {});
  approval.results = parse(approval.results, {});
  return approval;
}

async function csrfFor(env, approval) {
  return hmacHex(env.TOKEN_KEY, `csrf:${approval.id}:${approval.token_hash}`);
}

async function renderOffer(env, approval, token, error = "", statusCode = 200, ticked = null) {
  const payload = parse(approval.payload, {});
  const thumbs = new Set((await all(env, "SELECT clip_id FROM thumbs WHERE approval_id = ?", approval.id)).map((r) => r.clip_id));
  // The clips with a preview video, only while KV is bound (preview() serves
  // none without it). A D1 from before previews, schema.sql not run again,
  // has no such table: the page still shows, with thumbnails.
  let previews = new Set();
  if (env.PREVIEWS) {
    try {
      previews = new Set((await all(env, "SELECT clip_id FROM previews WHERE approval_id = ?", approval.id)).map((r) => r.clip_id));
    } catch (e) {
      console.error("previews not read", e && e.name);
    }
  }
  return html(page.approvalPage({
    approval, payload, token, csrf: await csrfFor(env, approval), accounts: await meta.accountsFor(env, approval.device_id),
    thumbs, previews, nowS: now(), error, blocked: await pinBlocked(env, approval.device_id), later: await laterChannel(env, approval, payload), ticked,
  }), statusCode);
}

/**
 * What can bring an offer back after Remind me later, for the page to say:
 * "whatsapp" when cron will send the link again and the send can work then,
 * "muse" when only the creator's Muse agent can, once asked (nothing here
 * can message it, lib/muse.js), else "".
 */
async function laterChannel(env, approval, payload) {
  const device = await one(env, "SELECT device_id, prefs FROM devices WHERE device_id = ?", approval.device_id);
  if (device && (await muse.whatsappReminds(env, device))) {
    // The same check cron makes, then what the send itself needs (sendReady):
    // the approved template, or else a plain link, which reaches the creator
    // only within a day of their last message. Until Meta approves the
    // template, a reminder after that would fail, so it isn't promised.
    const at = approval.state === "deferred" ? approval.remind_at : now() + Number(payload.remind_minutes || prefsOf(device).remind_minutes) * 60;
    const recipient = await one(env, "SELECT last_inbound FROM recipients WHERE device_id = ? AND state = 'confirmed'", approval.device_id);
    if ((await wa.templateStatus(env, "ck_highlights_ready")) === "APPROVED" || (recipient && wa.windowOpen(recipient, at))) return "whatsapp";
  }
  return (await muse.activeKey(env, approval.device_id)) ? "muse" : "";
}

/**
 * Once five tries are used up: the quarter-hour lock, noted for pinBlocked
 * (lib/db.js). Only one try can start it, since the same write puts the
 * count back to zero. The page to show when this lock is the one that
 * blocks approving from the phone, else null.
 */
async function lockPin(env, device, approval, token, ticked = null) {
  const started = await run(env, "UPDATE devices SET pin_fails = 0, pin_locked_until = ? WHERE device_id = ? AND pin_fails >= ?",
    now() + PIN_LOCK, device.device_id, PIN_MAX_FAILS);
  if (!started) return null;
  await run(env, "INSERT INTO pin_lockouts (device_id, at) VALUES (?, ?)", device.device_id, now());
  if (!(await pinBlocked(env, device.device_id))) return null;
  await audit(env, { device: device.device_id, approval: approval.id, actor: "relay", action: "pin_blocked" });
  return renderOffer(env, approval, token, page.PIN_BLOCKED, 403, ticked);
}

function pinLocked(env, approval, token, until, ticked = null) {
  const mins = Math.max(1, Math.ceil((until - now()) / 60));
  return renderOffer(env, approval, token, `Too many wrong PINs. Try again in ${mins} minute${mins === 1 ? "" : "s"}.`, 429, ticked);
}

async function showOffer(env, token) {
  const approval = await offerFor(env, token);
  if (!approval) return html(page.message("Link not valid", "This link is not valid or is too old. Open Clips Kitty on your PC."), 404);
  if (approval.delivery === "sent" && ["open", "deferred"].includes(approval.state)) {
    // Opening the page is proof the message arrived, even before WhatsApp says so.
    const changed = await run(env, "UPDATE approvals SET delivery = 'opened' WHERE id = ? AND delivery = 'sent'", approval.id);
    if (changed) await addCommand(env, approval.device_id, approval.id, "delivery", { approval_id: approval.id, review_ref: approval.review_ref, status: "opened" });
  }
  return renderOffer(env, approval, token);
}

async function thumb(env, token, clipId) {
  const approval = await offerFor(env, token);
  if (!approval) return new Response("", { status: 404 });
  const row = await one(env, "SELECT jpeg FROM thumbs WHERE approval_id = ? AND clip_id = ?", approval.id, clipId);
  if (!row) return new Response("", { status: 404 });
  const bin = atob(row.jpeg);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new Response(bytes, { headers: { "Content-Type": "image/jpeg", "Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff" } });
}

const PREVIEW_HEADERS = { "Content-Type": "video/mp4", "Accept-Ranges": "bytes", "Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff" };

/**
 * A clip's preview video, for the page's player: only while the offer can
 * still be decided, and in byte ranges, since an iPhone plays video only
 * from a server that answers its "bytes=0-1" probe with a 206. A range is a
 * view of the bytes KV returned, never a copy.
 */
async function preview(request, env, token, clipId) {
  const missing = () => new Response("", { status: 404, headers: { "Cache-Control": "no-store" } });
  if (!env.PREVIEWS) return missing();
  const approval = await offerFor(env, token);
  if (!approval || !["open", "deferred"].includes(approval.state) || approval.expires_at <= now()) return missing();
  const buf = await env.PREVIEWS.get(`preview:${approval.id}:${clipId}`, "arrayBuffer");
  if (!buf) return missing();
  const size = buf.byteLength;
  const range = byteRange(request.headers.get("Range"), size);
  if (range.status === 416) return new Response(null, { status: 416, headers: { ...PREVIEW_HEADERS, "Content-Range": `bytes */${size}` } });
  if (range.status === 206) {
    return new Response(new Uint8Array(buf, range.start, range.end - range.start + 1), {
      status: 206, headers: { ...PREVIEW_HEADERS, "Content-Range": `bytes ${range.start}-${range.end}/${size}` },
    });
  }
  return new Response(buf, { headers: PREVIEW_HEADERS });
}

/**
 * One Range header against a file of `size` bytes: {status: 206, start, end}
 * for a single range ("bytes=a-b", "bytes=a-" or "bytes=-n", the end
 * clamped to the last byte), {status: 416} when it starts at or past the end
 * or asks for no bytes ("bytes=-0"), and {status: 200}, the whole file, for
 * no header or one this doesn't take: malformed, several ranges, another
 * unit, or b < a.
 */
export function byteRange(header, size) {
  const m = /^bytes=(\d*)-(\d*)$/i.exec(String(header || ""));
  if (!m || (!m[1] && !m[2]) || (m[1] && m[2] && Number(m[2]) < Number(m[1]))) return { status: 200 };
  const start = m[1] ? Number(m[1]) : Math.max(0, size - Number(m[2]));
  if (start >= size) return { status: 416 };
  return { status: 206, start, end: m[1] && m[2] ? Math.min(Number(m[2]), size - 1) : size - 1 };
}

async function decide(request, env, token) {
  const approval = await offerFor(env, token);
  if (!approval) return html(page.message("Link not valid", "This link is not valid or is too old."), 404);
  const origin = request.headers.get("Origin");
  // Origin: null passes only when the browser marks the post Sec-Fetch-Site:
  // same-origin (sent by a document on the origin it was posted to, not just
  // the same site), a header no page can set itself.
  const ownPage = origin === "null" && request.headers.get("Sec-Fetch-Site") === "same-origin";
  if (origin && !ownPage && env.PUBLIC_URL && origin !== new URL(env.PUBLIC_URL).origin) {
    return html(page.message("Not allowed", "That request did not come from this page."), 403);
  }
  const form = await request.formData();
  if (!(await hmacVerify(env.TOKEN_KEY, `csrf:${approval.id}:${approval.token_hash}`, String(form.get("csrf") || "")))) {
    return html(page.message("Not allowed", "This page is out of date. Open the link again."), 403);
  }
  if (!["open", "deferred"].includes(approval.state) || approval.expires_at <= now()) return renderOffer(env, approval, token);
  const device = await one(env, "SELECT * FROM devices WHERE device_id = ?", approval.device_id);
  const payload = parse(approval.payload, {});
  const action = String(form.get("action") || "");
  // Every page this post is answered with shows the ticks as they were sent.
  // Put back to the defaults, a clip taken off before a wrong PIN would come
  // back ticked, and the next try would post it.
  const ticked = {
    clips: new Set(form.getAll("clip").map(Number)),
    platforms: new Set(form.getAll("platform").map(String)),
    confirm: form.get("confirm") === "yes",
  };
  const refuse = (message, statusCode) => renderOffer(env, approval, token, message, statusCode, ticked);

  if (action === "defer") {
    const at = now() + Number(payload.remind_minutes || prefsOf(device).remind_minutes) * 60;
    if (at >= approval.expires_at - 300) return refuse("This link expires too soon for Remind me later. Decide now, or ask for a new offer from the app later.", 400);
    const changed = await run(env, "UPDATE approvals SET state = 'deferred', remind_at = ?, updated_at = ? WHERE id = ? AND state IN ('open', 'deferred')", at, now(), approval.id);
    if (changed) {
      await addCommand(env, approval.device_id, approval.id, "defer", { approval_id: approval.id, review_ref: approval.review_ref, remind_at: iso(at) });
      await audit(env, { device: approval.device_id, approval: approval.id, actor: "phone", action: "deferred" });
    }
    return redirect(`/a/${token}`);
  }
  if (action === "cancel") {
    const changed = await run(env, "UPDATE approvals SET state = 'cancelled', updated_at = ? WHERE id = ? AND state IN ('open', 'deferred')", now(), approval.id);
    if (changed) {
      await addCommand(env, approval.device_id, approval.id, "cancel", { approval_id: approval.id, review_ref: approval.review_ref, decided_at: iso(now()) });
      await audit(env, { device: approval.device_id, approval: approval.id, actor: "phone", action: "cancelled" });
    }
    return redirect(`/a/${token}`);
  }
  if (action !== "approve") return refuse("Choose Approve, Remind me later or Cancel.", 400);

  if (!approval.remote_ok) return refuse("Approving from your phone is switched off on your PC.", 400);
  if (!device || !device.pin_hash) return refuse("Set an approval PIN in Clips Kitty first.", 400);
  if (await pinBlocked(env, device.device_id)) return refuse(page.PIN_BLOCKED, 403);
  if (device.pin_locked_until > now()) return pinLocked(env, approval, token, device.pin_locked_until, ticked);
  // A try is counted before its PIN is checked, in one write that also
  // checks the lock. Counted after the check, tries sent all at once each
  // read the count before any of them wrote it, so all of them were checked
  // and the lock never started. This way each takes its own place in the
  // count, and a burst gets the same five tries as one try at a time.
  const counted = await run(env, "UPDATE devices SET pin_fails = pin_fails + 1 WHERE device_id = ? AND pin_fails < ? AND pin_locked_until <= ?",
    device.device_id, PIN_MAX_FAILS, now());
  if (!counted) {
    // Five tries are already out. If none of them has started the lock yet
    // (they are still being checked, or one was cut off), this one starts
    // it, so the count can't stick at five with nothing locked.
    const blocked = await lockPin(env, device, approval, token, ticked);
    if (blocked) return blocked;
    const fresh = await one(env, "SELECT pin_locked_until FROM devices WHERE device_id = ?", device.device_id);
    return pinLocked(env, approval, token, fresh ? fresh.pin_locked_until : 0, ticked);
  }
  const pin = String(form.get("pin") || "");
  const good = /^\d{4,8}$/.test(pin) && (await safeEqual(await pinHash(pin, device.pin_salt), device.pin_hash));
  if (!good) {
    await audit(env, { device: device.device_id, approval: approval.id, actor: "phone", action: "pin_wrong" });
    return (await lockPin(env, device, approval, token, ticked)) || refuse("That PIN is not right. Nothing was approved.", 403);
  }
  await run(env, "UPDATE devices SET pin_fails = 0 WHERE device_id = ?", device.device_id);
  if (form.get("confirm") !== "yes") return refuse("Tick the box to confirm the clips should be posted publicly.", 400);

  const offered = new Set((payload.clips || []).map((c) => c.clip_id));
  const clips = [...new Set(form.getAll("clip").map(Number))];
  if (!clips.length || clips.some((c) => !offered.has(c))) return refuse("Tick at least one clip.", 400);
  const accounts = await meta.accountsFor(env, approval.device_id);
  const platforms = PLATFORMS.filter((p) => form.getAll("platform").includes(p));
  if (!platforms.length) return refuse("Tick at least one place to publish.", 400);
  // Every place ticked must be a connected account. One that isn't refuses
  // the lot, as a clip that wasn't offered does: nothing ticked is dropped quietly.
  if (form.getAll("platform").some((p) => !platforms.includes(p) || !accounts[p].connected)) {
    return refuse("One of those accounts is not connected anymore.", 400);
  }

  const decision = { clips, platforms, decided_at: now() };
  const changed = await run(env, "UPDATE approvals SET state = 'approved', decision = ?, updated_at = ? WHERE id = ? AND state IN ('open', 'deferred') AND expires_at > ?",
    JSON.stringify(decision), now(), approval.id, now());
  if (!changed) return renderOffer(env, await offerFor(env, token), token);
  await addCommand(env, approval.device_id, approval.id, "approve", {
    approval_id: approval.id, review_ref: approval.review_ref, fingerprint: approval.fingerprint,
    clips, platforms, decided_at: iso(decision.decided_at),
  });
  await audit(env, { device: approval.device_id, approval: approval.id, actor: "phone", action: "approved", detail: `${clips.length} clips to ${platforms.join(", ")}` });
  return redirect(`/a/${token}`);
}

// ---- WhatsApp webhook ------------------------------------------------------------------

function verifyWebhook(url, env) {
  const mode = url.searchParams.get("hub.mode");
  const token = url.searchParams.get("hub.verify_token") || "";
  const challenge = url.searchParams.get("hub.challenge") || "";
  if (mode === "subscribe" && env.WEBHOOK_VERIFY_TOKEN && token === env.WEBHOOK_VERIFY_TOKEN && /^[\w-]{1,100}$/.test(challenge)) {
    return new Response(challenge, { status: 200, headers: { "Content-Type": "text/plain" } });
  }
  return new Response("forbidden", { status: 403 });
}

async function webhook(request, env, ctx) {
  const raw = await request.text();
  const sig = (request.headers.get("X-Hub-Signature-256") || "").replace(/^sha256=/, "");
  if (!env.META_APP_SECRET || !(await hmacVerify(env.META_APP_SECRET, raw, sig))) {
    return new Response("bad signature", { status: 401 });
  }
  const body = parse(raw, {});
  // Answer at once (Meta wants well under a second); handle the batch after.
  ctx.waitUntil(handleWebhook(env, body).catch((e) => console.error("webhook handling failed", e && e.name)));
  return new Response("ok", { status: 200 });
}

export async function handleWebhook(env, body) {
  for (const entry of body.entry || []) {
    for (const change of entry.changes || []) {
      const value = change.value || {};
      if (change.field === "message_template_status_update") {
        if (wa.TEMPLATE_NAMES.includes(value.message_template_name)) {
          await run(env, "INSERT INTO templates (name, status, updated_at) VALUES (?, ?, ?) ON CONFLICT(name) DO UPDATE SET status = excluded.status, updated_at = excluded.updated_at",
            value.message_template_name, String(value.event || ""), now());
        }
        continue;
      }
      if (change.field !== "messages") continue;
      for (const msg of value.messages || []) {
        if (!(await firstTime(env, String(msg.id || "")))) continue;
        await inbound(env, msg);
      }
      for (const st of value.statuses || []) {
        if (!(await firstTime(env, `${st.id}:${st.status}`))) continue;
        await delivery(env, st);
      }
    }
  }
}

async function firstTime(env, id) {
  if (!id) return false;
  return (await run(env, "INSERT OR IGNORE INTO wa_seen (id, at) VALUES (?, ?)", id, now())) === 1;
}

async function inbound(env, msg) {
  const from = String(msg.from || "");
  const userId = String(msg.from_user_id || "");
  const text = msg.type === "text" ? String((msg.text && msg.text.body) || "").trim() : "";
  const link = text.match(/^link\s+([A-Za-z0-9]{6})$/i);
  if (link) return pairFromPhone(env, from, userId, link[1].toUpperCase());

  // One number can be paired with more than one PC (a desktop and a laptop,
  // or a reinstall). The 24-hour window and STOP belong to the number; a
  // button tap belongs to the offer it was sent with, and so to that PC.
  const recipients = await findRecipients(env, from, userId);
  if (!recipients.length) return; // a stranger: no reply, no cost
  for (const r of recipients) {
    await run(env, "UPDATE recipients SET last_inbound = ? WHERE device_id = ?", now(), r.device_id);
    r.last_inbound = now();
  }
  const recipient = recipients[0];

  if (msg.type === "button" && msg.button) return quickReply(env, recipients, String(msg.button.payload || ""), msg.context && msg.context.id);
  if (msg.type === "interactive" && msg.interactive && msg.interactive.button_reply) {
    return quickReply(env, recipients, String(msg.interactive.button_reply.id || ""), msg.context && msg.context.id);
  }
  if (!text) return;
  if (/^stop$/i.test(text)) {
    for (const r of recipients) {
      await run(env, "UPDATE recipients SET state = 'opted_out', updated_at = ? WHERE device_id = ?", now(), r.device_id);
      await audit(env, { device: r.device_id, actor: "phone", action: "opted_out" });
    }
    return reply(env, recipient, "You won't get Clips Kitty messages anymore. Send START to turn them back on.");
  }
  if (/^start$/i.test(text) && recipients.some((r) => r.state === "opted_out")) {
    for (const r of recipients.filter((x) => x.state === "opted_out")) {
      await run(env, "UPDATE recipients SET state = 'confirmed', updated_at = ? WHERE device_id = ?", now(), r.device_id);
      await audit(env, { device: r.device_id, actor: "phone", action: "opted_in" });
    }
    return reply(env, recipient, "Clips Kitty messages are back on.");
  }
  if (recipients.some((r) => r.state === "confirmed") && /\b(yes|yep|approve|approved|ok|okay|publish|post it|go)\b/i.test(text)) {
    // A bare "yes" never approves anything: it can't say which clips, where, or for which video.
    return reply(env, recipient, "To publish, open the Review clips link in the message about that video, choose the clips and where they go, and approve there with your PIN.");
  }
}

async function findRecipients(env, from, userId) {
  const states = "state IN ('confirmed', 'awaiting_confirm', 'opted_out')";
  if (from) {
    const rows = await all(env, `SELECT * FROM recipients WHERE wa_id = ? AND ${states} ORDER BY updated_at DESC`, from);
    if (rows.length) return rows;
  }
  if (userId) return all(env, `SELECT * FROM recipients WHERE user_id = ? AND ${states} ORDER BY updated_at DESC`, userId);
  return [];
}

async function pairFromPhone(env, from, userId, code) {
  const row = await one(env, "SELECT * FROM recipients WHERE code_hash = ? AND state = 'pending' AND code_expires > ?", await sha256Hex(code), now());
  const who = { wa_id: from, user_id: userId, last_inbound: now() };
  if (!row) return reply(env, who, "That code didn't match or has expired. Get a new one in Clips Kitty → Settings → Notifications & Remote Approval.");
  await run(env, "UPDATE recipients SET wa_id = ?, user_id = ?, state = 'awaiting_confirm', code_hash = '', code_expires = 0, last_inbound = ?, updated_at = ? WHERE device_id = ?",
    from, userId, now(), now(), row.device_id);
  await audit(env, { device: row.device_id, actor: "phone", action: "pairing_code_received", detail: from ? `••${from.slice(-4)}` : "hidden number" });
  return reply(env, who, `Almost done. On your PC, confirm that the number ending in ${from ? from.slice(-4) : "(hidden)"} is yours.`);
}

async function quickReply(env, recipients, payload, contextId) {
  const m = payload.match(/^(REMIND|CANCEL):(apr_[0-9a-f]{24})$/);
  if (!m) return;
  const approval = await one(env, "SELECT * FROM approvals WHERE id = ?", m[2]);
  // Only the number paired with the PC that made this offer can act on it.
  const recipient = approval && recipients.find((r) => r.device_id === approval.device_id);
  if (!recipient || recipient.state !== "confirmed") return;
  // The tap must come from a message we sent for this very offer.
  if (contextId) {
    const sent = await one(env, "SELECT approval_id FROM messages WHERE wamid = ?", contextId);
    if (!sent || sent.approval_id !== approval.id) return;
  }
  if (!["open", "deferred"].includes(approval.state) || approval.expires_at <= now()) {
    return reply(env, recipient, approval.state === "approved" || approval.state === "done"
      ? "That video's clips were already approved." : "That offer is closed. Open Clips Kitty to send a new one.");
  }
  if (m[1] === "CANCEL") {
    const changed = await run(env, "UPDATE approvals SET state = 'cancelled', updated_at = ? WHERE id = ? AND state IN ('open', 'deferred')", now(), approval.id);
    if (changed) {
      await addCommand(env, approval.device_id, approval.id, "cancel", { approval_id: approval.id, review_ref: approval.review_ref, decided_at: iso(now()) });
      await audit(env, { device: approval.device_id, approval: approval.id, actor: "phone", action: "cancelled", detail: "quick reply" });
    }
    return reply(env, recipient, "Cancelled. Nothing from that video will be posted.");
  }
  const payloadData = parse(approval.payload, {});
  const minutes = Number(payloadData.remind_minutes || 120);
  const at = now() + minutes * 60;
  if (at >= approval.expires_at - 300) return reply(env, recipient, "The link expires before a reminder would come. Open it to decide now.");
  const changed = await run(env, "UPDATE approvals SET state = 'deferred', remind_at = ?, updated_at = ? WHERE id = ? AND state IN ('open', 'deferred')", at, now(), approval.id);
  if (changed) {
    await addCommand(env, approval.device_id, approval.id, "defer", { approval_id: approval.id, review_ref: approval.review_ref, remind_at: iso(at) });
    await audit(env, { device: approval.device_id, approval: approval.id, actor: "phone", action: "deferred", detail: "quick reply" });
  }
  const span = minutes >= 120 ? `${Math.round(minutes / 60)} hours` : `${minutes} minutes`;
  return reply(env, recipient, `OK, I'll remind you in about ${span}.`);
}

async function reply(env, recipient, text) {
  try {
    await wa.sendText(env, recipient, text);
  } catch (e) {
    console.error("reply not sent", e.code);
  }
}

async function delivery(env, st) {
  const row = await one(env, "SELECT * FROM messages WHERE wamid = ?", String(st.id || ""));
  if (!row) return;
  await run(env, "UPDATE messages SET status = ? WHERE wamid = ?", String(st.status || ""), row.wamid);
  if (row.kind !== "ready" || !row.approval_id) return;
  const approval = await one(env, "SELECT * FROM approvals WHERE id = ?", row.approval_id);
  if (!approval) return;
  if (["delivered", "read"].includes(st.status)) {
    const changed = await run(env, "UPDATE approvals SET delivery = ? WHERE id = ? AND delivery IN ('', 'sent')", st.status, approval.id);
    if (changed) await addCommand(env, approval.device_id, approval.id, "delivery", { approval_id: approval.id, review_ref: approval.review_ref, status: st.status });
  } else if (st.status === "failed") {
    const code = Number(((st.errors || [])[0] || {}).code || 0);
    const { message } = wa.describeError(code);
    await run(env, "UPDATE approvals SET delivery = 'failed', state = CASE WHEN state IN ('open', 'deferred') THEN 'cancelled' ELSE state END WHERE id = ?", approval.id);
    await addCommand(env, approval.device_id, approval.id, "delivery", { approval_id: approval.id, review_ref: approval.review_ref, status: "failed", error: message });
    await audit(env, { device: approval.device_id, approval: approval.id, actor: "whatsapp", action: "undelivered", detail: String(code) });
  }
}

// ---- publishing steps, only inside an approval ------------------------------------------

async function approvedCommand(env, device, params, platform) {
  const command = await one(env, "SELECT * FROM commands WHERE id = ? AND device_id = ? AND kind = 'approve'", String(params.command_id || ""), device.device_id);
  if (!command || command.outcome !== "accepted") throw new HttpError(403, "not_approved", "Nothing approved covers this step. Nothing was posted.");
  const approval = await one(env, "SELECT * FROM approvals WHERE id = ? AND device_id = ?", command.approval_id, device.device_id);
  if (!approval || approval.id !== String(params.approval_id || "") || !["approved", "done"].includes(approval.state)) {
    throw new HttpError(403, "not_approved", "That approval is not active. Nothing was posted.");
  }
  const decision = parse(approval.decision, {});
  if (now() > Number(decision.decided_at || 0) + PUBLISH_WINDOW) throw new HttpError(403, "expired", "This approval is more than a day old. Approve again to publish.");
  const clipId = Number(params.clip_id);
  if (!(decision.clips || []).includes(clipId)) throw new HttpError(403, "not_approved", "That clip was not approved.");
  if (!(decision.platforms || []).includes(platform)) throw new HttpError(403, "not_approved", `${platform} was not approved for this clip.`);
  return { command, approval, device: device.device_id, clipId };
}

function metaHttp(e) {
  if (e instanceof HttpError) return e;
  if (e instanceof meta.MetaError) {
    const status = e.code === "not_approved" ? 403 : e.transient ? 503 : e.code === "not_configured" ? 409 : 422;
    return new HttpError(status, e.code, e.message, { meta_code: e.metaCode, meta_subcode: e.subcode, platform: e.platform });
  }
  return e;
}

async function metaStep(env, device, op, params) {
  const step = meta.STEPS[op];
  if (!step) throw new HttpError(404, "not_found", "Unknown publishing step.");
  const cmd = await approvedCommand(env, device, params, meta.STEP_PLATFORM[op]);
  try {
    return await step(env, cmd, params);
  } catch (e) {
    throw metaHttp(e);
  }
}

async function metaUpload(request, env, device, url) {
  const q = Object.fromEntries(url.searchParams);
  const platform = String(q.platform || "");
  if (!["instagram", "facebook"].includes(platform)) throw new HttpError(400, "bad_request", "Only Instagram and Facebook take uploads.");
  const length = Number(request.headers.get("Content-Length") || 0);
  const fileSize = Number(q.file_size || 0);
  const offset = Number(q.offset || 0);
  if (!length || length > MAX_UPLOAD || !fileSize || offset < 0 || offset + length > fileSize) {
    throw new HttpError(413, "bad_request", "The upload's size is missing or over the relay's 100 MB limit.");
  }
  const cmd = await approvedCommand(env, device, q, platform);
  try {
    return await meta.upload(env, cmd, platform, String(q.ref || ""), offset, fileSize, request.body, length);
  } catch (e) {
    throw metaHttp(e);
  }
}

/**
 * A clip's preview video from the PC, for the page to play before anything
 * is decided: POST /v1/previews?approval_id=&clip_id=&size=, the MP4 as the
 * body, signed as an upload is. Kept in KV until five minutes after the
 * link expires, then noted in D1, which is what makes the page show it. The
 * PC sends them one at a time once the offer is out; a refusal only means
 * that clip keeps its thumbnail, and preview_not_kept may be tried again.
 */
async function uploadPreview(request, env, device, url) {
  if (!env.PREVIEWS) throw new HttpError(409, "previews_off", "This relay keeps no preview videos.");
  const q = Object.fromEntries(url.searchParams);
  const id = String(q.approval_id || "");
  if (!/^apr_[0-9a-f]{24}$/.test(id) || !/^[1-9]\d{0,11}$/.test(String(q.clip_id || "")) || !/^[1-9]\d*$/.test(String(q.size || ""))) {
    throw new HttpError(400, "bad_request", "The preview's offer, clip or size is malformed.");
  }
  const clipId = Number(q.clip_id);
  const size = Number(q.size);
  if (size > MAX_PREVIEW) throw new HttpError(413, "too_large", "A preview can be at most 4 MiB.");
  if (Number(request.headers.get("Content-Length") || 0) !== size) throw new HttpError(400, "bad_request", "The preview's size doesn't match its body.");
  const approval = await one(env, "SELECT * FROM approvals WHERE id = ? AND device_id = ?", id, device.device_id);
  if (!approval) throw new HttpError(404, "bad_request", "That offer is not on this relay.");
  // Within two minutes of the link expiring, nobody would get to watch it.
  if (!["open", "deferred"].includes(approval.state) || approval.expires_at <= now() + 120) {
    throw new HttpError(409, "closed", "That offer is closed, or its link expires too soon for a preview.");
  }
  if (!(parse(approval.payload, {}).clips || []).some((c) => c.clip_id === clipId)) throw new HttpError(404, "bad_request", "That clip is not in the offer.");
  const buf = await request.arrayBuffer();
  // An MP4 starts with its "ftyp" box: four bytes of size, then the type.
  if (buf.byteLength !== size || size < 8 || String.fromCharCode(...new Uint8Array(buf, 4, 4)) !== "ftyp") {
    throw new HttpError(400, "bad_request", "A preview must be an MP4 video of the size given.");
  }
  try {
    await env.PREVIEWS.put(`preview:${id}:${clipId}`, buf, { expiration: approval.expires_at + 300 });
  } catch (e) {
    console.error("preview not kept", e && e.name);
    throw new HttpError(503, "preview_not_kept", "The relay couldn't keep this preview. Try again.");
  }
  await run(env, "INSERT OR REPLACE INTO previews (approval_id, clip_id, size, at) VALUES (?, ?, ?, ?)", id, clipId, size, now());
  return { ok: true, clip_id: clipId, size };
}

// ---- OAuth callbacks --------------------------------------------------------------------

async function facebookCallback(env, url) {
  if (url.searchParams.get("error")) return html(page.connectedPage("Facebook sign-in was cancelled. Nothing was connected.", false), 400);
  try {
    const out = await meta.finishFacebook(env, url.searchParams.get("state"), url.searchParams.get("code"));
    if (out.choose) return html(page.choosePageHtml(out.choose.state, out.choose.pages));
    await audit(env, { device: out.device, actor: "creator", action: "facebook_connected" });
    return html(page.connectedPage(`Connected the Page ${out.chose.name}${out.chose.instagram ? ` and Instagram ${out.chose.instagram}` : ""}. You can close this tab and go back to Clips Kitty.`));
  } catch (e) {
    return html(page.connectedPage(e instanceof meta.MetaError ? e.message : "Facebook sign-in failed. Try again from Clips Kitty.", false), 400);
  }
}

async function facebookPagePicked(request, env) {
  const form = await request.formData();
  try {
    const out = await meta.choosePage(env, String(form.get("state") || ""), String(form.get("page") || ""));
    await audit(env, { device: out.device, actor: "creator", action: "facebook_connected" });
    return html(page.connectedPage(`Connected the Page ${out.chose.name}${out.chose.instagram ? ` and Instagram ${out.chose.instagram}` : ""}. You can close this tab.`));
  } catch (e) {
    return html(page.connectedPage(e instanceof meta.MetaError ? e.message : "That didn't work. Try again from Clips Kitty.", false), 400);
  }
}

// ---- every ten minutes ---------------------------------------------------------------------

export async function cron(env) {
  const t = now();
  for (const a of await all(env, "SELECT * FROM approvals WHERE state IN ('open', 'deferred') AND expires_at <= ?", t)) {
    const changed = await run(env, "UPDATE approvals SET state = 'expired', updated_at = ? WHERE id = ? AND state IN ('open', 'deferred')", t, a.id);
    if (changed) {
      await addCommand(env, a.device_id, a.id, "expired", { approval_id: a.id, review_ref: a.review_ref });
      await audit(env, { device: a.device_id, approval: a.id, actor: "relay", action: "expired" });
    }
  }
  for (const a of await all(env, "SELECT * FROM approvals WHERE state = 'deferred' AND remind_at > 0 AND remind_at <= ? AND expires_at > ?", t, t)) {
    // Only a WhatsApp number gets a reminder. With none, or WhatsApp off on
    // the PC, remind_at stays for Muse to read (lib/muse.js).
    const recipient = await one(env, "SELECT * FROM recipients WHERE device_id = ? AND state = 'confirmed'", a.device_id);
    if (!recipient) continue;
    const device = await one(env, "SELECT prefs FROM devices WHERE device_id = ?", a.device_id);
    if (!device || !prefsOf(device).whatsapp_enabled) continue;
    const changed = await run(env, "UPDATE approvals SET state = 'open', remind_at = 0, updated_at = ? WHERE id = ? AND state = 'deferred'", t, a.id);
    if (!changed) continue;
    try {
      await sendReady(env, recipient, { id: a.id, payload: parse(a.payload, {}) }, await unseal(env, a.token_enc));
      await addCommand(env, a.device_id, a.id, "reminded", { approval_id: a.id, review_ref: a.review_ref });
      await audit(env, { device: a.device_id, approval: a.id, actor: "relay", action: "reminded" });
    } catch (e) {
      console.error("reminder not sent", e.code);
    }
  }
  await run(env, "DELETE FROM nonces WHERE at < ?", t - NONCE_TTL);
  await run(env, "DELETE FROM wa_seen WHERE at < ?", t - 8 * 86400);
  await run(env, "DELETE FROM oauth_states WHERE expires_at < ?", t);
  await run(env, "DELETE FROM events WHERE at < ?", t - 30 * 86400);
  await run(env, "DELETE FROM thumbs WHERE approval_id IN (SELECT id FROM approvals WHERE created_at < ?)", t - STATUS_TTL);
  try {
    // A preview plays only while its offer is open. Its bytes in KV expire on their own.
    await run(env, "DELETE FROM previews WHERE approval_id NOT IN (SELECT id FROM approvals WHERE state IN ('open', 'deferred'))");
  } catch (e) {
    console.error("previews not cleared", e && e.name);
  }
}
