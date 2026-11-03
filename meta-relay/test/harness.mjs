/**
 * What the relay runs on in tests and in the local demo, without Cloudflare
 * or Meta:
 *
 *   makeD1()     D1's API (prepare/bind/first/all/run) over node:sqlite,
 *                with schema.sql applied.
 *   makeKV()     Workers KV's API (put/get/delete) over a Map, for the
 *                PREVIEWS binding, with KV's expirations.
 *   FakeMeta     a stand-in for graph.facebook.com and rupload.facebook.com:
 *                records every WhatsApp message, fakes OAuth, containers,
 *                uploads (counting the bytes) and permalinks. Nothing leaves
 *                the machine.
 *   makeEnv()    the worker's env with test secrets and KV;
 *                makeEnv({ PREVIEWS: undefined }) is a relay without KV.
 *   signed()     a request signed exactly the way remote_approval/relay.py
 *                signs one.
 */

import { readFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { createHash, createHmac, randomBytes } from "node:crypto";

const SCHEMA = readFileSync(new URL("../schema.sql", import.meta.url), "utf8");

export function makeD1(path = ":memory:") {
  const db = new DatabaseSync(path);
  db.exec(SCHEMA);
  const norm = (args) => args.map((a) => (a === undefined ? null : typeof a === "boolean" ? (a ? 1 : 0) : a));
  const plain = (row) => (row ? { ...row } : null);
  return {
    raw: db,
    prepare(sql) {
      const stmt = db.prepare(sql);
      const bound = (args) => ({
        async first() { return plain(stmt.get(...norm(args))); },
        async all() { return { results: stmt.all(...norm(args)).map(plain) }; },
        async run() { const out = stmt.run(...norm(args)); return { meta: { changes: Number(out.changes) } }; },
      });
      return { bind: (...args) => bound(args), ...bound([]) };
    },
  };
}

/**
 * Like KV, put keeps its own copy of the bytes and refuses an expiration
 * (seconds since the epoch) less than 60 seconds away, and get finds nothing
 * once a key has expired. `raw` is the Map: key -> {bytes, expiration}.
 */
export function makeKV() {
  const raw = new Map();
  const nowS = () => Math.floor(Date.now() / 1000);
  return {
    raw,
    async put(key, value, { expiration } = {}) {
      if (expiration !== undefined && expiration < nowS() + 60) throw new Error("KV PUT failed: 400 Invalid expiration: it must be at least 60 seconds in the future.");
      const bytes = typeof value === "string" ? new TextEncoder().encode(value)
        : ArrayBuffer.isView(value) ? new Uint8Array(value.buffer, value.byteOffset, value.byteLength).slice() : new Uint8Array(value).slice();
      raw.set(key, { bytes, expiration });
    },
    async get(key, options) {
      const type = (typeof options === "string" ? options : options && options.type) || "text";
      const kept = raw.get(key);
      if (!kept || (kept.expiration !== undefined && kept.expiration <= nowS())) return null;
      if (type === "arrayBuffer") return kept.bytes.slice().buffer;
      const text = new TextDecoder().decode(kept.bytes);
      return type === "json" ? JSON.parse(text) : text;
    },
    async delete(key) {
      raw.delete(key);
    },
  };
}

export const BASES = {
  GRAPH_BASE: "https://graph.test/v26.0",
  RUPLOAD_BASE: "https://rupload.test",
};

export function makeEnv(overrides = {}) {
  return {
    DB: makeD1(),
    PREVIEWS: makeKV(),
    ENROLL_KEY: "enroll-key-for-tests-0123456789",
    TOKEN_KEY: randomBytes(32).toString("base64"),
    WHATSAPP_TOKEN: "wa-test-token",
    WHATSAPP_PHONE_NUMBER_ID: "PHONEID",
    WHATSAPP_WABA_ID: "WABAID",
    WEBHOOK_VERIFY_TOKEN: "verify-me",
    META_APP_ID: "1234",
    META_APP_SECRET: "app-secret-for-tests",
    PUBLIC_URL: "https://relay.test",
    WHATSAPP_DISPLAY_NUMBER: "15550001111",
    ...BASES,
    ...overrides,
  };
}

export function makeCtx() {
  const pending = [];
  return {
    waitUntil(p) { pending.push(p); },
    async settle() { while (pending.length) await pending.shift(); },
  };
}

/** Signs like remote_approval/relay.py: METHOD\nPATH?QUERY\nTS\nNONCE\nSHA256(body). */
export function signed(base, device, secret, method, pathAndQuery, body, { ts, nonce, unsigned = false, headers = {} } = {}) {
  const raw = body === undefined ? "" : typeof body === "string" ? body : JSON.stringify(body);
  const stamp = String(ts ?? Math.floor(Date.now() / 1000));
  const n = nonce ?? randomBytes(12).toString("hex");
  const hash = unsigned ? "UNSIGNED-PAYLOAD" : createHash("sha256").update(raw).digest("hex");
  const sig = createHmac("sha256", secret).update([method, pathAndQuery, stamp, n, hash].join("\n")).digest("hex");
  return new Request(base + pathAndQuery, {
    method,
    headers: { "X-CK-Device": device, "X-CK-Timestamp": stamp, "X-CK-Nonce": n, "X-CK-Signature": sig, "Content-Type": "application/json", ...headers },
    body: method === "GET" ? undefined : raw,
  });
}

export function webhookRequest(base, appSecret, payload) {
  const raw = JSON.stringify(payload);
  const sig = createHmac("sha256", appSecret).update(raw).digest("hex");
  return new Request(`${base}/webhook`, { method: "POST", headers: { "X-Hub-Signature-256": `sha256=${sig}`, "Content-Type": "application/json" }, body: raw });
}

export function inboundText(from, text, id) {
  return {
    object: "whatsapp_business_account",
    entry: [{ id: "WABAID", changes: [{ field: "messages", value: {
      messaging_product: "whatsapp",
      metadata: { display_phone_number: "15550001111", phone_number_id: "PHONEID" },
      contacts: [{ profile: { name: "Creator" }, wa_id: from, user_id: `US.${from}` }],
      messages: [{ from, from_user_id: `US.${from}`, id: id || `wamid.in.${randomBytes(6).toString("hex")}`, timestamp: String(Math.floor(Date.now() / 1000)), type: "text", text: { body: text } }],
    } }] }],
  };
}

export function inboundButton(from, payload, contextId, id) {
  return {
    object: "whatsapp_business_account",
    entry: [{ id: "WABAID", changes: [{ field: "messages", value: {
      messaging_product: "whatsapp",
      messages: [{ from, id: id || `wamid.btn.${randomBytes(6).toString("hex")}`, type: "button", context: { from: "15550001111", id: contextId }, button: { payload, text: "x" } }],
    } }] }],
  };
}

export function statusUpdate(wamid, status, errorCode) {
  return {
    object: "whatsapp_business_account",
    entry: [{ id: "WABAID", changes: [{ field: "messages", value: {
      messaging_product: "whatsapp",
      statuses: [{ id: wamid, status, timestamp: "1", recipient_id: "15551234567", ...(errorCode ? { errors: [{ code: errorCode }] } : {}) }],
    } }] }],
  };
}

/**
 * A stand-in for Meta's servers. Install with globalThis.fetch = fake.fetch;
 * anything not on a test host goes to the real fetch it wraps.
 */
export class FakeMeta {
  constructor(realFetch = globalThis.fetch) {
    this.real = realFetch;
    this.messages = [];          // WhatsApp sends, in order
    this.templates = [];         // created templates
    this.templateFails = 0;      // >0: template sends fail with 132001 this many times
    this.uploads = [];           // {platform, bytes, headers}
    this.posts = [];             // {platform, id, permalink, caption}
    this.graphFail = null;       // {match: RegExp, error: {...}} to make one call fail
    this.containers = {};
    this.n = 0;
    this.fetch = this.fetch.bind(this);
  }

  id(prefix) {
    this.n += 1;
    return `${prefix}${this.n}`;
  }

  async fetch(input, init = {}) {
    const url = new URL(typeof input === "string" ? input : input.url);
    if (!["graph.test", "rupload.test"].includes(url.hostname)) return this.real(input, init);
    const method = (init.method || "GET").toUpperCase();
    let body = init.body;
    let form = {};
    let jsonBody = null;
    let bytes = 0;
    if (body instanceof URLSearchParams) form = Object.fromEntries(body);
    else if (typeof body === "string") {
      try { jsonBody = JSON.parse(body); } catch { form = Object.fromEntries(new URLSearchParams(body)); }
    } else if (body && typeof body.getReader === "function") {
      const reader = body.getReader();
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        bytes += value.byteLength;
      }
    } else if (body instanceof Uint8Array || body instanceof ArrayBuffer) {
      bytes = body.byteLength;
    }
    const q = Object.fromEntries(url.searchParams);
    const params = { ...q, ...form };
    const headers = new Headers(init.headers || {});
    const out = (data, status = 200) => new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });
    const p = url.pathname;

    if (this.graphFail && this.graphFail.match.test(`${method} ${url.hostname}${p}`)) {
      const err = this.graphFail.error;
      if (!this.graphFail.sticky) this.graphFail = null;
      return out({ error: err }, 400);
    }

    // ---- WhatsApp ----
    if (p === "/v26.0/PHONEID/messages" && method === "POST") {
      const m = jsonBody;
      if (m.type === "template" && this.templateFails > 0) {
        this.templateFails -= 1;
        return out({ error: { code: 132001, message: "template does not exist" } }, 404);
      }
      const wamid = `wamid.out.${this.id("")}`;
      this.messages.push({ ...m, wamid });
      return out({ messaging_product: "whatsapp", contacts: [{ input: m.to, wa_id: m.to }], messages: [{ id: wamid }] });
    }
    if (p === "/v26.0/WABAID/message_templates" && method === "GET") {
      return out({ data: this.templates.map((t) => ({ name: t.name, status: t.status || "APPROVED", category: "UTILITY" })) });
    }
    if (p === "/v26.0/WABAID/message_templates" && method === "POST") {
      this.templates.push({ ...jsonBody, status: "PENDING" });
      return out({ id: this.id("tpl"), status: "PENDING", category: "UTILITY" });
    }

    // ---- Facebook Login ----
    if (p === "/v26.0/oauth/access_token") {
      if (params.code) return out({ access_token: "fb-short", token_type: "bearer" });
      if (params.fb_exchange_token) return out({ access_token: "fb-long-user", token_type: "bearer", expires_in: 5184000 });
    }
    if (p === "/v26.0/me/accounts") {
      return out({ data: [{ id: "PAGE1", name: "Hoops Daily", access_token: "page-token-1", instagram_business_account: { id: "IG1", username: "hoopsdaily" } }] });
    }

    // ---- Instagram ----
    if (p === "/v26.0/IG1/media" && method === "POST") {
      const id = this.id("IGC");
      this.containers[id] = { platform: "instagram", caption: params.caption, uploaded: false, status: "IN_PROGRESS" };
      return out({ id, uri: `https://rupload.test/ig-api-upload/v26.0/${id}` });
    }
    if (url.hostname === "rupload.test" && method === "POST") {
      const id = p.split("/").pop();
      const c = this.containers[id];
      if (!c) return out({ debug_info: { retriable: false, type: "ProcessingFailedError" } }, 400);
      if (!String(headers.get("Authorization") || "").startsWith("OAuth ")) return out({ error: { code: 190 } }, 401);
      const size = Number(headers.get("file_size"));
      if (size !== bytes) return out({ debug_info: { retriable: false, type: "PartialRequestError" } }, 400);
      c.uploaded = true;
      c.status = "FINISHED";
      this.uploads.push({ platform: c.platform, id, bytes });
      return out({ success: true, message: "Upload successful." });
    }
    if (/^\/v26\.0\/IGC\d+$/.test(p) && method === "GET") {
      const c = this.containers[p.split("/").pop()];
      return out({ status_code: c ? c.status : "ERROR", status: c ? "" : "Unknown container" });
    }
    if (p === "/v26.0/IG1/media_publish" && method === "POST") {
      const c = this.containers[params.creation_id];
      if (!c || !c.uploaded) return out({ error: { code: 9007, error_subcode: 2207027, message: "not ready" } }, 400);
      const id = this.id("IGM");
      this.posts.push({ platform: "instagram", id, permalink: `https://www.instagram.com/reel/${id}/`, caption: c.caption });
      return out({ id });
    }
    if (/^\/v26\.0\/IGM\d+$/.test(p) && method === "GET") {
      const post = this.posts.find((x) => x.id === p.split("/").pop());
      return out({ permalink: post ? post.permalink : "", shortcode: post ? post.id : "" });
    }

    // ---- Facebook Page Reels ----
    if (p === "/v26.0/PAGE1/video_reels" && method === "POST") {
      if (params.upload_phase === "start") {
        const id = this.id("FBV");
        this.containers[id] = { platform: "facebook", uploaded: false, status: "uploading", published: false };
        return out({ video_id: id, upload_url: `https://rupload.test/video-upload/v26.0/${id}` });
      }
      if (params.upload_phase === "finish") {
        const c = this.containers[params.video_id];
        if (!c || !c.uploaded) return out({ error: { code: 6000, message: "no upload" } }, 400);
        c.published = true;
        this.posts.push({ platform: "facebook", id: params.video_id, permalink: `https://www.facebook.com/reel/${params.video_id}`, caption: params.description, title: params.title });
        return out({ success: true });
      }
    }
    if (/^\/v26\.0\/FBV\d+$/.test(p) && method === "GET") {
      const id = p.split("/").pop();
      const c = this.containers[id];
      return out({
        status: { video_status: c && c.published ? "ready" : "processing", publishing_phase: { status: c && c.published ? "complete" : "not_started", publish_status: c && c.published ? "published" : "draft" } },
        permalink_url: `/reel/${id}`,
        post_id: `PAGE1_${id}`,
        id,
      });
    }
    return out({ error: { code: 100, message: `fake meta: no route ${method} ${url.hostname}${p}` } }, 404);
  }
}
