/**
 * The creator's Muse agent, as a custom connector: a small remote MCP server
 * (POST /mcp) and the same tools as plain HTTPS (/muse/v1/...). It lives on
 * this relay because Muse's servers can't reach a PC or a private network.
 *
 * What Muse can do with it is narrow on purpose. It can say which highlights
 * are waiting, hand the creator the approval link, say what got posted, set
 * a reminder and cancel an offer. It can't approve or publish: there is no
 * tool for that, no tool takes or shows the PIN, and the Graph steps still
 * open only under an approve command the PC accepted, which only the
 * approval page makes, after the creator's own PIN (worker.js decide).
 *
 * A key (ckm_...) is made on the PC (POST /v1/muse/token), shown once in
 * Settings and kept in Muse's credential store. The relay keeps only its
 * SHA-256. A key works for one PC, only in the Authorization header, about
 * 60 calls a minute, and stops working when it is replaced or revoked, or
 * the PC disconnects. It can't call the PC's routes (/v1/), and a PC's
 * signature can't call these.
 *
 * MCP: JSON answers only (no SSE stream, no sessions). Both eras are spoken:
 * the initialize handshake (2025-03-26 to 2025-11-25), and the stateless
 * 2026-07-28 revision, where every request names its protocol version in
 * _meta and the MCP-Protocol-Version, Mcp-Method and Mcp-Name headers must
 * agree with the body.
 *
 * Routes
 *   POST /mcp                          the MCP server (GET and DELETE: 405)
 *   GET  /muse/v1/offers               list_ready_highlights
 *   GET  /muse/v1/offers/:id/link      get_approval_link
 *   GET  /muse/v1/posts                list_recent_posts
 *   POST /muse/v1/offers/:id/remind    remind_me_later
 *   POST /muse/v1/offers/:id/cancel    cancel_offer ({"confirm": true})
 *   GET  /muse/v1/status               relay_status
 *   GET  /muse/openapi.json            the REST routes described for Muse (no key needed)
 *   GET  /muse                         how to connect, in plain text (no key needed)
 */

import { randomToken, sha256Hex, unseal } from "./crypto.js";
import { addCommand, all, audit, iso, now, one, parse, pinBlocked, prefsOf, run } from "./db.js";
import { accountsFor, safeLink } from "./meta.js";

export const CONNECTOR_VERSION = "1.0.0";
const KEY = /^bearer\s+(ckm_[A-Za-z0-9_-]{20,128})\s*$/i; // the scheme's case doesn't matter (RFC 6750)
const PER_MINUTE = 60;
const MAX_BODY = 64 * 1024;
const LINK_TTL = 7 * 86400; // the approval page shows an offer this long (worker.js STATUS_TTL)
const PC_QUIET = 15 * 60; // a PC that is on checks in every couple of minutes
const OFFER_ID = /^apr_[0-9a-f]{24}$/;

const LEGACY_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26"];
const STATELESS_VERSIONS = ["2026-07-28"];
const VERSION_META = "io.modelcontextprotocol/protocolVersion";

// JSON-RPC error codes this server answers with.
const PARSE_ERROR = -32700;
const INVALID_REQUEST = -32600;
const METHOD_NOT_FOUND = -32601;
const INVALID_PARAMS = -32602;
const HEADER_MISMATCH = -32020;
const UNSUPPORTED_VERSION = -32022;

const SERVER_INFO = { name: "clips-kitty-relay", title: "Clips Kitty", version: CONNECTOR_VERSION };

const INSTRUCTIONS =
  "Clips Kitty finds highlights in the person's videos and offers them for posting to the accounts the person connected. " +
  "With these tools you can say which highlights are ready, give the person the approval link, say what got posted, " +
  "set a reminder, or cancel an offer. You can't post anything. Publishing happens only on the approval page, where " +
  "the person ticks the clips and places and enters their own PIN. Never ask for the PIN, never type it, and never " +
  "offer to enter it for them. Cancel an offer only when the person clearly asks.";

const NEVER_THE_PIN = "Never ask for the PIN or type it: the person enters it themselves on the approval page.";

// What the tools say once wrong PINs have blocked approving from the phone
// (db.js pinBlocked), so Muse never hands out a link as if it could approve.
const BLOCKED =
  "Approving from the phone is blocked after too many wrong PINs. The person sets a new PIN in Clips Kitty on " +
  "their PC to unblock it. Until then the page's Remind me later and Cancel still work, but it can't approve.";

class MuseError extends Error {
  constructor(status, code, message, headers = {}) {
    super(message);
    this.status = status;
    this.code = code;
    this.headers = headers;
  }
}

const bad = (message) => new MuseError(400, "bad_request", message);

// ---- keys, from the PC's signed routes (worker.js) ---------------------------

/** A new key for this PC, replacing any older one. Returned once; only its hash is kept. */
export async function mintKey(env, deviceId) {
  const key = `ckm_${randomToken(32)}`;
  const t = now();
  await run(env, "UPDATE connector_tokens SET revoked_at = ? WHERE device_id = ? AND revoked_at = 0", t, deviceId);
  await run(env, "INSERT INTO connector_tokens (token_hash, device_id, created_at) VALUES (?, ?, ?)", await sha256Hex(key), deviceId, t);
  return key;
}

/** Stop this PC's key working. Returns how many were working (0 or 1). */
export async function revokeKeys(env, deviceId) {
  return run(env, "UPDATE connector_tokens SET revoked_at = ? WHERE device_id = ? AND revoked_at = 0", now(), deviceId);
}

export async function activeKey(env, deviceId) {
  return one(env, "SELECT * FROM connector_tokens WHERE device_id = ? AND revoked_at = 0 ORDER BY created_at DESC LIMIT 1", deviceId);
}

/** What /v1/status tells the PC: whether a key works, never the key. */
export async function keyStatus(env, deviceId) {
  const row = await activeKey(env, deviceId);
  return row ? { enabled: true, created_at: row.created_at, last_used: row.last_used } : { enabled: false, created_at: 0, last_used: 0 };
}

// ---- routing -------------------------------------------------------------------

export function handles(path) {
  return path === "/mcp" || path === "/muse" || path === "/muse/openapi.json" || path.startsWith("/muse/v1/");
}

export async function route(request, env, url) {
  const path = url.pathname;
  try {
    if (path === "/mcp") return await mcp(request, env);
    if (path === "/muse" || path === "/muse/openapi.json") {
      if (request.method !== "GET") return reply({ code: "method_not_allowed", error: "Use GET." }, 405, { Allow: "GET" });
      return path === "/muse" ? guide(env) : reply(openapi(env));
    }
    return await rest(request, env, url);
  } catch (e) {
    if (!(e instanceof MuseError)) throw e;
    if (path === "/mcp") return rpcError(null, INVALID_REQUEST, e.message, e.status, undefined, e.headers);
    return reply({ code: e.code, error: e.message }, e.status, e.headers);
  }
}

function reply(data, status = 200, headers = {}) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", ...headers },
  });
}

function checkOrigin(request, env) {
  const origin = request.headers.get("Origin");
  if (!origin) return;
  let mine = "";
  try {
    mine = new URL(env.PUBLIC_URL).origin;
  } catch {
    // No PUBLIC_URL: no page can be this relay's own.
  }
  if (origin !== mine) throw new MuseError(403, "forbidden", "That request came from another website.");
}

async function readBody(request) {
  if (Number(request.headers.get("Content-Length") || 0) > MAX_BODY) throw new MuseError(413, "too_large", "The request is too large.");
  const text = await request.text();
  if (text.length > MAX_BODY) throw new MuseError(413, "too_large", "The request is too large.");
  return text;
}

/** The caller's PC, from its Muse key: the Authorization header and nothing else. */
async function authorize(request, env) {
  const given = request.headers.get("Authorization") || "";
  const m = given.match(KEY);
  const challenge = `Bearer realm="Clips Kitty relay"${given ? ', error="invalid_token"' : ""}`;
  const refuse = (message) => new MuseError(401, given ? "invalid_token" : "unauthorized", message, { "WWW-Authenticate": challenge });
  if (!m) throw refuse("This needs the header Authorization: Bearer <your Muse key>. Make a key in Clips Kitty: Settings, Notifications & Remote Approval.");
  const hash = await sha256Hex(m[1]);
  const key = await one(env, "SELECT * FROM connector_tokens WHERE token_hash = ? AND revoked_at = 0", hash);
  const device = key && (await one(env, "SELECT * FROM devices WHERE device_id = ?", key.device_id));
  if (!device) throw refuse("That Muse key is not active. Make a new one in Clips Kitty: Settings, Notifications & Remote Approval.");
  // A fixed one-minute window, counted in the same write that marks the key used.
  const t = now();
  await run(env, `UPDATE connector_tokens SET
      window_count = CASE WHEN window_start <= ? THEN 1 ELSE window_count + 1 END,
      window_start = CASE WHEN window_start <= ? THEN ? ELSE window_start END,
      last_used = ?
    WHERE token_hash = ?`, t - 60, t - 60, t, t, hash);
  const used = await one(env, "SELECT window_start, window_count FROM connector_tokens WHERE token_hash = ?", hash);
  if (used && used.window_count > PER_MINUTE) {
    throw new MuseError(429, "rate_limited", "Too many calls with this key. Wait a minute and try again.",
      { "Retry-After": String(Math.max(1, used.window_start + 60 - t)) });
  }
  return { device };
}

// ---- MCP -----------------------------------------------------------------------

function rpcResult(id, result) {
  return reply({ jsonrpc: "2.0", id, result });
}

function rpcError(id, code, message, status = 200, data = undefined, headers = {}) {
  return reply({ jsonrpc: "2.0", id, error: { code, message, ...(data ? { data } : {}) } }, status, headers);
}

async function mcp(request, env) {
  if (request.method !== "POST") {
    // No event stream to open (GET) and no session to end (DELETE): every answer is one JSON reply.
    return rpcError(null, INVALID_REQUEST, "Send MCP messages with POST.", 405, undefined, { Allow: "POST" });
  }
  checkOrigin(request, env);
  const raw = await readBody(request);
  const caller = await authorize(request, env);
  let message;
  try {
    message = JSON.parse(raw);
  } catch {
    return rpcError(null, PARSE_ERROR, "That is not valid JSON.", 400);
  }
  if (Array.isArray(message)) return rpcError(null, INVALID_REQUEST, "Send one JSON-RPC message per request.", 400);
  if (!message || typeof message !== "object" || message.jsonrpc !== "2.0") return rpcError(null, INVALID_REQUEST, "That is not a JSON-RPC 2.0 message.", 400);
  const hasId = Object.prototype.hasOwnProperty.call(message, "id");
  // A notification (initialized, cancelled), or an answer to a request this
  // server never makes: accepted, and nothing to say back.
  if (typeof message.method === "string" && !hasId) return new Response(null, { status: 202 });
  if (message.method === undefined && hasId && ("result" in message || "error" in message)) return new Response(null, { status: 202 });
  const { id, method } = message;
  if (typeof method !== "string" || (typeof id !== "string" && typeof id !== "number")) {
    return rpcError(null, INVALID_REQUEST, "A request needs a method and a string or number id.", 400);
  }
  const params = message.params && typeof message.params === "object" && !Array.isArray(message.params) ? message.params : {};
  const refused = checkVersion(request, id, method, params);
  if (refused) return refused;

  if (method === "initialize") return rpcResult(id, handshake(params));
  if (method === "server/discover") return rpcResult(id, discover());
  if (method === "ping") return rpcResult(id, {});
  if (method === "tools/list") return rpcResult(id, { tools: TOOLS.map(({ run: _run, ...tool }) => tool) });
  if (method === "tools/call") {
    const tool = TOOLS.find((t) => t.name === params.name);
    // An unknown tool is the client's mistake, so a protocol error. A tool
    // that runs and refuses is isError instead, for the model to read.
    if (!tool) return rpcError(id, INVALID_PARAMS, `Unknown tool: ${String(params.name).slice(0, 60)}`);
    const args = params.arguments === undefined ? {} : params.arguments;
    if (!args || typeof args !== "object" || Array.isArray(args)) return rpcResult(id, toolError("The arguments must be a JSON object."));
    try {
      const out = await tool.run(env, caller, args);
      return rpcResult(id, {
        content: [{ type: "text", text: out.text }, { type: "text", text: JSON.stringify(out.data) }],
        structuredContent: out.data,
        isError: false,
      });
    } catch (e) {
      if (e instanceof MuseError) return rpcResult(id, toolError(e.message));
      throw e;
    }
  }
  return rpcError(id, METHOD_NOT_FOUND, `Unknown method: ${method.slice(0, 60)}`);
}

function toolError(text) {
  return { content: [{ type: "text", text }], isError: true };
}

/**
 * Which MCP era a request is from. One without the version in _meta is from
 * the initialize era, where the version was agreed in the handshake and
 * rides in a header after it. One with it is stateless (2026-07-28): its
 * headers must say what its body says, so nothing that routes on the
 * headers can be told one thing while this server does another.
 */
function checkVersion(request, id, method, params) {
  const meta = params._meta && typeof params._meta === "object" ? params._meta : {};
  const asked = meta[VERSION_META];
  const header = (name) => request.headers.get(name) ?? "";
  if (asked === undefined) {
    const given = header("MCP-Protocol-Version");
    if (given && ![...LEGACY_VERSIONS, ...STATELESS_VERSIONS].includes(given)) {
      return rpcError(id, INVALID_REQUEST, "Unsupported MCP-Protocol-Version header.", 400);
    }
    return null;
  }
  if (!STATELESS_VERSIONS.includes(asked)) {
    return rpcError(id, UNSUPPORTED_VERSION, "Unsupported protocol version.", 400,
      { supported: STATELESS_VERSIONS, requested: String(asked).slice(0, 40) });
  }
  const name = ["tools/call", "prompts/get"].includes(method) ? params.name : method === "resources/read" ? params.uri : undefined;
  if (header("MCP-Protocol-Version") !== asked || header("Mcp-Method") !== method || header("Mcp-Name") !== String(name ?? "")) {
    return rpcError(id, HEADER_MISMATCH, "The MCP-Protocol-Version, Mcp-Method and Mcp-Name headers must match the request body.", 400);
  }
  return null;
}

function handshake(params) {
  // The client's version back when it is one this server speaks; never a
  // session id, since every request stands alone.
  const asked = params.protocolVersion;
  return {
    protocolVersion: LEGACY_VERSIONS.includes(asked) ? asked : LEGACY_VERSIONS[0],
    capabilities: { tools: { listChanged: false } },
    serverInfo: SERVER_INFO,
    instructions: INSTRUCTIONS,
  };
}

function discover() {
  return {
    supportedVersions: STATELESS_VERSIONS,
    capabilities: { tools: { listChanged: false } },
    serverInfo: SERVER_INFO,
    instructions: INSTRUCTIONS,
  };
}

// ---- the tools -------------------------------------------------------------------

const ID_SCHEMA = {
  type: "string",
  pattern: "^apr_[0-9a-f]{24}$",
  description: "The offer's id (apr_...), from list_ready_highlights.",
};

/** In this order in tools/list. None approves, publishes, or takes or shows a PIN. */
const TOOLS = [
  {
    name: "list_ready_highlights",
    title: "Highlights ready to review",
    description:
      "The highlights Clips Kitty found in new videos that are waiting for the person to decide: each offer's title, " +
      "what was found, its clips, where it can post and when its link expires. Newest first. Read only.",
    inputSchema: {
      type: "object",
      properties: {
        match: { type: "string", maxLength: 200, description: "Only offers whose title, summary or clips mention these words, like a team or game." },
        since: { type: "string", format: "date-time", description: "Only offers made at or after this time, like 2026-11-04T18:00:00Z." },
        limit: { type: "integer", minimum: 1, maximum: 20, description: "At most this many offers. Defaults to 10." },
        include_captions: { type: "boolean", description: "Also give the caption each clip would be posted with on each platform." },
      },
      additionalProperties: false,
    },
    annotations: { title: "Highlights ready to review", readOnlyHint: true, openWorldHint: false },
    run: listReady,
  },
  {
    name: "get_approval_link",
    title: "Approval link",
    description:
      "The approval link for one offer, to give the person. They open it on their phone, tick the clips and the " +
      "places to post, and approve there with their own PIN. Give approval_id, or match to pick by name; with " +
      "neither, the one open offer. When none or several match, the choices come back instead: ask which one. " +
      `Read only. ${NEVER_THE_PIN}`,
    inputSchema: {
      type: "object",
      properties: {
        approval_id: ID_SCHEMA,
        match: { type: "string", maxLength: 200, description: "Words from the offer's title or clips, when there is no approval_id." },
      },
      additionalProperties: false,
    },
    annotations: { title: "Approval link", readOnlyHint: true, openWorldHint: false },
    run: getLink,
  },
  {
    name: "list_recent_posts",
    title: "What got posted",
    description:
      "Offers approved in the last few days and what became of each clip on each platform it went to, with " +
      "the links those platforms returned. Only links a platform returned are given. Read only.",
    inputSchema: {
      type: "object",
      properties: {
        days: { type: "integer", minimum: 1, maximum: 7, description: "How many days back. Defaults to 7." },
        limit: { type: "integer", minimum: 1, maximum: 20, description: "At most this many offers, newest first. Defaults to 10." },
      },
      additionalProperties: false,
    },
    annotations: { title: "What got posted", readOnlyHint: true, openWorldHint: false },
    run: recentPosts,
  },
  {
    name: "remind_me_later",
    title: "Remind me later",
    description:
      "Put an offer aside until later, as the page's Remind me later button does. The link keeps working. When " +
      "WhatsApp is set up it sends the link again then; otherwise remind the person yourself at remind_at. " +
      "Never posts anything.",
    inputSchema: {
      type: "object",
      properties: {
        approval_id: ID_SCHEMA,
        minutes: { type: "integer", minimum: 15, maximum: 1440, description: "How long to wait. Defaults to the person's setting, usually 2 hours." },
      },
      required: ["approval_id"],
      additionalProperties: false,
    },
    annotations: { title: "Remind me later", readOnlyHint: false, destructiveHint: false, idempotentHint: false, openWorldHint: false },
    run: remind,
  },
  {
    name: "cancel_offer",
    title: "Cancel an offer",
    description:
      "Cancel an offer so nothing from that video is posted, as the page's Cancel button does. It can't be undone. " +
      "Only call it after the person clearly says to cancel, with confirm set to true.",
    inputSchema: {
      type: "object",
      properties: {
        approval_id: ID_SCHEMA,
        confirm: { type: "boolean", const: true, description: "true, once the person has said to cancel." },
      },
      required: ["approval_id", "confirm"],
      additionalProperties: false,
    },
    annotations: { title: "Cancel an offer", readOnlyHint: false, destructiveHint: true, idempotentHint: true, openWorldHint: false },
    run: cancelOffer,
  },
  {
    name: "relay_status",
    title: "Relay status",
    description:
      "Whether Clips Kitty is ready: when the PC last checked in, whether an approval PIN is set, the WhatsApp " +
      "number's last four digits, the connected accounts and how many offers are open. Read only.",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
    annotations: { title: "Relay status", readOnlyHint: true, openWorldHint: false },
    run: relayStatus,
  },
];

// ---- arguments: checked here, whatever a client's own checks did ------------------

function words(value, name) {
  if (value === undefined || value === null) return "";
  if (typeof value !== "string") throw bad(`${name} must be text.`);
  return value.trim().slice(0, 200);
}

function whole(value, name, low, high, fallback) {
  if (value === undefined || value === null) return fallback;
  if (!Number.isInteger(value) || value < low || value > high) throw bad(`${name} must be a whole number from ${low} to ${high}.`);
  return value;
}

function yesNo(value, name) {
  if (value === undefined || value === null) return false;
  if (typeof value !== "boolean") throw bad(`${name} must be true or false.`);
  return value;
}

function moment(value, name) {
  if (value === undefined || value === null || value === "") return 0;
  const t = typeof value === "string" ? Date.parse(value) : NaN;
  if (!Number.isFinite(t)) throw bad(`${name} must be a date and time like 2026-11-04T18:00:00Z.`);
  return Math.floor(t / 1000);
}

function offerId(value, required) {
  if (value === undefined || value === null || value === "") {
    if (required) throw bad("approval_id is needed: the apr_... id list_ready_highlights gives.");
    return "";
  }
  if (typeof value !== "string" || !OFFER_ID.test(value)) throw bad("approval_id is the apr_... id list_ready_highlights gives.");
  return value;
}

// ---- reading offers ----------------------------------------------------------------

async function openOffers(env, deviceId) {
  return all(env, "SELECT * FROM approvals WHERE device_id = ? AND state IN ('open', 'deferred') AND expires_at > ? ORDER BY created_at DESC, id",
    deviceId, now());
}

async function mine(env, device, id) {
  const approval = await one(env, "SELECT * FROM approvals WHERE id = ? AND device_id = ?", id, device.device_id);
  if (!approval || now() > approval.created_at + LINK_TTL) throw new MuseError(404, "not_found", "No offer has that id. list_ready_highlights shows the open ones.");
  return approval;
}

function isOpen(approval) {
  return ["open", "deferred"].includes(approval.state) && approval.expires_at > now();
}

function stateOf(approval) {
  return ["open", "deferred"].includes(approval.state) && approval.expires_at <= now() ? "expired" : approval.state;
}

const CLOSED = {
  approved: "already approved",
  done: "already posted",
  rejected: "refused by the PC",
  cancelled: "already cancelled",
  expired: "expired",
};

function closed(approval) {
  return new MuseError(409, "closed", `That offer is ${CLOSED[stateOf(approval)] || stateOf(approval)}, so nothing more can be done with it.`);
}

/** Every word somewhere in the offer's title, summary or clips. */
function matches(approval, text) {
  if (!text) return true;
  const p = parse(approval.payload, {});
  const hay = [p.title, p.summary_text, ...(p.clips || []).flatMap((c) => [c.title, c.label])].join(" ").toLowerCase();
  return text.toLowerCase().split(/\s+/).every((w) => hay.includes(w));
}

/** An offer as Muse sees it: what the page shows, never its link token or a thumbnail. */
function offerView(approval, accounts, captions) {
  const p = parse(approval.payload, {});
  // Like the page: only the connected accounts, and only their captions.
  const places = Object.keys(accounts).filter((k) => accounts[k].connected);
  return {
    approval_id: approval.id,
    title: p.title || "your video",
    summary: p.summary_text || "",
    kind: p.kind || "",
    state: approval.state,
    approve_from_phone: Boolean(approval.remote_ok),
    clips: (p.clips || []).map((c) => ({
      clip_id: c.clip_id,
      title: c.title || "",
      label: c.label || "",
      category: c.category || "",
      duration_s: Math.round(Number(c.duration) || 0),
      already_posted: c.published || [],
      ...(captions ? { captions: Object.fromEntries(Object.entries(c.texts || {}).filter(([k]) => places.includes(k))) } : {}),
    })),
    preferred: (p.preferred || []).filter((k) => places.includes(k)),
    accounts: Object.fromEntries(places.map((k) => [k, accounts[k]])),
    created_at: iso(approval.created_at),
    expires_at: iso(approval.expires_at),
    remind_at: approval.remind_at ? iso(approval.remind_at) : "",
  };
}

function pcLine(device) {
  if (!device.last_seen) return "The PC has not checked in with the relay yet.";
  if (now() - device.last_seen > PC_QUIET) {
    return `The PC last checked in at ${iso(device.last_seen)}, so anything approved is posted when it is back on.`;
  }
  return "";
}

async function listReady(env, { device }, args) {
  const match = words(args.match, "match");
  const since = moment(args.since, "since");
  const limit = whole(args.limit, "limit", 1, 20, 10);
  const captions = yesNo(args.include_captions, "include_captions");
  const accounts = await accountsFor(env, device.device_id);
  const offers = (await openOffers(env, device.device_id))
    .filter((a) => a.created_at >= since && matches(a, match))
    .slice(0, limit)
    .map((a) => offerView(a, accounts, captions));
  const blocked = await pinBlocked(env, device.device_id);
  const data = { offers, pc_last_seen: device.last_seen ? iso(device.last_seen) : "", phone_approval_blocked: blocked };
  const lines = offers.map((o, i) => `${i + 1}. ${o.title}: ${o.summary || `${o.clips.length} clips`}. The link expires at ${o.expires_at}. (${o.approval_id})`);
  // How these get published: the page and the PIN, unless the page can't approve them.
  let how = "";
  if (offers.some((o) => o.approve_from_phone)) {
    how = blocked ? BLOCKED : "To publish, give the person the approval link (get_approval_link). They approve on that page with their own PIN.";
  } else if (offers.length) {
    how = "Approving from the phone is switched off on their PC, so they publish these from Clips Kitty on the PC.";
  }
  const text = [
    offers.length ? `${offers.length === 1 ? "One offer is" : `${offers.length} offers are`} waiting for a decision:\n${lines.join("\n")}` : "No highlights are waiting for a decision.",
    how,
    pcLine(device),
  ].filter(Boolean).join("\n");
  return { data, text };
}

function choice(approval) {
  const p = parse(approval.payload, {});
  return { approval_id: approval.id, title: p.title || "your video", summary: p.summary_text || "", created_at: iso(approval.created_at), expires_at: iso(approval.expires_at) };
}

async function getLink(env, { device }, args) {
  const id = offerId(args.approval_id, false);
  const match = words(args.match, "match");
  if (id) return linkOf(env, device, await mine(env, device, id));
  const open = await openOffers(env, device.device_id);
  const found = open.filter((a) => matches(a, match));
  if (found.length === 1) return linkOf(env, device, found[0]);
  const choices = (found.length ? found : open).map(choice);
  const listed = choices.map((c) => `- ${c.title}: ${c.summary} (${c.approval_id})`).join("\n");
  let text = "No offer is waiting, so there is no link to give.";
  if (found.length) text = `${found.length} offers match. Ask the person which one, then call again with its approval_id:\n${listed}`;
  else if (open.length) text = `No open offer matches "${match}". Ask the person which one, then call again with its approval_id:\n${listed}`;
  return { data: { url: "", choices }, text };
}

async function linkOf(env, device, approval) {
  // The same link the WhatsApp message carries, rebuilt the way reminders do.
  const url = `${env.PUBLIC_URL}/a/${await unseal(env, approval.token_enc)}`;
  await audit(env, { device: device.device_id, approval: approval.id, actor: "muse", action: "link_shared" });
  const p = parse(approval.payload, {});
  const title = p.title || "your video";
  const state = stateOf(approval);
  const blocked = await pinBlocked(env, device.device_id);
  let text = `This offer is ${CLOSED[state] || state}. Its page shows where it stands: ${url}`;
  if (isOpen(approval) && !approval.remote_ok) {
    text = `Here is the link for ${title}: ${url}\nApproving from the phone is switched off on their PC, so the page only shows the clips.`;
  } else if (isOpen(approval) && blocked) {
    text = `Here is the link for ${title}: ${url}\n${BLOCKED}`;
  } else if (isOpen(approval)) {
    text = `Here is the approval link for ${title}: ${url}\nThe person opens it on their phone, ticks the clips and the places to post, and enters their own PIN there. ${NEVER_THE_PIN}`;
  }
  return {
    data: {
      approval_id: approval.id, title, url, state, approve_from_phone: Boolean(approval.remote_ok), phone_approval_blocked: blocked,
      expires_at: iso(approval.expires_at), choices: [],
    },
    text,
  };
}

// ---- what got posted --------------------------------------------------------------

function decidedView(approval) {
  const p = parse(approval.payload, {});
  const decision = parse(approval.decision, {});
  const pc = parse(approval.pc_answer, {});
  const results = parse(approval.results, {});
  const titles = new Map((p.clips || []).map((c) => [c.clip_id, c.title || c.label || ""]));
  const clips = approval.state === "done"
    ? (results.clips || []).map((c) => ({
      clip_id: c.clip_id,
      title: c.title || titles.get(c.clip_id) || "",
      // Only a link a platform returned (the PC passes on what the API
      // said, the relay kept only Meta's own hosts), never one made up.
      posts: (c.posts || []).map((x) => ({ platform: x.platform, state: x.state, url: x.state === "published" ? safeLink(x.url) : "", error: x.error || "" })),
    }))
    : (decision.clips || []).map((clipId) => ({ clip_id: clipId, title: titles.get(clipId) || "", posts: [] }));
  const t = results.totals || {};
  let status = "Approved. Waiting for the PC to pick it up.";
  if (approval.state === "done") {
    const total = ["published", "failed", "unconfirmed", "skipped"].reduce((n, k) => n + (Number(t[k]) || 0), 0);
    status = `${Number(t.published) || 0} of ${total} posts published.`;
  } else if (approval.state === "rejected") {
    status = `The PC did not publish it: ${pc.reason || "it refused the approval"}.`;
  } else if (pc.outcome === "accepted") {
    status = "Approved. The PC is publishing it.";
  }
  return {
    approval_id: approval.id,
    title: p.title || "your video",
    state: approval.state,
    status,
    decided_at: decision.decided_at ? iso(decision.decided_at) : "",
    platforms: decision.platforms || [],
    totals: results.totals || {},
    clips,
  };
}

async function recentPosts(env, { device }, args) {
  const days = whole(args.days, "days", 1, 7, 7);
  const limit = whole(args.limit, "limit", 1, 20, 10);
  const rows = await all(env, `SELECT * FROM approvals WHERE device_id = ? AND state IN ('approved', 'done', 'rejected') AND updated_at >= ?
    ORDER BY updated_at DESC, id LIMIT ?`, device.device_id, now() - days * 86400, limit);
  const offers = rows.map(decidedView);
  const lines = offers.map((o) => {
    const links = o.clips.flatMap((c) => c.posts.filter((x) => x.url).map((x) => `${x.platform}: ${x.url}`));
    return `- ${o.title}: ${o.status}${links.length ? ` ${links.join(", ")}` : ""}`;
  });
  const text = offers.length
    ? `Approved in the last ${days} day${days === 1 ? "" : "s"}:\n${lines.join("\n")}`
    : `Nothing was approved in the last ${days} day${days === 1 ? "" : "s"}.`;
  return { data: { offers, days }, text };
}

// ---- remind and cancel: what the page's buttons do, marked as Muse's --------------

/**
 * Whether cron sends a deferred offer's link again: a confirmed number, with
 * WhatsApp messages on. The approval page says the same (worker.js laterChannel).
 */
export async function whatsappReminds(env, device) {
  const recipient = await one(env, "SELECT device_id FROM recipients WHERE device_id = ? AND state = 'confirmed'", device.device_id);
  return Boolean(recipient) && Boolean(prefsOf(device).whatsapp_enabled);
}

async function remind(env, { device }, args) {
  const id = offerId(args.approval_id, true);
  const minutes = args.minutes === undefined || args.minutes === null ? 0 : whole(args.minutes, "minutes", 15, 1440, 0);
  const approval = await mine(env, device, id);
  if (!isOpen(approval)) throw closed(approval);
  const payload = parse(approval.payload, {});
  const at = now() + (minutes || Number(payload.remind_minutes) || prefsOf(device).remind_minutes) * 60;
  if (at >= approval.expires_at - 300) {
    throw new MuseError(409, "too_late", "The link expires before that reminder would come. Ask the person to decide now, or to ask for a new offer from the app later.");
  }
  const changed = await run(env, "UPDATE approvals SET state = 'deferred', remind_at = ?, updated_at = ? WHERE id = ? AND state IN ('open', 'deferred')",
    at, now(), approval.id);
  if (!changed) throw closed(await mine(env, device, id));
  await addCommand(env, device.device_id, approval.id, "defer", { approval_id: approval.id, review_ref: approval.review_ref, remind_at: iso(at), via: "muse" });
  await audit(env, { device: device.device_id, approval: approval.id, actor: "muse", action: "deferred" });
  const whatsapp = await whatsappReminds(env, device);
  return {
    data: { approval_id: approval.id, state: "deferred", remind_at: iso(at), whatsapp_reminder: whatsapp },
    text: whatsapp
      ? `Reminder set for ${iso(at)}. WhatsApp sends the link again then.`
      : `Reminder set for ${iso(at)}. Nothing else will send one, so remind the person yourself then.`,
  };
}

async function cancelOffer(env, { device }, args) {
  const id = offerId(args.approval_id, true);
  if (args.confirm !== true) {
    throw bad("Cancelling can't be undone. Ask the person first, then call again with confirm set to true.");
  }
  const approval = await mine(env, device, id);
  if (!isOpen(approval)) throw closed(approval);
  const changed = await run(env, "UPDATE approvals SET state = 'cancelled', updated_at = ? WHERE id = ? AND state IN ('open', 'deferred')", now(), approval.id);
  if (!changed) throw closed(await mine(env, device, id));
  await addCommand(env, device.device_id, approval.id, "cancel", { approval_id: approval.id, review_ref: approval.review_ref, decided_at: iso(now()), via: "muse" });
  await audit(env, { device: device.device_id, approval: approval.id, actor: "muse", action: "cancelled" });
  const title = parse(approval.payload, {}).title || "your video";
  return { data: { approval_id: approval.id, state: "cancelled" }, text: `Cancelled. Nothing from ${title} will be posted.` };
}

async function relayStatus(env, { device }) {
  const r = await one(env, "SELECT * FROM recipients WHERE device_id = ?", device.device_id);
  const open = await one(env, "SELECT COUNT(*) AS n FROM approvals WHERE device_id = ? AND state IN ('open', 'deferred') AND expires_at > ?", device.device_id, now());
  const accounts = await accountsFor(env, device.device_id);
  const data = {
    pc_last_seen: device.last_seen ? iso(device.last_seen) : "",
    pin_set: Boolean(device.pin_hash),
    phone_approval_blocked: await pinBlocked(env, device.device_id),
    whatsapp: { state: r ? r.state : "none", tail: r && r.wa_id ? r.wa_id.slice(-4) : "", messages_on: Boolean(prefsOf(device).whatsapp_enabled) },
    accounts,
    open_offers: open ? open.n : 0,
  };
  const connected = Object.entries(accounts).filter(([, a]) => a.connected).map(([p, a]) => `${p} (${a.name})`);
  let pinLine = "No approval PIN is set yet, so nothing can be approved from the phone. The person sets one in Clips Kitty on their PC.";
  if (data.pin_set) pinLine = data.phone_approval_blocked ? BLOCKED : "An approval PIN is set.";
  const text = [
    `${data.open_offers} offer${data.open_offers === 1 ? "" : "s"} waiting.`,
    pinLine,
    connected.length ? `Connected: ${connected.join(", ")}.` : "No account is connected yet.",
    pcLine(device),
  ].filter(Boolean).join("\n");
  return { data, text };
}

// ---- the same tools as plain HTTPS ---------------------------------------------------

/** Each REST route: its tool, and where each argument comes from. */
const REST = [
  { method: "GET", path: "/muse/v1/offers", match: /^\/muse\/v1\/offers$/, tool: "list_ready_highlights", query: ["match", "since", "limit", "include_captions"] },
  { method: "GET", path: "/muse/v1/offers/{approval_id}/link", match: /^\/muse\/v1\/offers\/(apr_[0-9a-f]{24})\/link$/, tool: "get_approval_link" },
  { method: "GET", path: "/muse/v1/posts", match: /^\/muse\/v1\/posts$/, tool: "list_recent_posts", query: ["days", "limit"] },
  { method: "POST", path: "/muse/v1/offers/{approval_id}/remind", match: /^\/muse\/v1\/offers\/(apr_[0-9a-f]{24})\/remind$/, tool: "remind_me_later", body: ["minutes"] },
  { method: "POST", path: "/muse/v1/offers/{approval_id}/cancel", match: /^\/muse\/v1\/offers\/(apr_[0-9a-f]{24})\/cancel$/, tool: "cancel_offer", body: ["confirm"] },
  { method: "GET", path: "/muse/v1/status", match: /^\/muse\/v1\/status$/, tool: "relay_status" },
];

/** A query string value as the tool's arguments would carry it. */
function fromQuery(value, schema) {
  if (value === null) return undefined;
  if (schema.type === "integer") return /^-?\d{1,6}$/.test(value) ? Number(value) : value;
  if (schema.type === "boolean") return value === "true" || value === "1" ? true : value === "false" || value === "0" ? false : value;
  return value;
}

async function rest(request, env, url) {
  checkOrigin(request, env);
  const raw = request.method === "POST" ? await readBody(request) : "";
  const caller = await authorize(request, env);
  const routes = REST.filter((r) => r.match.test(url.pathname));
  if (!routes.length) throw new MuseError(404, "not_found", "Unknown Muse route. See /muse/openapi.json.");
  const r = routes.find((x) => x.method === request.method);
  if (!r) throw new MuseError(405, "method_not_allowed", `Use ${routes[0].method}.`, { Allow: routes[0].method });
  const tool = TOOLS.find((t) => t.name === r.tool);
  const body = raw ? parse(raw, null) : {};
  if (!body || typeof body !== "object" || Array.isArray(body)) throw bad("The request body must be a JSON object.");
  const args = {};
  for (const name of r.query || []) args[name] = fromQuery(url.searchParams.get(name), tool.inputSchema.properties[name]);
  for (const name of r.body || []) args[name] = body[name];
  const id = url.pathname.match(r.match)[1];
  if (id) args.approval_id = id;
  const out = await tool.run(env, caller, args);
  return reply(out.data);
}

// ---- for Muse to read: the OpenAPI description and the connect guide ---------------

const RESPONSES = {
  200: { description: "The answer, as JSON.", content: { "application/json": { schema: { type: "object" } } } },
  400: { description: "An argument is missing or out of range. The error says which." },
  401: { description: "No Muse key, or one that no longer works. Send Authorization: Bearer <key>." },
  404: { description: "No such offer for this PC." },
  409: { description: "The offer is closed (approved, cancelled or expired), or its link expires before the reminder." },
  429: { description: "More than about 60 calls a minute with this key. Retry-After says when to try again." },
};

function base(env) {
  return String(env.PUBLIC_URL || "").replace(/\/$/, "");
}

export function openapi(env) {
  const paths = {};
  for (const r of REST) {
    const tool = TOOLS.find((t) => t.name === r.tool);
    const props = tool.inputSchema.properties;
    const read = tool.annotations.readOnlyHint;
    const op = {
      operationId: r.tool.replace(/_(\w)/g, (_, c) => c.toUpperCase()),
      summary: tool.title,
      description: tool.description,
      tags: [read ? "read" : "write"],
      "x-read-only": read,
      parameters: [
        ...(r.path.includes("{approval_id}") ? [{ name: "approval_id", in: "path", required: true, schema: ID_SCHEMA }] : []),
        ...(r.query || []).map((name) => ({ name, in: "query", required: false, schema: props[name] })),
      ],
      responses: RESPONSES,
    };
    if (r.body) {
      const required = (tool.inputSchema.required || []).filter((k) => r.body.includes(k));
      op.requestBody = {
        required: required.length > 0,
        content: { "application/json": { schema: { type: "object", properties: Object.fromEntries(r.body.map((k) => [k, props[k]])), required, additionalProperties: false } } },
      };
    }
    paths[r.path] = { ...(paths[r.path] || {}), [r.method.toLowerCase()]: op };
  }
  return {
    openapi: "3.1.0",
    info: {
      title: "Clips Kitty for Muse",
      version: CONNECTOR_VERSION,
      description:
        "Say which highlights Clips Kitty found in new videos, give the person the approval link, say what got posted, " +
        "set a reminder or cancel an offer. Nothing here can publish. Publishing happens only on the approval page, where " +
        `the person ticks the clips and places and enters their own PIN. ${NEVER_THE_PIN} The same tools are an MCP ` +
        `server at ${base(env)}/mcp.`,
    },
    servers: [{ url: base(env) }],
    security: [{ museKey: [] }],
    tags: [
      { name: "read", description: "Reads only. Changes nothing." },
      { name: "write", description: "Changes an offer, as the approval page's own buttons do. Never publishes." },
    ],
    components: {
      securitySchemes: {
        museKey: {
          type: "http",
          scheme: "bearer",
          description: "The Muse key from Clips Kitty Settings (it starts with ckm_). Only in the Authorization header, never in the address.",
        },
      },
    },
    paths,
  };
}

/** The sentence Settings shows, for the person to say to Muse. */
export function connectSentence(env) {
  return `Create a custom connector called Clips Kitty: a remote MCP server at ${base(env)}/mcp that uses the header Authorization: Bearer <your key>`;
}

function guide(env) {
  const text = [
    "Clips Kitty for Muse",
    "",
    "Your Muse agent can say which highlights are ready, give you the approval link, say what got posted, and put an offer aside for later or cancel it. It can't post anything: you approve on the approval page, with your own PIN. Clips Kitty sends reminders only to a WhatsApp number. Without one, ask Muse for the link when you are ready.",
    "",
    "Set it up",
    "1. In Clips Kitty, open Settings, Notifications & Remote Approval, and press Make a Muse key.",
    `2. In Muse chat, say: ${connectSentence(env)}`,
    "3. When Muse asks for the key, paste it into its credential prompt, not into the chat.",
    "",
    `If Muse asks for API details instead, give it ${base(env)}/muse/openapi.json`,
    "",
    "Never give your PIN to Muse or to any assistant. Enter it yourself on the approval page.",
    "To stop Muse, press Revoke in the same place in Clips Kitty.",
    "",
  ].join("\n");
  return new Response(text, { headers: { "Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff" } });
}
