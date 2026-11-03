// The Muse connector (lib/muse.js) under Node: the relay's real routes, D1 on
// node:sqlite, Meta faked. What Muse can read and do, what it can never do
// (approve or publish), and how a Muse key is kept apart from the PC's.

import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { afterEach, beforeEach, describe, it } from "node:test";

import worker, { cron } from "../worker.js";
import { canonical } from "../lib/crypto.js";
import { FakeMeta, inboundText, makeCtx, makeEnv, signed, webhookRequest } from "./harness.mjs";

const BASE = "https://relay.test";
const DEVICE = "dev_0123456789abcdef";
const SECRET = "device-secret-for-tests-0123456789abcdef";
const PHONE = "15551234567";
const PIN = "4826";
const STATELESS = "2026-07-28";

let env;
let fake;
let realFetch;

async function call(req) {
  const ctx = makeCtx();
  const res = await worker.fetch(req, env, ctx);
  await ctx.settle();
  return res;
}

async function pc(method, path, body) {
  const res = await call(signed(BASE, DEVICE, SECRET, method, path, body));
  return { status: res.status, data: await res.json() };
}

async function enroll() {
  const res = await call(new Request(`${BASE}/v1/devices`, {
    method: "POST",
    headers: { Authorization: `Bearer ${env.ENROLL_KEY}`, "Content-Type": "application/json" },
    body: JSON.stringify({ device_id: DEVICE, device_secret: SECRET, name: "Test PC" }),
  }));
  assert.equal(res.status, 200);
}

async function pairPhone() {
  const start = await pc("POST", "/v1/pair/start", {});
  const res = await call(webhookRequest(BASE, env.META_APP_SECRET, inboundText(PHONE, `LINK ${start.data.code}`)));
  assert.equal(res.status, 200);
  assert.equal((await pc("POST", "/v1/pair/confirm", { confirm: true })).data.pairing.state, "confirmed");
}

async function connectAll() {
  const { data } = await pc("POST", "/v1/accounts/connect", { platform: "facebook" });
  const state = new URL(data.url).searchParams.get("state");
  const res = await call(new Request(`${BASE}/oauth/facebook/callback?state=${state}&code=abc`));
  assert.equal(res.status, 200);
}

async function setUp({ whatsapp = true } = {}) {
  if (whatsapp) await pairPhone();
  await pc("POST", "/v1/pin", { pin: PIN });
  await pc("POST", "/v1/preferences", {
    notify_on_finish: true, notify_on_fail: true, notify_on_publish: true, remote_approvals: true,
    whatsapp_enabled: whatsapp, preferred: ["instagram"], remind_minutes: 120,
  });
  await connectAll();
}

function offer(overrides = {}) {
  return {
    kind: "ready",
    event_key: "ready:job:7:1",
    review_ref: 3,
    fingerprint: "f".repeat(32),
    expires_at: new Date(Date.now() + 24 * 3600 * 1000).toISOString(),
    title: "Some Team vs Other Team",
    summary_text: "3 clips: 2 dunks and 1 three",
    video_kind: "basketball",
    muse_line: "",
    remote_approvals: true,
    preferred: ["instagram"],
    remind_minutes: 120,
    clips: [
      { clip_id: 11, title: "Poster dunk in the third", label: "Poster dunk", category: "dunks", duration: 14, texts: { instagram: "Poster! #basketball", facebook: "Poster!" }, thumb: Buffer.from("jpegbytes").toString("base64") },
      { clip_id: 12, title: "Corner three", label: "Corner three", category: "threes", duration: 11, texts: { instagram: "Cash", facebook: "Cash" } },
      { clip_id: 13, title: "Alley-oop", label: "Alley-oop", category: "dunks", duration: 12, texts: { instagram: "Oop", facebook: "Oop" } },
    ],
    ...overrides,
  };
}

function tokenFrom(message) {
  return message.template.components.find((c) => c.sub_type === "url").parameters[0].text;
}

async function page(token) {
  const res = await call(new Request(`${BASE}/a/${token}`));
  return { status: res.status, text: await res.text() };
}

function csrfOf(html) {
  return html.match(/name="csrf" value="([0-9a-f]{64})"/)[1];
}

/** The ticked boxes of an approval page, as its form would send them. */
function ticks(html) {
  return [...html.matchAll(/<input type="checkbox" name="(clip|platform|confirm)" value="([^"]*)" checked>/g)].map((m) => [m[1], m[2]]);
}

async function submit(token, fields) {
  const form = new URLSearchParams();
  for (const [k, v] of fields) form.append(k, v);
  const res = await call(new Request(`${BASE}/a/${token}`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded", Origin: BASE },
    body: form.toString(),
  }));
  return { status: res.status, text: res.status === 303 ? "" : await res.text() };
}

async function commands() {
  return (await pc("GET", "/v1/commands")).data.commands;
}

async function makeKey() {
  const res = await pc("POST", "/v1/muse/token", {});
  assert.equal(res.status, 200);
  assert.match(res.data.key, /^ckm_[A-Za-z0-9_-]{43}$/);
  return res.data.key;
}

/** A POST to /mcp, as Muse's servers would send it. */
function mcpRequest(message, { key, headers = {} } = {}) {
  return new Request(`${BASE}/mcp`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "application/json, text/event-stream",
      ...(key ? { Authorization: `Bearer ${key}` } : {}),
      ...headers,
    },
    body: typeof message === "string" ? message : JSON.stringify(message),
  });
}

async function rpc(key, method, params = {}, headers = {}) {
  const res = await call(mcpRequest({ jsonrpc: "2.0", id: 7, method, params }, { key, headers }));
  return { status: res.status, headers: res.headers, body: res.status === 202 ? null : await res.json() };
}

async function tool(key, name, args = {}) {
  const r = await rpc(key, "tools/call", { name, arguments: args });
  assert.equal(r.status, 200);
  return r.body.result;
}

/** A stateless (2026-07-28) request: its version in _meta, and the headers that must agree. */
async function modern(key, method, params = {}, headers = null) {
  const name = method === "tools/call" ? params.name : undefined;
  const given = headers ?? { "MCP-Protocol-Version": STATELESS, "Mcp-Method": method, ...(name ? { "Mcp-Name": name } : {}) };
  return rpc(key, method, { ...params, _meta: { "io.modelcontextprotocol/protocolVersion": STATELESS } }, given);
}

async function rest(key, method, path, body) {
  const res = await call(new Request(`${BASE}${path}`, {
    method,
    headers: { ...(key ? { Authorization: `Bearer ${key}` } : {}), "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  }));
  return { status: res.status, headers: res.headers, data: await res.json() };
}

function audits(action) {
  return env.DB.raw.prepare("SELECT * FROM audit WHERE action = ?").all(action);
}

beforeEach(async () => {
  env = makeEnv();
  realFetch = globalThis.fetch;
  fake = new FakeMeta(realFetch);
  globalThis.fetch = fake.fetch;
  await enroll();
});

afterEach(() => {
  globalThis.fetch = realFetch;
});

describe("keys", () => {
  it("refuses /mcp without a key, with a wrong key and with a revoked key", async () => {
    let r = await rpc("", "tools/list");
    assert.equal(r.status, 401);
    assert.match(r.headers.get("WWW-Authenticate"), /^Bearer /);
    r = await rpc(`ckm_${"x".repeat(43)}`, "tools/list");
    assert.equal(r.status, 401);
    assert.match(r.headers.get("WWW-Authenticate"), /invalid_token/);
    const key = await makeKey();
    assert.equal((await rpc(key, "tools/list")).status, 200);
    const lower = await call(mcpRequest({ jsonrpc: "2.0", id: 1, method: "ping" }, { headers: { Authorization: `bearer ${key}` } }));
    assert.equal(lower.status, 200);
    await pc("POST", "/v1/muse/revoke", {});
    assert.equal((await rpc(key, "tools/list")).status, 401);
    // Never in the address: a key in the query string is not a key.
    const res = await call(new Request(`${BASE}/muse/v1/status?key=${key}&access_token=${key}`));
    assert.equal(res.status, 401);
  });

  it("keeps Muse keys and the PC's signature apart", async () => {
    const key = await makeKey();
    // Refused by the key check itself, before anything looks for the PC's
    // signature (which a key-only request would fail anyway), and on
    // /v1/devices before any enrolment.
    for (const [method, path] of [["GET", "/v1/status"], ["GET", "/v1/commands"], ["POST", "/v1/muse/token"], ["POST", "/v1/devices"]]) {
      const res = await call(new Request(`${BASE}${path}`, { method, headers: { Authorization: `Bearer ${key}` }, body: method === "POST" ? "{}" : undefined }));
      assert.equal(res.status, 401, path);
      assert.deepEqual(await res.json(), { code: "forbidden", error: "A Muse key can't call this PC's routes." }, path);
    }
    const asPc = await call(signed(BASE, DEVICE, SECRET, "POST", "/mcp", { jsonrpc: "2.0", id: 1, method: "tools/list" }));
    assert.equal(asPc.status, 401);
    const restAsPc = await call(signed(BASE, DEVICE, SECRET, "GET", "/muse/v1/status"));
    assert.equal(restAsPc.status, 401);
  });

  it("replaces an older key, reports the key without showing it, and disconnecting stops it", async () => {
    const first = await makeKey();
    const second = await makeKey();
    assert.notEqual(first, second);
    assert.equal((await rpc(first, "tools/list")).status, 401);
    assert.equal((await rpc(second, "tools/list")).status, 200);
    const status = (await pc("GET", "/v1/status")).data;
    assert.equal(status.muse.enabled, true);
    assert.ok(status.muse.created_at > 0 && status.muse.last_used >= status.muse.created_at);
    assert.doesNotMatch(JSON.stringify(status), /ckm_/);
    const stored = JSON.stringify(env.DB.raw.prepare("SELECT * FROM connector_tokens").all());
    assert.ok(!stored.includes(second.slice(4)) && !stored.includes(first.slice(4)));
    await pc("POST", "/v1/disconnect", {});
    assert.equal((await rpc(second, "tools/list")).status, 401);
    assert.equal((await pc("GET", "/v1/status")).data.muse.enabled, false);
  });

  it("limits a key to about 60 calls a minute", async () => {
    const key = await makeKey();
    assert.equal((await rpc(key, "ping")).status, 200);
    env.DB.raw.prepare("UPDATE connector_tokens SET window_count = 60").run();
    const r = await rpc(key, "ping");
    assert.equal(r.status, 429);
    assert.ok(Number(r.headers.get("Retry-After")) >= 1);
    env.DB.raw.prepare("UPDATE connector_tokens SET window_start = window_start - 61").run();
    assert.equal((await rpc(key, "ping")).status, 200);
  });
});

describe("the MCP transport", () => {
  let key;
  beforeEach(async () => {
    key = await makeKey();
  });

  it("refuses a request from another website", async () => {
    let r = await rpc(key, "tools/list", {}, { Origin: "https://evil.example" });
    assert.equal(r.status, 403);
    r = await rpc(key, "tools/list", {}, { Origin: BASE });
    assert.equal(r.status, 200);
  });

  it("answers GET and DELETE with 405, and a notification with 202", async () => {
    for (const method of ["GET", "DELETE"]) {
      const res = await call(new Request(`${BASE}/mcp`, { method, headers: { Authorization: `Bearer ${key}` } }));
      assert.equal(res.status, 405, method);
      assert.equal(res.headers.get("Allow"), "POST");
    }
    const res = await call(mcpRequest({ jsonrpc: "2.0", method: "notifications/initialized" }, { key }));
    assert.equal(res.status, 202);
    assert.equal(await res.text(), "");
  });

  it("echoes the client's version in the handshake and never makes a session", async () => {
    for (const version of ["2025-06-18", "2025-03-26", "2025-11-25"]) {
      const r = await rpc(key, "initialize", { protocolVersion: version, capabilities: {}, clientInfo: { name: "muse", version: "1" } });
      assert.equal(r.status, 200);
      assert.equal(r.body.result.protocolVersion, version);
      assert.equal(r.headers.get("Mcp-Session-Id"), null);
    }
    const r = await rpc(key, "initialize", { protocolVersion: "2024-11-05" });
    assert.equal(r.body.result.protocolVersion, "2025-11-25");
    assert.deepEqual(r.body.result.capabilities, { tools: { listChanged: false } });
    assert.equal(r.body.result.serverInfo.title, "Clips Kitty");
    assert.match(r.body.result.instructions, /only on the approval page/);
    assert.match(r.body.result.instructions, /Never ask for the PIN, never type it/);
  });

  it("checks a stateless request's headers against its body", async () => {
    let r = await modern(key, "tools/call", { name: "relay_status", arguments: {} });
    assert.equal(r.status, 200);
    assert.equal(r.body.result.isError, false);
    r = await modern(key, "server/discover");
    assert.deepEqual(r.body.result.supportedVersions, [STATELESS]);
    assert.match(r.body.result.instructions, /own PIN/);

    const wrongName = { "MCP-Protocol-Version": STATELESS, "Mcp-Method": "tools/call", "Mcp-Name": "list_recent_posts" };
    r = await modern(key, "tools/call", { name: "relay_status", arguments: {} }, wrongName);
    assert.equal(r.status, 400);
    assert.equal(r.body.error.code, -32020);
    r = await modern(key, "tools/list", {}, { "MCP-Protocol-Version": STATELESS });
    assert.equal(r.status, 400);
    assert.equal(r.body.error.code, -32020);

    r = await rpc(key, "tools/list", { _meta: { "io.modelcontextprotocol/protocolVersion": "2099-01-01" } },
      { "MCP-Protocol-Version": "2099-01-01", "Mcp-Method": "tools/list" });
    assert.equal(r.status, 400);
    assert.equal(r.body.error.code, -32022);
    assert.deepEqual(r.body.error.data, { supported: [STATELESS], requested: "2099-01-01" });
  });

  it("lists the tools in a fixed order with their annotations, and none that approves", async () => {
    const { tools } = (await rpc(key, "tools/list")).body.result;
    assert.deepEqual(tools.map((t) => t.name), [
      "list_ready_highlights", "get_approval_link", "list_recent_posts", "remind_me_later", "cancel_offer", "relay_status",
    ]);
    const by = Object.fromEntries(tools.map((t) => [t.name, t]));
    for (const name of ["list_ready_highlights", "get_approval_link", "list_recent_posts", "relay_status"]) {
      assert.equal(by[name].annotations.readOnlyHint, true, name);
    }
    assert.equal(by.remind_me_later.annotations.readOnlyHint, false);
    assert.equal(by.remind_me_later.annotations.destructiveHint, false);
    assert.equal(by.cancel_offer.annotations.destructiveHint, true);
    assert.deepEqual(by.cancel_offer.inputSchema.required, ["approval_id", "confirm"]);
    assert.ok(tools.every((t) => t.annotations.openWorldHint === false && t.inputSchema.type === "object"));
    assert.doesNotMatch(JSON.stringify(tools.map((t) => [t.name, Object.keys(t.inputSchema.properties)])), /approve|publish|pin|platform/i);
    const unknown = await rpc(key, "tools/call", { name: "approve_offer", arguments: {} });
    assert.equal(unknown.body.error.code, -32602);
  });
});

describe("what Muse can read and do", () => {
  let key;
  let sent;
  let message;
  beforeEach(async () => {
    await setUp();
    key = await makeKey();
    sent = await pc("POST", "/v1/notify", offer());
    message = fake.messages.at(-1);
  });

  it("lists ready highlights with no link token and no thumbnail", async () => {
    const result = await tool(key, "list_ready_highlights");
    assert.equal(result.isError, false);
    const [o] = result.structuredContent.offers;
    assert.equal(o.approval_id, sent.data.approval_id);
    assert.equal(o.title, "Some Team vs Other Team");
    assert.equal(o.summary, "3 clips: 2 dunks and 1 three");
    assert.equal(o.kind, "basketball");
    assert.equal(o.state, "open");
    assert.equal(o.approve_from_phone, true);
    assert.deepEqual(o.clips[0], { clip_id: 11, title: "Poster dunk in the third", label: "Poster dunk", category: "dunks", duration_s: 14, already_posted: [] });
    assert.deepEqual(o.preferred, ["instagram"]);
    assert.equal(o.accounts.instagram.connected, true);
    assert.match(o.expires_at, /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$/);
    assert.ok(result.structuredContent.pc_last_seen);
    const everything = JSON.stringify(result);
    assert.ok(!everything.includes(tokenFrom(message)));
    assert.ok(!everything.includes(Buffer.from("jpegbytes").toString("base64")));
    assert.doesNotMatch(everything, /thumb|token|fingerprint/i);
    assert.match(result.content[0].text, /approve on that page with their own PIN/);

    const withCaptions = await tool(key, "list_ready_highlights", { include_captions: true, match: "some team", limit: 1 });
    assert.equal(withCaptions.structuredContent.offers[0].clips[0].captions.instagram, "Poster! #basketball");
    assert.deepEqual((await tool(key, "list_ready_highlights", { match: "derby" })).structuredContent.offers, []);
    const late = new Date(Date.now() + 3600 * 1000).toISOString();
    assert.deepEqual((await tool(key, "list_ready_highlights", { since: late })).structuredContent.offers, []);
    const bad = await tool(key, "list_ready_highlights", { limit: 50 });
    assert.equal(bad.isError, true);
    assert.match(bad.content[0].text, /from 1 to 20/);
  });

  it("shows Muse only the connected accounts, and only their captions, like the page", async () => {
    await pc("POST", "/v1/accounts/disconnect", { platform: "instagram" });
    const [o] = (await tool(key, "list_ready_highlights", { include_captions: true })).structuredContent.offers;
    assert.deepEqual(Object.keys(o.accounts), ["facebook"]);
    assert.deepEqual(o.clips[0].captions, { facebook: "Poster!" });
    assert.deepEqual(o.preferred, []);
    assert.doesNotMatch(JSON.stringify(o), /instagram/i);
    await pc("POST", "/v1/accounts/disconnect", { platform: "facebook" });
    const none = await tool(key, "relay_status");
    assert.match(none.content[0].text, /No account is connected yet\./);
    assert.deepEqual(none.structuredContent.accounts, { instagram: { connected: false, name: "" }, facebook: { connected: false, name: "" } });
  });

  it("gives the same approval link the WhatsApp message carries", async () => {
    const result = await tool(key, "get_approval_link", { approval_id: sent.data.approval_id });
    assert.equal(result.isError, false);
    assert.equal(result.structuredContent.url, `${BASE}/a/${tokenFrom(message)}`);
    assert.match(result.content[0].text, /enters their own PIN there\. Never ask for the PIN/);
    const shared = audits("link_shared");
    assert.equal(shared.length, 1);
    assert.equal(shared[0].actor, "muse");
    assert.equal(shared[0].approval_id, sent.data.approval_id);
    // The one open offer, with nothing to pick by.
    assert.equal((await tool(key, "get_approval_link")).structuredContent.url, result.structuredContent.url);
    const unknown = await tool(key, "get_approval_link", { approval_id: `apr_${"0".repeat(24)}` });
    assert.equal(unknown.isError, true);
    assert.equal((await tool(key, "get_approval_link", { approval_id: "../../v1/status" })).isError, true);
  });

  it("picks the offer that matches, and gives the choices when several or none do", async () => {
    const second = await pc("POST", "/v1/notify", offer({ event_key: "ready:job:8:1", review_ref: 4, title: "Other Team vs Away Team" }));
    const both = await tool(key, "get_approval_link");
    assert.equal(both.structuredContent.url, "");
    assert.deepEqual(both.structuredContent.choices.map((c) => c.approval_id).sort(), [sent.data.approval_id, second.data.approval_id].sort());
    assert.match(both.content[0].text, /Ask the person which one/);
    const picked = await tool(key, "get_approval_link", { match: "away team" });
    assert.equal(picked.structuredContent.url, `${BASE}/a/${tokenFrom(fake.messages.at(-1))}`);
    const none = await tool(key, "get_approval_link", { match: "derby" });
    assert.equal(none.structuredContent.url, "");
    assert.equal(none.structuredContent.choices.length, 2);
    assert.equal(audits("link_shared").length, 1);
  });

  it("defers with one command marked as Muse's, inside the signature", async () => {
    const result = await tool(key, "remind_me_later", { approval_id: sent.data.approval_id, minutes: 60 });
    assert.equal(result.isError, false);
    assert.equal(result.structuredContent.state, "deferred");
    assert.equal(result.structuredContent.whatsapp_reminder, true);
    const cmds = await commands();
    assert.equal(cmds.length, 1);
    const [defer] = cmds;
    assert.equal(defer.kind, "defer");
    assert.equal(defer.via, "muse");
    assert.equal(defer.remind_at, result.structuredContent.remind_at);
    const { sig, ...rest } = defer;
    assert.equal(sig, createHmac("sha256", SECRET).update(canonical(rest)).digest("hex"));
    assert.equal(audits("deferred")[0].actor, "muse");
    assert.match((await page(tokenFrom(message))).text, /Reminder set\. WhatsApp sends you this link again in about 1 h\. You can still decide now\./);
    for (const minutes of [5, 2000, "60"]) {
      assert.equal((await tool(key, "remind_me_later", { approval_id: sent.data.approval_id, minutes })).isError, true);
    }
    // Still the one defer, waiting for the PC (opening the page added a delivery note).
    assert.deepEqual((await commands()).map((c) => c.kind).sort(), ["defer", "delivery"]);
  });

  it("cancels only with confirm, with one command marked as Muse's", async () => {
    const unconfirmed = await tool(key, "cancel_offer", { approval_id: sent.data.approval_id });
    assert.equal(unconfirmed.isError, true);
    assert.match(unconfirmed.content[0].text, /Ask the person first/);
    assert.equal((await tool(key, "cancel_offer", { approval_id: sent.data.approval_id, confirm: "yes" })).isError, true);
    assert.equal((await commands()).length, 0);
    const result = await tool(key, "cancel_offer", { approval_id: sent.data.approval_id, confirm: true });
    assert.equal(result.isError, false);
    const cmds = await commands();
    assert.deepEqual(cmds.map((c) => [c.kind, c.via]), [["cancel", "muse"]]);
    assert.equal(audits("cancelled")[0].actor, "muse");
    assert.match((await page(tokenFrom(message))).text, /Cancelled\. Nothing was posted/);
    const again = await tool(key, "cancel_offer", { approval_id: sent.data.approval_id, confirm: true });
    assert.equal(again.isError, true);
    assert.match(again.content[0].text, /already cancelled/);
  });

  it("reads what got posted after a PIN approval, with only the links the platforms returned", async () => {
    const token = tokenFrom(message);
    const csrf = csrfOf((await page(token)).text);
    await submit(token, [["csrf", csrf], ["action", "approve"], ["clip", "11"], ["clip", "12"], ["platform", "instagram"], ["platform", "facebook"], ["confirm", "yes"], ["pin", PIN]]);
    let posts = await tool(key, "list_recent_posts");
    assert.equal(posts.structuredContent.offers[0].status, "Approved. Waiting for the PC to pick it up.");
    const approve = (await commands()).find((c) => c.kind === "approve");
    await pc("POST", `/v1/commands/${approve.id}/ack`, { outcome: "accepted", reason: "2 clips queued" });
    assert.equal((await tool(key, "list_recent_posts")).structuredContent.offers[0].status, "Approved. The PC is publishing it.");
    await pc("POST", "/v1/notify", {
      kind: "result", event_key: "result:review:3:0", approval_id: sent.data.approval_id, text: "2 of 3 posts published (1 failed)",
      results: { clips: [
        { clip_id: 11, title: "Poster dunk in the third", posts: [
          { platform: "instagram", state: "published", url: "https://www.instagram.com/reel/ABC/" },
          { platform: "facebook", state: "published", url: "https://evil.example/phish" },
        ] },
        { clip_id: 12, title: "Corner three", posts: [
          { platform: "facebook", state: "failed", url: "https://www.facebook.com/reel/999/", error: "Refused." },
        ] },
      ] },
    });
    posts = await tool(key, "list_recent_posts", { days: 1 });
    const [o] = posts.structuredContent.offers;
    assert.equal(o.state, "done");
    const urls = o.clips.map((c) => c.posts.map((p) => p.url));
    assert.deepEqual(urls, [["https://www.instagram.com/reel/ABC/", ""], [""]]);
    // Only a published post's link, and only on Meta's own hosts.
    assert.doesNotMatch(JSON.stringify(posts), /evil\.example|reel\/999/);
    assert.match(posts.content[0].text, /instagram: https:\/\/www\.instagram\.com\/reel\/ABC\//);
    assert.equal((await tool(key, "list_recent_posts", { days: 8 })).isError, true);
  });

  it("lets a key see and change only its own PC's offers", async () => {
    const other = { id: "dev_fedcba9876543210", secret: "another-device-secret-0123456789abcdefgh" };
    const res = await call(new Request(`${BASE}/v1/devices`, {
      method: "POST",
      headers: { Authorization: `Bearer ${env.ENROLL_KEY}`, "Content-Type": "application/json" },
      body: JSON.stringify({ device_id: other.id, device_secret: other.secret }),
    }));
    assert.equal(res.status, 200);
    const minted = await call(signed(BASE, other.id, other.secret, "POST", "/v1/muse/token", {}));
    const otherKey = (await minted.json()).key;
    assert.deepEqual((await tool(otherKey, "list_ready_highlights")).structuredContent.offers, []);
    assert.equal((await tool(otherKey, "get_approval_link")).structuredContent.url, "");
    for (const name of ["get_approval_link", "remind_me_later", "cancel_offer"]) {
      const r = await tool(otherKey, name, { approval_id: sent.data.approval_id, confirm: true });
      assert.equal(r.isError, true, name);
    }
    assert.equal((await tool(otherKey, "list_recent_posts")).structuredContent.offers.length, 0);
    assert.deepEqual(await commands(), []);
    assert.equal((await tool(key, "list_ready_highlights")).structuredContent.offers.length, 1);
  });

  it("never makes an approve command, whatever Muse sends", async () => {
    const id = sent.data.approval_id;
    const everything = {
      approval_id: id, match: "some team", since: "2020-01-01T00:00:00Z", limit: 5, include_captions: true, days: 7, minutes: 60,
      confirm: true, pin: PIN, approve: true, action: "approve", clips: [11, 12], platforms: ["instagram", "facebook"],
    };
    const { tools } = (await rpc(key, "tools/list")).body.result;
    for (const t of tools) {
      await tool(key, t.name, everything);
      await tool(key, t.name, {});
    }
    for (const [method, path] of [["GET", `/muse/v1/offers/${id}/link`], ["POST", `/muse/v1/offers/${id}/remind`], ["POST", `/muse/v1/offers/${id}/cancel`],
      ["POST", `/muse/v1/offers/${id}/approve`], ["POST", "/muse/v1/approve"]]) {
      await rest(key, method, path, method === "POST" ? everything : undefined);
    }
    const cmds = await commands();
    assert.deepEqual(cmds.map((c) => c.kind).sort(), ["cancel", "defer"]);
    assert.ok(cmds.every((c) => c.via === "muse"));
    const row = env.DB.raw.prepare("SELECT state, decision FROM approvals WHERE id = ?").get(id);
    assert.equal(row.state, "cancelled");
    assert.equal(row.decision, "{}");
    assert.equal(fake.posts.length, 0);
    assert.equal(env.DB.raw.prepare("SELECT COUNT(*) AS n FROM commands WHERE kind = 'approve'").get().n, 0);
  });

  it("mirrors the tools over plain HTTPS", async () => {
    const id = sent.data.approval_id;
    assert.equal((await rest("", "GET", "/muse/v1/offers")).status, 401);
    let r = await rest(key, "GET", "/muse/v1/offers?match=some%20team&limit=5&include_captions=true");
    assert.equal(r.status, 200);
    assert.equal(r.data.offers[0].clips[0].captions.facebook, "Poster!");
    r = await rest(key, "GET", `/muse/v1/offers/${id}/link`);
    assert.equal(r.data.url, `${BASE}/a/${tokenFrom(message)}`);
    assert.equal((await rest(key, "GET", `/muse/v1/offers/apr_${"0".repeat(24)}/link`)).status, 404);
    assert.equal((await rest(key, "GET", "/muse/v1/offers?limit=abc")).status, 400);
    assert.equal((await rest(key, "GET", "/muse/v1/posts?days=8")).status, 400);
    assert.equal((await rest(key, "POST", "/muse/v1/offers")).status, 405);
    r = await rest(key, "POST", `/muse/v1/offers/${id}/remind`, { minutes: 30 });
    assert.equal(r.data.state, "deferred");
    r = await rest(key, "POST", `/muse/v1/offers/${id}/cancel`, {});
    assert.equal(r.status, 400);
    assert.equal(r.data.code, "bad_request");
    assert.equal((await rest(key, "POST", `/muse/v1/offers/${id}/cancel`, { confirm: true })).data.state, "cancelled");
    r = await rest(key, "POST", `/muse/v1/offers/${id}/remind`, {});
    assert.equal(r.status, 409);
    assert.match(r.data.error, /already cancelled/);
    r = await rest(key, "GET", "/muse/v1/status");
    assert.equal(r.data.pin_set, true);
    assert.deepEqual(r.data.whatsapp, { state: "confirmed", tail: "4567", messages_on: true });
    assert.equal(r.data.open_offers, 0);
    assert.deepEqual((await commands()).map((c) => `${c.kind} ${c.via}`).sort(), ["cancel muse", "defer muse"]);
  });
});

describe("offers with Muse alone", () => {
  it("keeps the offer for Muse when no WhatsApp number is paired, and sends nothing", async () => {
    await setUp({ whatsapp: false });
    // No number and no Muse key: nothing can carry the offer, as before.
    let res = await pc("POST", "/v1/notify", offer());
    assert.equal(res.status, 409);
    assert.equal(res.data.code, "no_channel");
    const key = await makeKey();
    res = await pc("POST", "/v1/notify", offer({ event_key: "ready:job:7:2" }));
    assert.equal(res.status, 200);
    assert.equal(res.data.channel, "muse");
    assert.match(res.data.approval_id, /^apr_[0-9a-f]{24}$/);
    assert.equal(fake.messages.length, 0);
    const [o] = (await tool(key, "list_ready_highlights")).structuredContent.offers;
    assert.equal(o.approval_id, res.data.approval_id);
    const url = (await tool(key, "get_approval_link")).structuredContent.url;
    assert.ok(url.startsWith(`${BASE}/a/`));
    // Opening the page tells the PC the offer was seen.
    const token = url.split("/a/")[1];
    assert.match((await page(token)).text, /Enter your PIN yourself\. Never give it to an assistant\./);
    assert.deepEqual((await commands()).map((c) => [c.kind, c.status]), [["delivery", "opened"]]);
    // A reminder is only recorded: the cron has no number to send it to.
    const later = await tool(key, "remind_me_later", { approval_id: o.approval_id, minutes: 15 });
    assert.equal(later.structuredContent.whatsapp_reminder, false);
    assert.match(later.content[0].text, /remind the person yourself/);
    env.DB.raw.prepare("UPDATE approvals SET remind_at = ?").run(Math.floor(Date.now() / 1000) - 5);
    await cron(env);
    assert.equal(fake.messages.length, 0);
    assert.equal(env.DB.raw.prepare("SELECT state FROM approvals WHERE id = ?").get(o.approval_id).state, "deferred");
  });

  it("keeps offers for Muse when the PC has WhatsApp off, even with a number paired", async () => {
    await setUp();
    await pc("POST", "/v1/preferences", { notify_on_finish: true, notify_on_publish: true, remote_approvals: true, whatsapp_enabled: false });
    const key = await makeKey();
    const before = fake.messages.length;
    const res = await pc("POST", "/v1/notify", offer());
    assert.equal(res.data.channel, "muse");
    assert.equal((await tool(key, "remind_me_later", { approval_id: res.data.approval_id })).structuredContent.whatsapp_reminder, false);
    // The page says the same: the paired number gets nothing while WhatsApp is off.
    const url = (await tool(key, "get_approval_link", { approval_id: res.data.approval_id })).structuredContent.url;
    const view = (await page(url.split("/a/")[1])).text;
    assert.match(view, /<div class="card notice">Put aside for later\. Clips Kitty will not remind you\. Your Muse agent can give you this link again when you ask it\. You can still decide now\.<\/div>/);
    assert.doesNotMatch(view, /Reminder set|WhatsApp sends/);
    env.DB.raw.prepare("UPDATE approvals SET remind_at = ?").run(Math.floor(Date.now() / 1000) - 5);
    await cron(env);
    assert.equal(fake.messages.length, before);
  });

  it("says on the page, before and after Remind me later, that only Muse can bring the offer back", async () => {
    await setUp({ whatsapp: false });
    const key = await makeKey();
    const sent = await pc("POST", "/v1/notify", offer());
    const url = (await tool(key, "get_approval_link", { approval_id: sent.data.approval_id })).structuredContent.url;
    const token = url.split("/a/")[1];
    let view = (await page(token)).text;
    // Before the tap, under the button: what it does here.
    assert.match(view, /value="defer">Remind me later<\/button>\s*<p class="small muted">Remind me later only puts this aside\. Clips Kitty will not remind you\. Your Muse agent can give you this link again when you ask it\.<\/p>/);
    assert.equal((await submit(token, [["csrf", csrfOf(view)], ["action", "defer"]])).status, 303);
    // After it, at the top: what happened and what happens next, with no time and no promise.
    view = (await page(token)).text;
    assert.match(view, /<div class="card notice">Put aside for later\. Clips Kitty will not remind you\. Your Muse agent can give you this link again when you ask it\. You can still decide now\.<\/div>/);
    assert.doesNotMatch(view, /Reminder set|WhatsApp sends|only puts this aside|asked to be reminded/);
    assert.match(view, /Approve and publish/);
    const cmds = await commands();
    assert.deepEqual(cmds.map((c) => c.kind).sort(), ["defer", "delivery"]);
    assert.equal(cmds.find((c) => c.kind === "defer").via, undefined); // the page's, not Muse's
    const [o] = (await tool(key, "list_ready_highlights")).structuredContent.offers;
    assert.equal(o.state, "deferred");
    assert.ok(o.remind_at);
    // The wait passes: nothing is sent, and the page still says the same.
    env.DB.raw.prepare("UPDATE approvals SET remind_at = ?").run(Math.floor(Date.now() / 1000) - 5);
    await cron(env);
    assert.equal(fake.messages.length, 0);
    assert.match((await page(token)).text, /Put aside for later\. Clips Kitty will not remind you\./);
    // With the Muse key revoked too, nothing can bring it back.
    await pc("POST", "/v1/muse/revoke", {});
    assert.match((await page(token)).text, /Put aside for later\. Nothing will remind you, so keep this link\. You can still decide now\./);
  });

  it("stores results for Muse without a message when no number can take one", async () => {
    await setUp({ whatsapp: false });
    const key = await makeKey();
    const sent = await pc("POST", "/v1/notify", offer());
    const url = (await tool(key, "get_approval_link", { approval_id: sent.data.approval_id })).structuredContent.url;
    const token = url.split("/a/")[1];
    const csrf = csrfOf((await page(token)).text);
    const r = await submit(token, [["csrf", csrf], ["action", "approve"], ["clip", "12"], ["platform", "instagram"], ["confirm", "yes"], ["pin", PIN]]);
    assert.equal(r.status, 303);
    const approve = (await commands()).find((c) => c.kind === "approve");
    assert.equal(approve.via, undefined); // the page's approval, not Muse's
    await pc("POST", `/v1/commands/${approve.id}/ack`, { outcome: "accepted" });
    const res = await pc("POST", "/v1/notify", {
      kind: "result", event_key: "result:review:3:0", approval_id: sent.data.approval_id,
      results: { clips: [{ clip_id: 12, title: "Corner three", posts: [{ platform: "instagram", state: "published", url: "https://www.instagram.com/reel/XYZ/" }] }] },
    });
    assert.equal(res.status, 200);
    assert.deepEqual(res.data, { channel: "", skipped: true });
    assert.equal(fake.messages.length, 0);
    const posts = (await tool(key, "list_recent_posts")).structuredContent.offers;
    assert.equal(posts[0].clips[0].posts[0].url, "https://www.instagram.com/reel/XYZ/");
  });
});

describe("the PIN", () => {
  it("blocks phone approval after three lockouts in a day, until a new PIN is set on the PC", async () => {
    await setUp();
    await pc("POST", "/v1/notify", offer());
    const token = tokenFrom(fake.messages.at(-1));
    const csrf = csrfOf((await page(token)).text);
    const fields = [["csrf", csrf], ["action", "approve"], ["clip", "11"], ["platform", "instagram"], ["confirm", "yes"]];
    let r;
    for (let round = 1; round <= 3; round++) {
      for (let i = 0; i < 5; i++) r = await submit(token, [...fields, ["pin", "9999"]]);
      if (round < 3) {
        assert.match(r.text, /That PIN is not right/);
        assert.match((await submit(token, [...fields, ["pin", PIN]])).text, /Too many wrong PINs/);
        env.DB.raw.prepare("UPDATE devices SET pin_locked_until = 0").run(); // the quarter hour passes
      }
    }
    assert.equal(r.status, 403);
    assert.match(r.text, /blocked after too many wrong PINs/);
    env.DB.raw.prepare("UPDATE devices SET pin_locked_until = 0").run();
    r = await submit(token, [...fields, ["pin", PIN]]);
    assert.equal(r.status, 403);
    assert.match(r.text, /Set a new PIN in Clips Kitty on your PC/);
    const view = (await page(token)).text;
    assert.match(view, /blocked after too many wrong PINs/);
    assert.doesNotMatch(view, /name="pin"|Approve and publish/);
    assert.match(view, /<button name="action" value="defer">Remind me later<\/button>/);
    assert.match(view, /Remind me later and Cancel still work\./);
    // It does, and the page doesn't offer a decision it can't take.
    assert.equal((await submit(token, [["csrf", csrf], ["action", "defer"]])).status, 303);
    assert.match((await page(token)).text, /<div class="card notice">Reminder set\. WhatsApp sends you this link again in about 2 h\.<\/div>/);
    assert.equal((await commands()).filter((c) => c.kind === "approve").length, 0);

    await pc("POST", "/v1/pin", { pin: "5937" });
    assert.match((await page(token)).text, /name="pin"/);
    r = await submit(token, [...fields, ["pin", "5937"]]);
    assert.equal(r.status, 303);
    assert.equal((await commands()).filter((c) => c.kind === "approve").length, 1);
  });

  it("gives wrong PINs sent all at once the same five tries as one at a time", async () => {
    await setUp();
    await pc("POST", "/v1/notify", offer());
    const token = tokenFrom(fake.messages.at(-1));
    const csrf = csrfOf((await page(token)).text);
    const fields = [["csrf", csrf], ["action", "approve"], ["clip", "11"], ["platform", "instagram"], ["confirm", "yes"]];
    // Forty at the same moment. Counted after the check, each read the count
    // before any of them wrote it: all forty were checked and nothing locked.
    const answers = await Promise.all(Array.from({ length: 40 }, (_, i) => submit(token, [...fields, ["pin", String(5000 + i)]])));
    assert.equal(audits("pin_wrong").length, 5);
    assert.equal(answers.filter((a) => /That PIN is not right/.test(a.text)).length, 5);
    const refused = answers.filter((a) => a.status === 429);
    assert.equal(refused.length, 35);
    assert.ok(refused.every((a) => /Too many wrong PINs\. Try again in 15 minutes/.test(a.text)));
    assert.equal(env.DB.raw.prepare("SELECT COUNT(*) AS n FROM pin_lockouts").get().n, 1);
    // Locked, so the right PIN waits its quarter hour too.
    assert.equal((await submit(token, [...fields, ["pin", PIN]])).status, 429);
    assert.equal((await commands()).filter((c) => c.kind === "approve").length, 0);
  });

  it("starts the lock when five counted tries never finished, so the count can't stick at five", async () => {
    await setUp();
    await pc("POST", "/v1/notify", offer());
    const token = tokenFrom(fake.messages.at(-1));
    const csrf = csrfOf((await page(token)).text);
    const fields = [["csrf", csrf], ["action", "approve"], ["clip", "11"], ["platform", "instagram"], ["confirm", "yes"]];
    env.DB.raw.prepare("UPDATE devices SET pin_fails = 5").run(); // counted, then cut off before their PIN was checked
    let r = await submit(token, [...fields, ["pin", PIN]]);
    assert.equal(r.status, 429);
    assert.match(r.text, /Try again in 15 minutes/);
    assert.deepEqual(ticks(r.text), fields.slice(2)); // the lock this try started keeps its ticks
    assert.equal(env.DB.raw.prepare("SELECT COUNT(*) AS n FROM pin_lockouts").get().n, 1);
    env.DB.raw.prepare("UPDATE devices SET pin_locked_until = 0").run(); // the quarter hour passes
    r = await submit(token, [...fields, ["pin", PIN]]);
    assert.equal(r.status, 303);
    assert.equal((await commands()).filter((c) => c.kind === "approve").length, 1);
  });

  it("tells Muse when phone approval is blocked, instead of handing out the link as if it could approve", async () => {
    await setUp();
    const key = await makeKey();
    const sent = await pc("POST", "/v1/notify", offer());
    const t = Math.floor(Date.now() / 1000);
    for (const at of [t - 3600, t - 1800, t - 60]) {
      env.DB.raw.prepare("INSERT INTO pin_lockouts (device_id, at) VALUES (?, ?)").run(DEVICE, at);
    }
    const link = await tool(key, "get_approval_link", { approval_id: sent.data.approval_id });
    assert.equal(link.structuredContent.phone_approval_blocked, true);
    assert.match(link.content[0].text, /blocked after too many wrong PINs\. The person sets a new PIN in Clips Kitty on their PC/);
    assert.doesNotMatch(link.content[0].text, /enters their own PIN there/);
    const ready = await tool(key, "list_ready_highlights");
    assert.equal(ready.structuredContent.phone_approval_blocked, true);
    assert.match(ready.content[0].text, /blocked after too many wrong PINs/);
    assert.doesNotMatch(ready.content[0].text, /They approve on that page/);
    const status = await tool(key, "relay_status");
    assert.equal(status.structuredContent.phone_approval_blocked, true);
    assert.match(status.content[0].text, /blocked after too many wrong PINs/);

    await pc("POST", "/v1/pin", { pin: "5937" });
    const after = await tool(key, "get_approval_link", { approval_id: sent.data.approval_id });
    assert.equal(after.structuredContent.phone_approval_blocked, false);
    assert.match(after.content[0].text, /enters their own PIN there/);
    assert.match((await tool(key, "relay_status")).content[0].text, /An approval PIN is set\./);
  });

  it("tells Muse when approving from the phone is off, rather than pointing to a PIN", async () => {
    await setUp();
    const key = await makeKey();
    await pc("POST", "/v1/notify", offer({ remote_approvals: false }));
    const ready = await tool(key, "list_ready_highlights");
    assert.equal(ready.structuredContent.offers[0].approve_from_phone, false);
    assert.match(ready.content[0].text, /switched off on their PC, so they publish these from Clips Kitty on the PC/);
    assert.doesNotMatch(ready.content[0].text, /own PIN/);
  });

  it("counts only lockouts within a day of each other", async () => {
    await setUp();
    await pc("POST", "/v1/notify", offer());
    const token = tokenFrom(fake.messages.at(-1));
    const t = Math.floor(Date.now() / 1000);
    for (const at of [t - 50 * 3600, t - 25 * 3600, t]) {
      env.DB.raw.prepare("INSERT INTO pin_lockouts (device_id, at) VALUES (?, ?)").run(DEVICE, at);
    }
    const view = (await page(token)).text;
    assert.match(view, /name="pin"/);
    assert.match(view, /Enter your PIN yourself\. Never give it to an assistant\./);
  });
});

describe("for Muse to read", () => {
  it("describes the REST routes in OpenAPI 3.1 and explains how to connect, without a key", async () => {
    let res = await call(new Request(`${BASE}/muse/openapi.json`));
    assert.equal(res.status, 200);
    const doc = await res.json();
    assert.equal(doc.openapi, "3.1.0");
    assert.equal(doc.servers[0].url, BASE);
    assert.match(doc.info.description, /Never ask for the PIN or type it/);
    assert.equal(doc.components.securitySchemes.museKey.scheme, "bearer");
    assert.deepEqual(Object.keys(doc.paths).sort(), [
      "/muse/v1/offers", "/muse/v1/offers/{approval_id}/cancel", "/muse/v1/offers/{approval_id}/link",
      "/muse/v1/offers/{approval_id}/remind", "/muse/v1/posts", "/muse/v1/status",
    ]);
    assert.deepEqual(doc.paths["/muse/v1/offers"].get.tags, ["read"]);
    assert.equal(doc.paths["/muse/v1/offers"].get["x-read-only"], true);
    assert.deepEqual(doc.paths["/muse/v1/offers/{approval_id}/cancel"].post.tags, ["write"]);
    assert.deepEqual(doc.paths["/muse/v1/offers/{approval_id}/cancel"].post.requestBody.content["application/json"].schema.required, ["confirm"]);
    assert.doesNotMatch(JSON.stringify(Object.values(doc.paths).map((p) => Object.values(p).map((op) => op.operationId))), /approve|publish/i);

    res = await call(new Request(`${BASE}/muse`));
    assert.equal(res.status, 200);
    assert.match(res.headers.get("Content-Type"), /^text\/plain/);
    const text = await res.text();
    assert.ok(text.includes(`a remote MCP server at ${BASE}/mcp that uses the header Authorization: Bearer <your key>`));
    assert.ok(text.includes(`${BASE}/muse/openapi.json`));
    assert.match(text, /Never give your PIN to Muse or to any assistant/);
    assert.equal((await call(new Request(`${BASE}/muse`, { method: "POST" }))).status, 405);
  });
});
