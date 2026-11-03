// Preview videos under Node: the PC uploads a short MP4 of each offered clip
// (POST /v1/previews), the relay keeps it in KV until the link expires, and
// the approval page plays it in the browser's own player
// (GET /a/<token>/preview/<clip>, in byte ranges). D1 on node:sqlite, KV on
// a Map, Meta faked. Run with `npm test` from meta-relay/.

import assert from "node:assert/strict";
import { randomBytes } from "node:crypto";
import { afterEach, beforeEach, describe, it } from "node:test";

import worker, { byteRange, cron } from "../worker.js";
import { FakeMeta, inboundText, makeCtx, makeEnv, signed, webhookRequest } from "./harness.mjs";

const BASE = "https://relay.test";
const DEVICE = "dev_0123456789abcdef";
const SECRET = "device-secret-for-tests-0123456789abcdef";
const OTHER = "dev_fedcba9876543210";
const OTHER_SECRET = "another-pc-secret-for-tests-0123456789ab";
const PHONE = "15551234567";
const PIN = "4826";
const MIB = 1024 * 1024;
const CSP = "default-src 'none'; img-src 'self'; media-src 'self'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'";
const COMING = `<p class="small muted">Videos of these clips are still coming from your PC. Reload this page in a minute to watch them.</p>`;
const VIDEO_HEADERS = ["video/mp4", "bytes", "private, max-age=3600", "nosniff"];

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

async function enroll(device = DEVICE, secret = SECRET) {
  const res = await call(new Request(`${BASE}/v1/devices`, {
    method: "POST",
    headers: { Authorization: `Bearer ${env.ENROLL_KEY}`, "Content-Type": "application/json" },
    body: JSON.stringify({ device_id: device, device_secret: secret, name: "Test PC" }),
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

/** A relay with this PC enrolled, a number paired, a PIN and both accounts: with KV unless overridden. */
async function setUp(overrides = {}) {
  env = makeEnv(overrides);
  await enroll();
  await pairPhone();
  await pc("POST", "/v1/pin", { pin: PIN });
  await pc("POST", "/v1/preferences", { notify_on_finish: true, notify_on_fail: true, notify_on_publish: true, remote_approvals: true, preferred: ["instagram"], remind_minutes: 120 });
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
    muse_line: "",
    remote_approvals: true,
    preferred: ["instagram"],
    remind_minutes: 120,
    previews: true,
    clips: [
      { clip_id: 11, title: "Poster dunk in the third", label: "Poster dunk", category: "dunks", duration: 14, texts: { instagram: "Poster! #basketball", facebook: "Poster!" }, thumb: Buffer.from("jpegbytes").toString("base64") },
      { clip_id: 12, title: 'Corner three, "cash"', label: "Corner three", category: "threes", duration: 11, texts: { instagram: "Cash", facebook: "Cash" } },
      { clip_id: 13, title: "Alley-oop", label: "Alley-oop", category: "dunks", duration: 12, texts: { instagram: "Oop", facebook: "Oop" } },
    ],
    ...overrides,
  };
}

function tokenFrom(message) {
  return message.template.components.find((c) => c.sub_type === "url").parameters[0].text;
}

/** Sends an offer to the paired number: its id, its link's token and the relay's answer. */
async function sendOffer(overrides = {}) {
  const res = await pc("POST", "/v1/notify", offer(overrides));
  assert.equal(res.status, 200);
  return { id: res.data.approval_id, token: tokenFrom(fake.messages.at(-1)), answer: res.data };
}

async function page(token) {
  const res = await call(new Request(`${BASE}/a/${token}`));
  return { status: res.status, headers: res.headers, text: await res.text() };
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

async function approve(token) {
  const view = (await page(token)).text;
  const r = await submit(token, [["csrf", csrfOf(view)], ["action", "approve"], ["clip", "11"], ["platform", "instagram"], ["confirm", "yes"], ["pin", PIN]]);
  assert.equal(r.status, 303);
}

async function defer(token) {
  const r = await submit(token, [["csrf", csrfOf((await page(token)).text)], ["action", "defer"]]);
  assert.equal(r.status, 303);
}

/** n bytes that start as an MP4 does: the first box's size, then "ftyp" at byte 4. */
function mp4(n) {
  const bytes = randomBytes(n);
  bytes.write("ftyp", 4);
  return bytes;
}

/** A preview upload as remote_approval/relay.py sends one, like uploadRequest in relay.test.mjs: the query signed, the body UNSIGNED-PAYLOAD. */
function previewRequest(pathAndQuery, bytes, { device = DEVICE, secret = SECRET, length = bytes.length } = {}) {
  const headers = signed(BASE, device, secret, "POST", pathAndQuery, undefined, { unsigned: true }).headers;
  headers.set("Content-Type", "video/mp4");
  headers.set("Content-Length", String(length));
  return new Request(BASE + pathAndQuery, { method: "POST", headers, body: bytes, duplex: "half" });
}

/** Uploads a preview of an offer's clip. The query's size is the body's unless given. */
async function upload(approvalId, clipId, bytes, { size = bytes.length, ...options } = {}) {
  const res = await call(previewRequest(`/v1/previews?approval_id=${approvalId}&clip_id=${clipId}&size=${size}`, bytes, options));
  return { status: res.status, data: await res.json() };
}

/** What the page's player gets for a clip, asked with a Range header when one is given. */
async function play(token, clipId, range) {
  const res = await call(new Request(`${BASE}/a/${token}/preview/${clipId}`, { headers: range === undefined ? {} : { Range: range } }));
  return { status: res.status, headers: res.headers, bytes: Buffer.from(await res.arrayBuffer()) };
}

function videoHeaders(headers) {
  return ["Content-Type", "Accept-Ranges", "Cache-Control", "X-Content-Type-Options"].map((h) => headers.get(h));
}

/** The previews D1 notes, without their times. */
function rows() {
  return env.DB.raw.prepare("SELECT approval_id, clip_id, size FROM previews ORDER BY approval_id, clip_id").all().map((r) => ({ ...r }));
}

function nowS() {
  return Math.floor(Date.now() / 1000);
}

beforeEach(async () => {
  realFetch = globalThis.fetch;
  fake = new FakeMeta(realFetch);
  globalThis.fetch = fake.fetch;
  await setUp();
});

afterEach(() => {
  globalThis.fetch = realFetch;
});

describe("what the relay tells the PC", () => {
  it("says on /health whether it keeps preview videos", async () => {
    let res = await call(new Request(`${BASE}/health`));
    assert.deepEqual(await res.json(), { ok: true, version: "1.1.0", previews: true });
    env = makeEnv({ PREVIEWS: undefined });
    res = await call(new Request(`${BASE}/health`));
    assert.deepEqual(await res.json(), { ok: true, version: "1.1.0", previews: false });
  });

  it("says so in the ready answer and its duplicate, and expects previews only from a PC that said it sends them", async () => {
    const first = await pc("POST", "/v1/notify", offer());
    assert.equal(first.data.previews, true);
    const again = await pc("POST", "/v1/notify", offer());
    assert.equal(again.data.duplicate, true);
    assert.equal(again.data.approval_id, first.data.approval_id);
    assert.equal(again.data.previews, true);
    // A PC from before previews never says it sends them.
    const old = await pc("POST", "/v1/notify", offer({ event_key: "ready:job:8:1", review_ref: 4, previews: undefined }));
    assert.equal(old.data.previews, true);
    const kept = new Map(env.DB.raw.prepare("SELECT id, payload FROM approvals").all().map((r) => [r.id, JSON.parse(r.payload).previews]));
    assert.deepEqual(kept, new Map([[first.data.approval_id, true], [old.data.approval_id, false]]));
  });

  it("says so too when the offer is kept for Muse alone", async () => {
    await pc("POST", "/v1/preferences", { remote_approvals: true, whatsapp_enabled: false, preferred: ["instagram"] });
    assert.equal((await pc("POST", "/v1/muse/token", {})).status, 200);
    const res = await pc("POST", "/v1/notify", offer());
    assert.equal(res.data.channel, "muse");
    assert.equal(res.data.previews, true);
  });
});

describe("uploading a preview", () => {
  it("keeps it in KV until five minutes after the link expires, then notes it in D1", async () => {
    const { id } = await sendOffer();
    const bytes = mp4(3000);
    const r = await upload(id, 11, bytes);
    assert.equal(r.status, 200);
    assert.deepEqual(r.data, { ok: true, clip_id: 11, size: 3000 });
    const { expires_at: expires } = env.DB.raw.prepare("SELECT expires_at FROM approvals WHERE id = ?").get(id);
    const kept = env.PREVIEWS.raw.get(`preview:${id}:11`);
    assert.equal(kept.expiration, expires + 300);
    assert.ok(Buffer.from(kept.bytes).equals(bytes));
    assert.deepEqual(rows(), [{ approval_id: id, clip_id: 11, size: 3000 }]);
    assert.ok(Math.abs(env.DB.raw.prepare("SELECT at FROM previews").get().at - nowS()) <= 2);
  });

  it("takes one of exactly 4 MiB, the same clip again, and one for an offer put aside or with two minutes left", async () => {
    const { id, token } = await sendOffer();
    assert.equal((await upload(id, 11, mp4(4 * MIB))).status, 200);
    // A retry replaces it.
    assert.equal((await upload(id, 11, mp4(2000))).status, 200);
    assert.deepEqual(rows(), [{ approval_id: id, clip_id: 11, size: 2000 }]);
    assert.equal(env.PREVIEWS.raw.get(`preview:${id}:11`).bytes.byteLength, 2000);
    await defer(token);
    assert.equal((await upload(id, 12, mp4(1000))).status, 200);
    env.DB.raw.prepare("UPDATE approvals SET expires_at = ?").run(nowS() + 130);
    assert.equal((await upload(id, 13, mp4(1000))).status, 200);
    assert.deepEqual(rows().map((r) => r.clip_id), [11, 12, 13]);
  });
});

describe("refusing a preview", () => {
  function nothingKept() {
    assert.equal(env.PREVIEWS.raw.size, 0);
    assert.deepEqual(rows(), []);
  }

  it("refuses one that isn't signed, or whose query changed after signing (401)", async () => {
    const { id } = await sendOffer();
    const bytes = mp4(1000);
    const path = `/v1/previews?approval_id=${id}&clip_id=11&size=1000`;
    let res = await call(new Request(BASE + path, { method: "POST", headers: { "Content-Type": "video/mp4", "Content-Length": "1000" }, body: bytes, duplex: "half" }));
    assert.equal(res.status, 401);
    assert.equal((await res.json()).code, "unsigned");
    // Signed for clip 11, sent for clip 12.
    const forClip11 = previewRequest(path, bytes);
    res = await call(new Request(BASE + path.replace("clip_id=11", "clip_id=12"), { method: "POST", headers: forClip11.headers, body: bytes, duplex: "half" }));
    assert.equal(res.status, 401);
    assert.equal((await res.json()).code, "bad_signature");
    // Signed over a body's hash instead of UNSIGNED-PAYLOAD.
    const hashed = signed(BASE, DEVICE, SECRET, "POST", path, "").headers;
    hashed.set("Content-Length", "1000");
    res = await call(new Request(BASE + path, { method: "POST", headers: hashed, body: bytes, duplex: "half" }));
    assert.equal(res.status, 401);
    assert.equal((await res.json()).code, "bad_signature");
    nothingKept();
  });

  it("refuses another PC's offer, a clip not in the offer, and an offer it doesn't have (404)", async () => {
    const { id } = await sendOffer();
    await enroll(OTHER, OTHER_SECRET);
    for (const [approvalId, clipId, who] of [[id, 11, { device: OTHER, secret: OTHER_SECRET }], [id, 99, {}], [`apr_${"0".repeat(24)}`, 11, {}]]) {
      const r = await upload(approvalId, clipId, mp4(1000), who);
      assert.equal(r.status, 404, `${approvalId} ${clipId}`);
      assert.equal(r.data.code, "bad_request");
    }
    nothingKept();
  });

  it("refuses an offer that is closed, or whose link expires within two minutes (409 closed)", async () => {
    const cancelled = await sendOffer();
    assert.equal((await pc("POST", `/v1/approvals/${cancelled.id}/cancel`, {})).data.cancelled, true);
    const approved = await sendOffer({ event_key: "ready:job:8:1", review_ref: 4 });
    await approve(approved.token);
    const soon = await sendOffer({ event_key: "ready:job:9:1", review_ref: 5 });
    env.DB.raw.prepare("UPDATE approvals SET expires_at = ? WHERE id = ?").run(nowS() + 120, soon.id);
    const gone = await sendOffer({ event_key: "ready:job:10:1", review_ref: 6 });
    env.DB.raw.prepare("UPDATE approvals SET expires_at = ? WHERE id = ?").run(nowS() - 5, gone.id);
    for (const [what, o] of Object.entries({ cancelled, approved, soon, gone })) {
      const r = await upload(o.id, 11, mp4(1000));
      assert.equal(r.status, 409, what);
      assert.equal(r.data.code, "closed", what);
    }
    nothingKept();
  });

  it("refuses one over 4 MiB (413), and one whose size doesn't match its body or that isn't an MP4 (400)", async () => {
    const { id } = await sendOffer();
    let r = await upload(id, 11, mp4(4 * MIB + 1));
    assert.equal(r.status, 413);
    assert.equal(r.data.code, "too_large");
    const jpeg = Buffer.concat([Buffer.from([0xff, 0xd8, 0xff, 0xe0, 0x00, 0x10]), Buffer.from("JFIF"), Buffer.alloc(990)]);
    for (const [what, bytes, options] of [
      ["a size unlike its Content-Length", mp4(1000), { size: 1001 }],
      ["a body shorter than both say", mp4(500), { size: 1000, length: 1000 }],
      ["a JPEG", jpeg, {}],
      ['"ftyp" at byte 0', Buffer.concat([Buffer.from("ftyp"), randomBytes(996)]), {}],
      ["too short to hold a box", Buffer.from("\0\0\0\x08fty", "latin1"), {}],
    ]) {
      r = await upload(id, 11, bytes, options);
      assert.equal(r.status, 400, what);
      assert.equal(r.data.code, "bad_request", what);
    }
    const path = `/v1/previews?approval_id=${id}&clip_id=11&size=1000`;
    const headers = signed(BASE, DEVICE, SECRET, "POST", path, undefined, { unsigned: true }).headers;
    headers.delete("Content-Length");
    headers.set("Content-Type", "video/mp4");
    const res = await call(new Request(BASE + path, { method: "POST", headers, body: mp4(1000), duplex: "half" }));
    assert.equal(res.status, 400, "no Content-Length");
    nothingKept();
  });

  it("refuses a malformed query (400)", async () => {
    const { id } = await sendOffer();
    for (const query of [
      "approval_id=apr_123&clip_id=11&size=1000",
      `approval_id=${id}&clip_id=0&size=1000`,
      `approval_id=${id}&clip_id=-11&size=1000`,
      `approval_id=${id}&clip_id=1.5&size=1000`,
      `approval_id=${id}&clip_id=11&size=0`,
      `approval_id=${id}&clip_id=11&size=1e3`,
      `approval_id=${id}&clip_id=11`,
    ]) {
      const res = await call(previewRequest(`/v1/previews?${query}`, mp4(1000)));
      assert.equal(res.status, 400, query);
      assert.equal((await res.json()).code, "bad_request", query);
    }
    nothingKept();
  });

  it("checks the size before the offer, the offer before the clip, and the clip before the bytes", async () => {
    const { id, token } = await sendOffer();
    assert.equal((await upload(`apr_${"0".repeat(24)}`, 11, mp4(4 * MIB + 1))).status, 413);
    assert.equal((await upload(id, 99, Buffer.alloc(1000))).status, 404);
    await approve(token);
    assert.equal((await upload(id, 99, mp4(1000))).status, 409);
    nothingKept();
  });

  it("answers 503 preview_not_kept when KV can't keep it, and notes nothing", async () => {
    const { id } = await sendOffer();
    env.PREVIEWS.put = async () => {
      throw new Error("KV is down");
    };
    const r = await upload(id, 11, mp4(1000));
    assert.equal(r.status, 503);
    assert.equal(r.data.code, "preview_not_kept");
    assert.deepEqual(rows(), []);
  });
});

describe("a relay without KV", () => {
  it("refuses previews (409 previews_off) before anything else, says so, and shows thumbnails", async () => {
    await setUp({ PREVIEWS: undefined });
    assert.equal(env.PREVIEWS, undefined);
    const { id, token, answer } = await sendOffer();
    assert.equal(answer.previews, false);
    assert.equal(JSON.parse(env.DB.raw.prepare("SELECT payload FROM approvals").get().payload).previews, false);
    for (const [approvalId, clipId] of [[id, 11], ["apr_123", 0]]) {
      const r = await upload(approvalId, clipId, mp4(1000));
      assert.equal(r.status, 409);
      assert.equal(r.data.code, "previews_off");
    }
    const view = (await page(token)).text;
    assert.doesNotMatch(view, /<video|still coming/);
    assert.ok(view.includes(`<img src="/a/${token}/thumb/11" alt="">`));
    assert.equal((await play(token, 11, "bytes=0-1")).status, 404);
  });

  it("stops playing previews kept before the binding was taken away", async () => {
    const { id, token } = await sendOffer();
    assert.equal((await upload(id, 11, mp4(1000))).status, 200);
    assert.match((await page(token)).text, /<video /);
    env.PREVIEWS = undefined;
    const view = (await page(token)).text;
    assert.doesNotMatch(view, /<video/);
    assert.ok(view.includes(`<img src="/a/${token}/thumb/11" alt="">`));
    assert.equal((await play(token, 11)).status, 404);
  });
});

describe("the approval page", () => {
  it("plays each clip that has a preview, its thumbnail as the poster, and leaves the others as they were", async () => {
    const { id, token } = await sendOffer();
    const before = (await page(token)).text;
    assert.ok(before.includes(`<input type="checkbox" name="clip" value="11" checked><img src="/a/${token}/thumb/11" alt="">`));
    assert.ok(before.includes(`<input type="checkbox" name="clip" value="12" checked><div class="ph" aria-hidden="true">&#9654;</div>`));
    assert.doesNotMatch(before, /<video/);
    assert.equal((await upload(id, 11, mp4(1000))).status, 200);
    assert.equal((await upload(id, 12, mp4(1000))).status, 200);
    const res = await page(token);
    assert.equal(res.headers.get("Content-Security-Policy"), CSP);
    const view = res.text;
    assert.ok(view.includes(`<input type="checkbox" name="clip" value="11" checked><video controls preload="none" poster="/a/${token}/thumb/11" src="/a/${token}/preview/11" aria-label="Play Poster dunk in the third">Your browser can't play this clip here.</video>`));
    assert.ok(view.includes(`<input type="checkbox" name="clip" value="12" checked><video controls preload="none" src="/a/${token}/preview/12" aria-label="Play Corner three, &quot;cash&quot;">Your browser can't play this clip here.</video>`));
    assert.ok(view.includes(`<input type="checkbox" name="clip" value="13" checked><div class="ph" aria-hidden="true">&#9654;</div>`));
    assert.equal(view.match(/<video /g).length, 2);
    // Fullscreen on an iPhone, and still no script.
    assert.doesNotMatch(view, /<img|playsinline|<script/i);
    assert.deepEqual(ticks(view), ticks(before));
    assert.deepEqual(ticks(view), [["clip", "11"], ["clip", "12"], ["clip", "13"], ["platform", "instagram"]]);
  });

  it("approves as before, with the players on the page", async () => {
    const { id, token } = await sendOffer();
    for (const clip of [11, 12, 13]) assert.equal((await upload(id, clip, mp4(1000))).status, 200);
    const view = (await page(token)).text;
    const r = await submit(token, [["csrf", csrfOf(view)], ["action", "approve"], ...ticks(view), ["confirm", "yes"], ["pin", PIN]]);
    assert.equal(r.status, 303);
    const cmd = (await commands()).find((c) => c.kind === "approve");
    assert.deepEqual([cmd.clips, cmd.platforms], [[11, 12, 13], ["instagram"]]);
  });

  it("says the videos are still coming until every clip has one, for ten minutes at most", async () => {
    const { id, token } = await sendOffer();
    assert.ok((await page(token)).text.includes(`<h2>Clips</h2>${COMING}<label class="card row clip">`));
    for (const clip of [11, 12]) await upload(id, clip, mp4(1000));
    assert.ok((await page(token)).text.includes(`<h2>Clips</h2>${COMING}`));
    await upload(id, 13, mp4(1000));
    assert.doesNotMatch((await page(token)).text, /still coming/);
    // One still missing ten minutes on: it isn't coming.
    env.DB.raw.prepare("DELETE FROM previews WHERE clip_id = 13").run();
    assert.ok((await page(token)).text.includes(COMING));
    env.DB.raw.prepare("UPDATE approvals SET created_at = created_at - 600").run();
    const view = (await page(token)).text;
    assert.doesNotMatch(view, /still coming/);
    assert.equal(view.match(/<video /g).length, 2);
  });

  it("never says so for a PC that sends no previews", async () => {
    const { token } = await sendOffer({ previews: undefined });
    const view = (await page(token)).text;
    assert.doesNotMatch(view, /still coming|<video/);
    assert.ok(view.includes(`<img src="/a/${token}/thumb/11" alt="">`));
  });

  it("says so before the clips on the read-only page, where they play too", async () => {
    await pc("POST", "/v1/preferences", { remote_approvals: false });
    const { id, token } = await sendOffer();
    assert.equal((await upload(id, 11, mp4(1000))).status, 200);
    const view = (await page(token)).text;
    assert.ok(view.includes(`Open Clips Kitty on your PC to publish these.</div>${COMING}<div class="card clip"><video controls preload="none" poster="/a/${token}/thumb/11" src="/a/${token}/preview/11"`));
    assert.doesNotMatch(view, /name="clip"/);
  });

  it("still shows, with thumbnails, on a D1 from before previews, and the cron still runs", async () => {
    const { token } = await sendOffer();
    env.DB.raw.exec("DROP TABLE previews");
    const res = await page(token);
    assert.equal(res.status, 200);
    assert.ok(res.text.includes(`<img src="/a/${token}/thumb/11" alt="">`));
    assert.doesNotMatch(res.text, /<video/);
    env.DB.raw.prepare("UPDATE approvals SET expires_at = ?").run(nowS() - 5);
    await cron(env);
    assert.equal(env.DB.raw.prepare("SELECT state FROM approvals").get().state, "expired");
  });
});

describe("playing a preview", () => {
  let id;
  let token;
  let bytes;

  beforeEach(async () => {
    ({ id, token } = await sendOffer());
    bytes = mp4(3000);
    assert.equal((await upload(id, 11, bytes)).status, 200);
  });

  it("serves the whole file without a Range, as a private video not to be sniffed", async () => {
    const r = await play(token, 11);
    assert.equal(r.status, 200);
    assert.ok(r.bytes.equals(bytes));
    assert.deepEqual(videoHeaders(r.headers), VIDEO_HEADERS);
    assert.equal(r.headers.get("Content-Range"), null);
  });

  it("answers an iPhone's first request, bytes=0-1, with 206 and two bytes", async () => {
    const r = await play(token, 11, "bytes=0-1");
    assert.equal(r.status, 206);
    assert.equal(r.headers.get("Content-Range"), "bytes 0-1/3000");
    assert.equal(r.bytes.length, 2);
    assert.ok(r.bytes.equals(bytes.subarray(0, 2)));
    assert.deepEqual(videoHeaders(r.headers), VIDEO_HEADERS);
  });

  it("serves one range from a start, a suffix, or with its end clamped to the file (206)", async () => {
    for (const [range, start, end] of [["bytes=4-7", 4, 7], ["bytes=0-", 0, 2999], ["bytes=2990-", 2990, 2999], ["bytes=-100", 2900, 2999], ["bytes=-5000", 0, 2999], ["bytes=2990-9999", 2990, 2999]]) {
      const r = await play(token, 11, range);
      assert.equal(r.status, 206, range);
      assert.equal(r.headers.get("Content-Range"), `bytes ${start}-${end}/3000`, range);
      assert.ok(r.bytes.equals(bytes.subarray(start, end + 1)), range);
      assert.deepEqual(videoHeaders(r.headers), VIDEO_HEADERS, range);
    }
    assert.equal((await play(token, 11, "bytes=4-7")).bytes.toString("latin1"), "ftyp");
  });

  it("answers 416 to a range that starts past the end or asks for no bytes", async () => {
    for (const range of ["bytes=3000-", "bytes=3000-3100", "bytes=-0", "bytes=99999999999999999999-"]) {
      const r = await play(token, 11, range);
      assert.equal(r.status, 416, range);
      assert.equal(r.headers.get("Content-Range"), "bytes */3000", range);
      assert.equal(r.bytes.length, 0, range);
      assert.deepEqual(videoHeaders(r.headers), VIDEO_HEADERS, range);
    }
  });

  it("serves the whole file for a Range it doesn't take", async () => {
    for (const range of ["", "bytes=0-1,4-7", "items=0-1", "bytes=7-4", "bytes=-", "bytes=abc", "bytes 0-1", "0-1"]) {
      const r = await play(token, 11, range);
      assert.equal(r.status, 200, range);
      assert.ok(r.bytes.equals(bytes), range);
      assert.equal(r.headers.get("Content-Range"), null, range);
    }
  });

  it("reads one Range header against a size (byteRange)", () => {
    for (const [header, want] of [
      [null, { status: 200 }],
      ["bytes=0-1", { status: 206, start: 0, end: 1 }],
      ["bytes=5-", { status: 206, start: 5, end: 9 }],
      ["bytes=-3", { status: 206, start: 7, end: 9 }],
      ["bytes=-30", { status: 206, start: 0, end: 9 }],
      ["bytes=8-20", { status: 206, start: 8, end: 9 }],
      ["Bytes=0-1", { status: 206, start: 0, end: 1 }],
      ["bytes=10-", { status: 416 }],
      ["bytes=10-12", { status: 416 }],
      ["bytes=-0", { status: 416 }],
      ["bytes=3-2", { status: 200 }],
      ["bytes=0-1, 3-4", { status: 200 }],
      ["bytes=-", { status: 200 }],
      ["pages=0-1", { status: 200 }],
      ["bytes=0x10-", { status: 200 }],
    ]) {
      assert.deepEqual(byteRange(header, 10), want, String(header));
    }
    assert.deepEqual(byteRange("bytes=0-1", 0), { status: 416 });
  });

  it("keeps playing while the offer is put aside, and 404s for a clip without one or a link it doesn't know", async () => {
    await defer(token);
    assert.equal((await play(token, 11, "bytes=0-1")).status, 206);
    for (const r of [await play(token, 12), await play("x".repeat(43), 11)]) {
      assert.equal(r.status, 404);
      assert.equal(r.headers.get("Cache-Control"), "no-store");
    }
  });

  it("stops once the offer is approved", async () => {
    await approve(token);
    const r = await play(token, 11, "bytes=0-1");
    assert.equal(r.status, 404);
    assert.equal(r.headers.get("Cache-Control"), "no-store");
    assert.doesNotMatch((await page(token)).text, /<video/);
  });

  it("stops once the offer is cancelled", async () => {
    assert.equal((await submit(token, [["csrf", csrfOf((await page(token)).text)], ["action", "cancel"]])).status, 303);
    assert.equal((await play(token, 11, "bytes=0-1")).status, 404);
  });

  it("stops once the link expires, before the cron marks it and after", async () => {
    env.DB.raw.prepare("UPDATE approvals SET expires_at = ?").run(nowS() - 5);
    assert.equal((await play(token, 11, "bytes=0-1")).status, 404);
    await cron(env);
    assert.equal(env.DB.raw.prepare("SELECT state FROM approvals").get().state, "expired");
    assert.equal((await play(token, 11)).status, 404);
  });

  it("stops once KV lets the bytes expire", async () => {
    env.PREVIEWS.raw.get(`preview:${id}:11`).expiration = nowS();
    assert.equal((await play(token, 11, "bytes=0-1")).status, 404);
  });
});

describe("the cron", () => {
  it("keeps the rows of offers still open or put aside, and leaves the bytes for KV to expire", async () => {
    const open = await sendOffer();
    const aside = await sendOffer({ event_key: "ready:job:8:1", review_ref: 4 });
    const approved = await sendOffer({ event_key: "ready:job:9:1", review_ref: 5 });
    const cancelled = await sendOffer({ event_key: "ready:job:10:1", review_ref: 6 });
    const expired = await sendOffer({ event_key: "ready:job:11:1", review_ref: 7 });
    for (const o of [open, aside, approved, cancelled, expired]) assert.equal((await upload(o.id, 11, mp4(1000))).status, 200);
    await defer(aside.token);
    await approve(approved.token);
    assert.equal((await pc("POST", `/v1/approvals/${cancelled.id}/cancel`, {})).data.cancelled, true);
    env.DB.raw.prepare("UPDATE approvals SET expires_at = ? WHERE id = ?").run(nowS() - 5, expired.id);
    await cron(env);
    assert.deepEqual(rows().map((r) => r.approval_id).sort(), [open.id, aside.id].sort());
    assert.equal(env.PREVIEWS.raw.size, 5);
  });
});
