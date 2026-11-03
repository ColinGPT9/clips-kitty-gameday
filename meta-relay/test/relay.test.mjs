// The relay end to end under Node: D1 on node:sqlite, Meta faked, nothing
// leaves the machine. Run with `node --test test/` from meta-relay/.

import assert from "node:assert/strict";
import { createHmac, randomBytes } from "node:crypto";
import { afterEach, beforeEach, describe, it } from "node:test";

import worker, { cron, handleWebhook } from "../worker.js";
import { canonical } from "../lib/crypto.js";
import { FakeMeta, inboundButton, inboundText, makeCtx, makeEnv, signed, statusUpdate, webhookRequest } from "./harness.mjs";

const BASE = "https://relay.test";
const DEVICE = "dev_0123456789abcdef";
const SECRET = "device-secret-for-tests-0123456789abcdef";
const PHONE = "15551234567";

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
  const data = await res.json();
  return { status: res.status, data };
}

async function enroll() {
  const res = await call(new Request(`${BASE}/v1/devices`, {
    method: "POST",
    headers: { Authorization: `Bearer ${env.ENROLL_KEY}`, "Content-Type": "application/json" },
    body: JSON.stringify({ device_id: DEVICE, device_secret: SECRET, name: "Test PC" }),
  }));
  assert.equal(res.status, 200);
}

async function wh(payload) {
  const res = await call(webhookRequest(BASE, env.META_APP_SECRET, payload));
  assert.equal(res.status, 200);
}

async function pairPhone() {
  const start = await pc("POST", "/v1/pair/start", {});
  assert.equal(start.status, 200);
  await wh(inboundText(PHONE, `LINK ${start.data.code}`));
  const confirm = await pc("POST", "/v1/pair/confirm", { confirm: true });
  assert.equal(confirm.data.pairing.state, "confirmed");
}

async function connectAll() {
  const { data } = await pc("POST", "/v1/accounts/connect", { platform: "facebook" });
  const state = new URL(data.url).searchParams.get("state");
  const res = await call(new Request(`${BASE}/oauth/facebook/callback?state=${state}&code=abc`));
  assert.equal(res.status, 200, await res.clone().text());
}

function offer(overrides = {}) {
  return {
    kind: "ready",
    event_key: "ready:job:7:1",
    review_ref: 3,
    fingerprint: "f".repeat(32),
    expires_at: new Date(Date.now() + 24 * 3600 * 1000).toISOString(),
    title: "Raptors vs Celtics",
    summary_text: "3 clips: 2 dunks and 1 three",
    muse_line: "",
    remote_approvals: true,
    preferred: ["instagram"],
    remind_minutes: 120,
    clips: [
      { clip_id: 11, title: "Barnes poster dunk", label: "Poster dunk", category: "dunks", duration: 14, texts: { instagram: "Poster! #basketball", facebook: "Poster!" }, thumb: Buffer.from("jpegbytes").toString("base64") },
      { clip_id: 12, title: "Corner three", label: "Corner three", category: "threes", duration: 11, texts: { instagram: "Cash", facebook: "Cash" } },
      { clip_id: 13, title: "Alley-oop", label: "Alley-oop", category: "dunks", duration: 12, texts: { instagram: "Oop", facebook: "Oop" } },
    ],
    ...overrides,
  };
}

function tokenFrom(message) {
  const button = message.template.components.find((c) => c.sub_type === "url");
  return button.parameters[0].text;
}

// The Referrer-Policy of the approval page last opened. submit() posts its
// form with the Origin a browser derives from it, as a phone would.
let pagePolicy = "";

async function page(token) {
  const res = await call(new Request(`${BASE}/a/${token}`));
  pagePolicy = res.headers.get("Referrer-Policy") || "";
  return { status: res.status, text: await res.text() };
}

/** The Origin a browser puts on a page's form post to its own site (Fetch,
 *  "append a request Origin header"): null under no-referrer, else the site. */
function browserOrigin(policy) {
  return policy.split(",").map((p) => p.trim()).filter(Boolean).pop() === "no-referrer" ? "null" : BASE;
}

// A browser without Fetch Metadata (Safari before 16.4) sends none of these.
const NO_FETCH_METADATA = { "Sec-Fetch-Site": undefined, "Sec-Fetch-Mode": undefined, "Sec-Fetch-Dest": undefined };

function csrfOf(html) {
  return html.match(/name="csrf" value="([0-9a-f]{64})"/)[1];
}

/** The ticked boxes of an approval page, as its form would send them. */
function ticks(html) {
  return [...html.matchAll(/<input type="checkbox" name="(clip|platform|confirm)" value="([^"]*)" checked>/g)].map((m) => [m[1], m[2]]);
}

async function submit(token, fields, headers = {}) {
  const form = new URLSearchParams();
  for (const [k, v] of fields) form.append(k, v);
  // What a phone's browser sends with the page's own form, not a chosen
  // Origin. A header given as undefined is left out.
  const sent = {
    "Content-Type": "application/x-www-form-urlencoded",
    Origin: browserOrigin(pagePolicy),
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Dest": "document",
    ...headers,
  };
  const res = await call(new Request(`${BASE}/a/${token}`, {
    method: "POST",
    headers: Object.fromEntries(Object.entries(sent).filter(([, v]) => v !== undefined)),
    body: form.toString(),
  }));
  return { status: res.status, text: res.status === 303 ? "" : await res.text(), location: res.headers.get("Location") };
}

function uploadRequest(pathAndQuery, bytes) {
  const headers = signed(BASE, DEVICE, SECRET, "POST", pathAndQuery, undefined, { unsigned: true }).headers;
  headers.set("Content-Type", "application/octet-stream");
  headers.set("Content-Length", String(bytes.length));
  return new Request(BASE + pathAndQuery, { method: "POST", headers, body: bytes, duplex: "half" });
}

async function commands() {
  return (await pc("GET", "/v1/commands")).data.commands;
}

beforeEach(async () => {
  pagePolicy = "";
  env = makeEnv();
  realFetch = globalThis.fetch;
  fake = new FakeMeta(realFetch);
  globalThis.fetch = fake.fetch;
  await enroll();
});

afterEach(() => {
  globalThis.fetch = realFetch;
});

describe("basics", () => {
  it("answers health", async () => {
    const res = await call(new Request(`${BASE}/health`));
    assert.equal((await res.json()).ok, true);
  });

  it("verifies the WhatsApp webhook only with the right token", async () => {
    const good = await call(new Request(`${BASE}/webhook?hub.mode=subscribe&hub.verify_token=verify-me&hub.challenge=12345`));
    assert.equal(good.status, 200);
    assert.equal(await good.text(), "12345");
    const bad = await call(new Request(`${BASE}/webhook?hub.mode=subscribe&hub.verify_token=nope&hub.challenge=12345`));
    assert.equal(bad.status, 403);
  });

  it("refuses a webhook whose signature is wrong", async () => {
    const raw = JSON.stringify(inboundText(PHONE, "hello"));
    const res = await call(new Request(`${BASE}/webhook`, { method: "POST", headers: { "X-Hub-Signature-256": `sha256=${"0".repeat(64)}` }, body: raw }));
    assert.equal(res.status, 401);
  });

  it("enrols only with the relay's key", async () => {
    const res = await call(new Request(`${BASE}/v1/devices`, {
      method: "POST", headers: { Authorization: "Bearer wrong" }, body: JSON.stringify({ device_id: DEVICE, device_secret: SECRET }),
    }));
    assert.equal(res.status, 401);
  });
});

describe("signed requests", () => {
  it("accepts a good signature and refuses a bad one", async () => {
    assert.equal((await pc("GET", "/v1/status")).status, 200);
    const res = await call(signed(BASE, DEVICE, "the-wrong-secret-0123456789abcdefghij", "GET", "/v1/status"));
    assert.equal(res.status, 401);
  });

  it("refuses a replayed nonce", async () => {
    const nonce = randomBytes(12).toString("hex");
    const first = await call(signed(BASE, DEVICE, SECRET, "GET", "/v1/status", undefined, { nonce }));
    assert.equal(first.status, 200);
    const again = await call(signed(BASE, DEVICE, SECRET, "GET", "/v1/status", undefined, { nonce }));
    assert.equal(again.status, 401);
    assert.equal((await again.json()).code, "replay");
  });

  it("refuses a request from a clock far off", async () => {
    const res = await call(signed(BASE, DEVICE, SECRET, "GET", "/v1/status", undefined, { ts: Math.floor(Date.now() / 1000) - 3600 }));
    assert.equal((await res.json()).code, "clock");
  });

  it("refuses a body changed after signing", async () => {
    const req = signed(BASE, DEVICE, SECRET, "POST", "/v1/pin", { pin: "4826" });
    const tampered = new Request(req.url, { method: "POST", headers: req.headers, body: JSON.stringify({ pin: "1357" }) });
    assert.equal((await call(tampered)).status, 401);
  });
});

describe("pairing", () => {
  it("pairs a number that sends the code, after the PC confirms it", async () => {
    const start = await pc("POST", "/v1/pair/start", {});
    assert.match(start.data.code, /^[A-Z2-9]{6}$/);
    assert.match(start.data.link, /^https:\/\/wa\.me\/15550001111\?text=LINK%20/);
    await wh(inboundText(PHONE, `link ${start.data.code.toLowerCase()}`));
    let status = (await pc("GET", "/v1/status")).data;
    assert.equal(status.pairing.state, "awaiting_confirm");
    assert.equal(status.pairing.tail, "4567");
    assert.match(fake.messages.at(-1).text.body, /ending in 4567/);
    await pc("POST", "/v1/pair/confirm", { confirm: true });
    status = (await pc("GET", "/v1/status")).data;
    assert.equal(status.pairing.state, "confirmed");
    assert.match(fake.messages.at(-1).text.body, /when a video's highlights are ready\. Reply STOP/);
  });

  it("does not pair on a wrong code, and ignores strangers", async () => {
    await pc("POST", "/v1/pair/start", {});
    await wh(inboundText(PHONE, "LINK ZZZZZZ"));
    assert.match(fake.messages.at(-1).text.body, /didn't match/);
    const before = fake.messages.length;
    await wh(inboundText("15559999999", "hello there"));
    assert.equal(fake.messages.length, before);
    assert.equal((await pc("GET", "/v1/status")).data.pairing.state, "pending");
  });

  it("handles a webhook delivered twice once", async () => {
    const start = await pc("POST", "/v1/pair/start", {});
    const payload = inboundText(PHONE, `LINK ${start.data.code}`, "wamid.same");
    await wh(payload);
    const sent = fake.messages.length;
    await wh(payload);
    assert.equal(fake.messages.length, sent);
  });

  it("stops on STOP and refuses to notify", async () => {
    await pairPhone();
    await wh(inboundText(PHONE, "STOP"));
    const res = await pc("POST", "/v1/notify", { kind: "test", event_key: "t1" });
    assert.equal(res.status, 409);
    assert.equal(res.data.code, "opted_out");
  });

  it("rejects weak PINs", async () => {
    assert.equal((await pc("POST", "/v1/pin", { pin: "1111" })).status, 400);
    assert.equal((await pc("POST", "/v1/pin", { pin: "1234" })).status, 400);
    assert.equal((await pc("POST", "/v1/pin", { pin: "12a4" })).status, 400);
    assert.equal((await pc("POST", "/v1/pin", { pin: "4826" })).status, 200);
  });
});

describe("offers and approval", () => {
  beforeEach(async () => {
    await pairPhone();
    await pc("POST", "/v1/pin", { pin: "4826" });
    await pc("POST", "/v1/preferences", { notify_on_finish: true, notify_on_fail: true, notify_on_publish: true, remote_approvals: true, preferred: ["instagram"], remind_minutes: 120 });
    await connectAll();
  });

  it("sends the ready template once per event key", async () => {
    const first = await pc("POST", "/v1/notify", offer());
    assert.equal(first.status, 200);
    assert.equal(first.data.channel, "template");
    const msg = fake.messages.at(-1);
    assert.equal(msg.template.name, "ck_highlights_ready");
    assert.deepEqual(msg.template.components[0].parameters.map((p) => p.text), ["Raptors vs Celtics", "3 clips: 2 dunks and 1 three"]);
    assert.match(tokenFrom(msg), /^[A-Za-z0-9_-]{43}$/);
    const count = fake.messages.length;
    const again = await pc("POST", "/v1/notify", offer());
    assert.equal(again.data.duplicate, true);
    assert.equal(again.data.approval_id, first.data.approval_id);
    assert.equal(fake.messages.length, count);
  });

  it("falls back to an interactive link while the template is not approved", async () => {
    fake.templateFails = 1;
    const res = await pc("POST", "/v1/notify", offer());
    assert.equal(res.data.channel, "interactive");
    const msg = fake.messages.at(-1);
    assert.equal(msg.interactive.type, "cta_url");
    assert.match(msg.interactive.action.parameters.url, /^https:\/\/relay\.test\/a\//);
    assert.match(msg.interactive.body.text, /^Clips Kitty finished your video .+\. Ready to review: 3 clips: 2 dunks and 1 three\. Choose where to publish them before the link expires\.$/);
  });

  it("approves only with the PIN, the confirm tick and real choices", async () => {
    const sent = await pc("POST", "/v1/notify", offer());
    const token = tokenFrom(fake.messages.at(-1));
    const view = await page(token);
    assert.equal(view.status, 200);
    assert.match(view.text, /Barnes poster dunk/);
    assert.match(view.text, /hoopsdaily/);
    assert.doesNotMatch(view.text, /page-token|fb-long/);
    const csrf = csrfOf(view.text);
    const base = [["csrf", csrf], ["action", "approve"], ["clip", "11"], ["platform", "instagram"], ["confirm", "yes"]];

    let r = await submit(token, [...base, ["pin", "0000"]]);
    assert.equal(r.status, 403);
    assert.match(r.text, /PIN is not right/);
    r = await submit(token, base.filter(([k]) => k !== "confirm").concat([["pin", "4826"]]));
    assert.match(r.text, /Tick the box/);
    r = await submit(token, [["csrf", csrf], ["action", "approve"], ["clip", "99"], ["platform", "instagram"], ["confirm", "yes"], ["pin", "4826"]]);
    assert.match(r.text, /Tick at least one clip/);
    assert.equal((await commands()).filter((c) => c.kind === "approve").length, 0);

    r = await submit(token, [...base, ["clip", "12"], ["platform", "facebook"], ["pin", "4826"]]);
    assert.equal(r.status, 303);
    const cmds = await commands();
    const approve = cmds.find((c) => c.kind === "approve");
    assert.deepEqual(approve.clips, [11, 12]);
    assert.deepEqual(approve.platforms, ["instagram", "facebook"]);
    assert.equal(approve.approval_id, sent.data.approval_id);
    assert.equal(approve.review_ref, 3);
    assert.equal(approve.fingerprint, "f".repeat(32));
    const { sig, ...rest } = approve;
    assert.equal(sig, createHmac("sha256", SECRET).update(canonical(rest)).digest("hex"));

    // Single use: a second approval of the same link does nothing.
    await submit(token, [...base, ["pin", "4826"]]);
    assert.equal((await commands()).filter((c) => c.kind === "approve").length, 1);
    assert.match((await page(token)).text, /Waiting for your PC/);
  });

  it("shows the ticks as they were sent after a wrong PIN or a missing tick, so a retry posts only those", async () => {
    await pc("POST", "/v1/notify", offer());
    const token = tokenFrom(fake.messages.at(-1));
    const csrf = csrfOf((await page(token)).text);
    const start = ticks((await page(token)).text);
    assert.deepEqual(start, [["clip", "11"], ["clip", "12"], ["clip", "13"], ["platform", "instagram"]]);

    // Clip 12 taken off, Facebook added and the confirm ticked, then a wrong PIN.
    const chosen = [["clip", "11"], ["clip", "13"], ["platform", "instagram"], ["platform", "facebook"], ["confirm", "yes"]];
    let r = await submit(token, [["csrf", csrf], ["action", "approve"], ...chosen, ["pin", "0000"]]);
    assert.equal(r.status, 403);
    assert.match(r.text, /PIN is not right/);
    assert.deepEqual(ticks(r.text), chosen);

    // Without the confirm the clips and places stay as chosen, and the box stays clear.
    r = await submit(token, [["csrf", csrf], ["action", "approve"], ...chosen.slice(0, 4), ["pin", "4826"]]);
    assert.match(r.text, /Tick the box/);
    assert.deepEqual(ticks(r.text), chosen.slice(0, 4));

    // A link opened again starts from the defaults.
    assert.deepEqual(ticks((await page(token)).text), start);

    // The page as the wrong PIN left it, sent again with the right PIN: clip 12 stays out.
    r = await submit(token, [["csrf", csrf], ["action", "approve"], ...chosen, ["pin", "0000"]]);
    r = await submit(token, [["csrf", csrf], ["action", "approve"], ...ticks(r.text), ["pin", "4826"]]);
    assert.equal(r.status, 303);
    const approve = (await commands()).find((c) => c.kind === "approve");
    assert.deepEqual(approve.clips, [11, 13]);
    assert.deepEqual(approve.platforms, ["instagram", "facebook"]);
  });

  it("locks the PIN after five wrong tries", async () => {
    await pc("POST", "/v1/notify", offer());
    const token = tokenFrom(fake.messages.at(-1));
    const csrf = csrfOf((await page(token)).text);
    const fields = [["csrf", csrf], ["action", "approve"], ["clip", "11"], ["platform", "instagram"], ["confirm", "yes"]];
    for (let i = 0; i < 5; i++) await submit(token, [...fields, ["pin", "9999"]]);
    let r = await submit(token, [...fields, ["pin", "4826"]]);
    assert.equal(r.status, 429);
    assert.match(r.text, /Too many wrong PINs/);
    // The locked page keeps the one clip ticked, not every clip again, so
    // the try made from it once the quarter hour passes posts only that one.
    assert.deepEqual(ticks(r.text), fields.slice(2));
    env.DB.raw.prepare("UPDATE devices SET pin_locked_until = 0").run();
    r = await submit(token, [["csrf", csrf], ["action", "approve"], ...ticks(r.text), ["pin", "4826"]]);
    assert.equal(r.status, 303);
    assert.deepEqual((await commands()).find((c) => c.kind === "approve").clips, [11]);
  });

  it("refuses a decision without the page's CSRF token or from another site", async () => {
    await pc("POST", "/v1/notify", offer());
    const token = tokenFrom(fake.messages.at(-1));
    let r = await submit(token, [["csrf", "0".repeat(64)], ["action", "cancel"]]);
    assert.equal(r.status, 403);
    assert.match(r.text, /out of date/);
    const csrf = csrfOf((await page(token)).text);
    r = await submit(token, [["csrf", csrf], ["action", "cancel"]], { Origin: "https://evil.example", "Sec-Fetch-Site": "cross-site" });
    assert.equal(r.status, 403);
    assert.match(r.text, /did not come from this page/);
    r = await submit(token, [["csrf", csrf], ["action", "cancel"]], { Origin: "https://evil.example", ...NO_FETCH_METADATA });
    assert.equal(r.status, 403);
    assert.match(r.text, /did not come from this page/);
  });

  it("serves the page with Referrer-Policy same-origin, so its own form keeps its Origin", async () => {
    await pc("POST", "/v1/notify", offer());
    await page(tokenFrom(fake.messages.at(-1)));
    assert.equal(pagePolicy, "same-origin");
    assert.equal(browserOrigin(pagePolicy), BASE);
  });

  for (const action of ["approve", "defer", "cancel"]) {
    it(`takes ${action} from the page as a phone's browser posts it`, async () => {
      await pc("POST", "/v1/notify", offer());
      const token = tokenFrom(fake.messages.at(-1));
      const csrf = csrfOf((await page(token)).text);
      const fields = action === "approve"
        ? [["csrf", csrf], ["action", "approve"], ["clip", "11"], ["platform", "instagram"], ["confirm", "yes"], ["pin", "4826"]]
        : [["csrf", csrf], ["action", action]];
      const r = await submit(token, fields);
      assert.equal(r.status, 303, r.text);
      assert.equal((await commands()).filter((c) => c.kind === action).length, 1);
    });
  }

  it("takes Origin null only when the browser marks the post Sec-Fetch-Site: same-origin", async () => {
    await pc("POST", "/v1/notify", offer());
    const token = tokenFrom(fake.messages.at(-1));
    const csrf = csrfOf((await page(token)).text);
    for (const site of ["cross-site", "same-site", "none"]) {
      const r = await submit(token, [["csrf", csrf], ["action", "cancel"]], { Origin: "null", "Sec-Fetch-Site": site });
      assert.equal(r.status, 403, site);
      assert.match(r.text, /did not come from this page/);
    }
    let r = await submit(token, [["csrf", csrf], ["action", "cancel"]], { Origin: "null", ...NO_FETCH_METADATA });
    assert.equal(r.status, 403);
    assert.match(r.text, /did not come from this page/);
    r = await submit(token, [["csrf", csrf], ["action", "defer"]], { Origin: "null" });
    assert.equal(r.status, 303);
  });

  it("says after Remind me later on the page that WhatsApp sends the link again, and about when", async () => {
    await pc("POST", "/v1/notify", offer());
    const token = tokenFrom(fake.messages.at(-1));
    let view = await page(token);
    assert.doesNotMatch(view.text, /only puts this aside|Reminder set/);
    const r = await submit(token, [["csrf", csrfOf(view.text)], ["action", "defer"]]);
    assert.equal(r.status, 303);
    assert.equal(r.location, `/a/${token}`);
    view = await page(token);
    assert.match(view.text, /<div class="card notice">Reminder set\. WhatsApp sends you this link again in about 2 h\. You can still decide now\.<\/div>/);
    // At the top, where the redirect lands, above the form.
    assert.ok(view.text.indexOf("Reminder set.") < view.text.indexOf("<form"));
    assert.match(view.text, /Approve and publish/);
    const row = env.DB.raw.prepare("SELECT state, remind_at FROM approvals").get();
    assert.equal(row.state, "deferred");
    assert.ok(Math.abs(row.remind_at - (Math.floor(Date.now() / 1000) + 7200)) <= 2);
    // The wait as the page is opened later, to the nearest 5 minutes.
    for (const [left, said] of [[5140, "1 h 25 min"], [2500, "40 min"], [60, "5 min"], [7080, "2 h"]]) {
      env.DB.raw.prepare("UPDATE approvals SET remind_at = ?").run(Math.floor(Date.now() / 1000) + left);
      assert.match((await page(token)).text, new RegExp(`WhatsApp sends you this link again in about ${said}\\.`), String(left));
    }
    // The time has come but cron hasn't yet: soon, not a wait.
    env.DB.raw.prepare("UPDATE approvals SET remind_at = ?").run(Math.floor(Date.now() / 1000) - 5);
    assert.match((await page(token)).text, /WhatsApp sends you this link again in a few minutes\./);
    await cron(env);
    assert.doesNotMatch((await page(token)).text, /Reminder set/);
  });

  it("refuses Remind me later when the link expires first, without promising a reminder", async () => {
    await pc("POST", "/v1/notify", offer({ expires_at: new Date(Date.now() + 3600 * 1000).toISOString() }));
    const token = tokenFrom(fake.messages.at(-1));
    // A browser sends the ticked boxes with whichever button was pressed.
    const chosen = [["clip", "11"], ["clip", "13"], ["platform", "instagram"], ["platform", "facebook"]];
    const r = await submit(token, [["csrf", csrfOf((await page(token)).text)], ...chosen, ["action", "defer"]]);
    assert.equal(r.status, 400);
    assert.match(r.text, /This link expires too soon for Remind me later\. Decide now, or ask for a new offer from the app later\./);
    assert.doesNotMatch(r.text, /reminder would come/);
    assert.deepEqual(ticks(r.text), chosen);
    assert.equal((await commands()).filter((c) => c.kind === "defer").length, 0);
    assert.equal(env.DB.raw.prepare("SELECT state FROM approvals").get().state, "open");
  });

  it("promises no WhatsApp reminder to a number that sent STOP", async () => {
    await pc("POST", "/v1/notify", offer());
    const token = tokenFrom(fake.messages.at(-1));
    await wh(inboundText(PHONE, "STOP"));
    // Even with the template approved: the number asked for no more messages.
    env.DB.raw.prepare("INSERT INTO templates (name, status, updated_at) VALUES ('ck_highlights_ready', 'APPROVED', ?)").run(Math.floor(Date.now() / 1000));
    const sent = fake.messages.length;
    let view = (await page(token)).text;
    assert.match(view, /value="defer">Remind me later<\/button>\s*<p class="small muted">Remind me later only puts this aside\. Nothing will remind you, so keep this link\.<\/p>/);
    assert.equal((await submit(token, [["csrf", csrfOf(view)], ["action", "defer"]])).status, 303);
    view = (await page(token)).text;
    assert.match(view, /<div class="card notice">Put aside for later\. Nothing will remind you, so keep this link\. You can still decide now\.<\/div>/);
    assert.doesNotMatch(view, /Reminder set|WhatsApp sends/);
    env.DB.raw.prepare("UPDATE approvals SET remind_at = ?").run(Math.floor(Date.now() / 1000) - 5);
    await cron(env);
    assert.equal(fake.messages.length, sent);
  });

  it("promises a WhatsApp reminder only when WhatsApp can send it then", async () => {
    await pc("POST", "/v1/notify", offer());
    const token = tokenFrom(fake.messages.at(-1));
    // The template still waiting for Meta, and the creator's last message 23 h
    // ago: in 2 h only the template could reach them, so nothing is promised.
    const t = Math.floor(Date.now() / 1000);
    env.DB.raw.prepare("INSERT INTO templates (name, status, updated_at) VALUES ('ck_highlights_ready', 'PENDING', ?)").run(t);
    env.DB.raw.prepare("UPDATE recipients SET last_inbound = ?").run(t - 23 * 3600);
    let view = (await page(token)).text;
    assert.match(view, /Remind me later only puts this aside\. Nothing will remind you, so keep this link\./);
    assert.equal((await submit(token, [["csrf", csrfOf(view)], ["action", "defer"]])).status, 303);
    assert.match((await page(token)).text, /Put aside for later\. Nothing will remind you, so keep this link\./);
    // A message from the creator now keeps a plain link open past the reminder.
    env.DB.raw.prepare("UPDATE recipients SET last_inbound = ?").run(t);
    assert.match((await page(token)).text, /Reminder set\. WhatsApp sends you this link again in about 2 h\./);
    // So does the template, once Meta approves it.
    env.DB.raw.prepare("UPDATE recipients SET last_inbound = ?").run(t - 23 * 3600);
    await handleWebhook(env, { entry: [{ changes: [{ field: "message_template_status_update", value: { message_template_name: "ck_highlights_ready", event: "APPROVED" } }] }] });
    assert.match((await page(token)).text, /Reminder set\. WhatsApp sends you this link again in about 2 h\./);
  });

  it("says on the read-only page what Remind me later did, without offering a decision", async () => {
    await pc("POST", "/v1/preferences", { remote_approvals: false });
    const sent = await pc("POST", "/v1/notify", offer());
    const msg = fake.messages.at(-1);
    await wh(inboundButton(PHONE, `REMIND:${sent.data.approval_id}`, msg.wamid));
    const view = (await page(tokenFrom(msg))).text;
    assert.match(view, /<div class="card notice">Reminder set\. WhatsApp sends you this link again in about 2 h\.<\/div>/);
    assert.match(view, /Approving from your phone is switched off/);
    assert.doesNotMatch(view, /You can still decide now|Approve and publish/);
  });

  it("never approves from a bare yes, and handles the quick replies", async () => {
    const sent = await pc("POST", "/v1/notify", offer());
    const msg = fake.messages.at(-1);
    await wh(inboundText(PHONE, "yes"));
    assert.match(fake.messages.at(-1).text.body, /open the Review clips link/);
    assert.equal((await commands()).filter((c) => c.kind === "approve").length, 0);
    await wh(inboundButton(PHONE, `REMIND:${sent.data.approval_id}`, msg.wamid));
    assert.match(fake.messages.at(-1).text.body, /remind you in about 2 hours/);
    let cmds = await commands();
    assert.ok(cmds.find((c) => c.kind === "defer" && c.remind_at));
    await wh(inboundButton(PHONE, `CANCEL:${sent.data.approval_id}`, msg.wamid));
    cmds = await commands();
    assert.ok(cmds.find((c) => c.kind === "cancel"));
    assert.match((await page(tokenFrom(msg))).text, /Cancelled\. Nothing was posted/);
  });

  it("ignores a quick reply tapped on a different message", async () => {
    const first = await pc("POST", "/v1/notify", offer());
    await pc("POST", "/v1/notify", offer({ event_key: "ready:job:8:1", review_ref: 4 }));
    const second = fake.messages.at(-1);
    await wh(inboundButton(PHONE, `CANCEL:${first.data.approval_id}`, second.wamid));
    assert.equal((await commands()).filter((c) => c.kind === "cancel").length, 0);
  });

  it("tells the PC about delivery, once, and about failures", async () => {
    await pc("POST", "/v1/notify", offer());
    const msg = fake.messages.at(-1);
    await wh(statusUpdate(msg.wamid, "delivered"));
    await wh(statusUpdate(msg.wamid, "read"));
    let delivered = (await commands()).filter((c) => c.kind === "delivery");
    assert.equal(delivered.length, 1);
    assert.equal(delivered[0].status, "delivered");
    await pc("POST", "/v1/notify", offer({ event_key: "ready:job:9:1", review_ref: 5 }));
    await wh(statusUpdate(fake.messages.at(-1).wamid, "failed", 131026));
    delivered = (await commands()).filter((c) => c.kind === "delivery" && c.status === "failed");
    assert.match(delivered[0].error, /could not deliver/);
  });

  it("expires offers and sends reminders on the cron", async () => {
    const sent = await pc("POST", "/v1/notify", offer());
    const msg = fake.messages.at(-1);
    await wh(inboundButton(PHONE, `REMIND:${sent.data.approval_id}`, msg.wamid));
    env.DB.raw.prepare("UPDATE approvals SET remind_at = ?").run(Math.floor(Date.now() / 1000) - 5);
    const before = fake.messages.filter((m) => m.type === "template").length;
    await cron(env);
    assert.equal(fake.messages.filter((m) => m.type === "template").length, before + 1);
    assert.equal(tokenFrom(fake.messages.at(-1)), tokenFrom(msg)); // the same link
    assert.ok((await commands()).find((c) => c.kind === "reminded"));
    env.DB.raw.prepare("UPDATE approvals SET expires_at = ?").run(Math.floor(Date.now() / 1000) - 5);
    await cron(env);
    assert.ok((await commands()).find((c) => c.kind === "expired"));
    assert.match((await page(tokenFrom(msg))).text, /expired/);
  });

  it("allows publishing steps only inside an accepted approval", async () => {
    const sent = await pc("POST", "/v1/notify", offer());
    const token = tokenFrom(fake.messages.at(-1));
    const csrf = csrfOf((await page(token)).text);
    await submit(token, [["csrf", csrf], ["action", "approve"], ["clip", "11"], ["platform", "instagram"], ["platform", "facebook"], ["confirm", "yes"], ["pin", "4826"]]);
    const approve = (await commands()).find((c) => c.kind === "approve");
    const ctx = { command_id: approve.id, approval_id: sent.data.approval_id, clip_id: 11 };

    // Not yet accepted by the PC: refused.
    let r = await pc("POST", "/v1/meta/ig_create", { ...ctx, caption: "x", file_size: 2048 });
    assert.equal(r.status, 403);
    await pc("POST", `/v1/commands/${approve.id}/ack`, { outcome: "accepted", reason: "1 clip queued" });

    // A clip or platform that was not approved: refused.
    assert.equal((await pc("POST", "/v1/meta/ig_create", { ...ctx, clip_id: 12, caption: "x", file_size: 2048 })).status, 403);

    r = await pc("POST", "/v1/meta/ig_create", { ...ctx, caption: "Poster! #basketball", file_size: 2048 });
    assert.equal(r.status, 200);
    const container = r.data.container_id;
    const bytes = randomBytes(2048);
    const q = new URLSearchParams({ ...Object.fromEntries(Object.entries(ctx).map(([k, v]) => [k, String(v)])), platform: "instagram", ref: container, offset: "0", file_size: "2048" });
    const up = await call(uploadRequest(`/v1/meta/upload?${q}`, bytes));
    assert.equal(up.status, 200, await up.clone().text());
    assert.equal(fake.uploads[0].bytes, 2048);
    assert.equal((await pc("POST", "/v1/meta/ig_status", { ...ctx, container_id: container })).data.status_code, "FINISHED");
    const media = (await pc("POST", "/v1/meta/ig_publish", { ...ctx, container_id: container })).data.media_id;
    const link = (await pc("POST", "/v1/meta/ig_permalink", { ...ctx, media_id: media })).data.permalink;
    assert.match(link, /^https:\/\/www\.instagram\.com\/reel\//);

    // A container from this command can't be used for another clip.
    assert.equal((await pc("POST", "/v1/meta/ig_status", { ...ctx, clip_id: 12, container_id: container })).status, 403);
  });

  it("shows only confirmed links from Meta's own hosts", async () => {
    const sent = await pc("POST", "/v1/notify", offer());
    const token = tokenFrom(fake.messages.at(-1));
    const csrf = csrfOf((await page(token)).text);
    await submit(token, [["csrf", csrf], ["action", "approve"], ["clip", "11"], ["platform", "instagram"], ["confirm", "yes"], ["pin", "4826"]]);
    const approve = (await commands()).find((c) => c.kind === "approve");
    await pc("POST", `/v1/commands/${approve.id}/ack`, { outcome: "accepted" });
    const res = await pc("POST", "/v1/notify", {
      kind: "result", event_key: "result:review:3:0", approval_id: sent.data.approval_id, text: "1 of 2 posts published (1 failed)",
      results: { clips: [{ clip_id: 11, title: "Poster", posts: [
        { platform: "instagram", state: "published", url: "https://www.instagram.com/reel/ABC/" },
        { platform: "facebook", state: "published", url: "https://evil.example/phish" },
      ] }] },
    });
    assert.equal(res.data.channel, "template");
    assert.equal(fake.messages.at(-1).template.name, "ck_publish_update");
    const view = (await page(token)).text;
    assert.ok(view.includes('href="https://www.instagram.com/reel/ABC/"'));
    assert.doesNotMatch(view, /evil\.example/);
  });

  it("tells the page when the PC refused an approval", async () => {
    await pc("POST", "/v1/notify", offer());
    const token = tokenFrom(fake.messages.at(-1));
    const csrf = csrfOf((await page(token)).text);
    await submit(token, [["csrf", csrf], ["action", "approve"], ["clip", "11"], ["platform", "instagram"], ["confirm", "yes"], ["pin", "4826"]]);
    const approve = (await commands()).find((c) => c.kind === "approve");
    await pc("POST", `/v1/commands/${approve.id}/ack`, { outcome: "rejected", reason: "A clip changed after the message was sent" });
    assert.match((await page(token)).text, /did not publish this: A clip changed/);
    assert.equal((await commands()).filter((c) => c.kind === "approve").length, 0);
  });

  it("words the page for any video and says where a clip is already posted", async () => {
    const clips = offer().clips.map((c) => ({ ...c, label: "", category: "highlight" }));
    clips[1].published = ["instagram", "tiktok", "instagram"];
    await pc("POST", "/v1/notify", offer({ title: "", clips }));
    const view = (await page(tokenFrom(fake.messages.at(-1)))).text;
    assert.match(view, /<h1>your video<\/h1>/);
    assert.match(view, /<span class="pill">highlight<\/span>/);
    assert.match(view, /Already posted to Instagram Reels, so it is not posted there again\./);
    assert.equal(view.match(/Already posted to/g).length, 1);
    assert.doesNotMatch(view, /tiktok|🏀/i);
    assert.match(view, /<div class="ph" aria-hidden="true">&#9654;<\/div>/); // no thumbnail: a neutral placeholder
  });

  it("keeps the approval page read-only when remote approval is off", async () => {
    await pc("POST", "/v1/preferences", { remote_approvals: false });
    await pc("POST", "/v1/notify", offer());
    const view = (await page(tokenFrom(fake.messages.at(-1)))).text;
    assert.match(view, /switched off/);
    assert.doesNotMatch(view, /Approve and publish/);
  });

  it("lists only the connected accounts and their texts, and refuses a forged tick for another", async () => {
    await pc("POST", "/v1/accounts/disconnect", { platform: "instagram" });
    await pc("POST", "/v1/notify", offer());
    const token = tokenFrom(fake.messages.at(-1));
    const view = (await page(token)).text;
    assert.match(view, /name="platform" value="facebook">/);
    assert.match(view, /<pre><b>Facebook<\/b>\nPoster!<\/pre>/);
    assert.doesNotMatch(view, /instagram|not connected|disabled/i);
    // A place that isn't connected, and one the relay doesn't post to at all.
    for (const forged of ["instagram", "threads"]) {
      const r = await submit(token, [["csrf", csrfOf(view)], ["action", "approve"], ["clip", "11"], ["platform", "facebook"], ["platform", forged], ["confirm", "yes"], ["pin", "4826"]]);
      assert.equal(r.status, 400, forged);
      assert.match(r.text, /not connected anymore/);
      assert.doesNotMatch(r.text, new RegExp(`value="${forged}"`));
    }
    assert.equal((await commands()).filter((c) => c.kind === "approve").length, 0);
    // Refused with the right PIN: the ticks come back as sent, and the try is
    // not counted as a wrong PIN, so six in a row don't lock the phone.
    const stale = [["csrf", csrfOf(view)], ["action", "approve"], ["clip", "11"], ["platform", "facebook"], ["platform", "instagram"], ["confirm", "yes"], ["pin", "4826"]];
    let r;
    for (let i = 0; i < 6; i++) {
      r = await submit(token, stale);
      assert.equal(r.status, 400, `try ${i + 1}`);
      assert.deepEqual(ticks(r.text), [["clip", "11"], ["platform", "facebook"], ["confirm", "yes"]]);
    }
    r = await submit(token, [["csrf", csrfOf(view)], ["action", "approve"], ["clip", "11"], ["confirm", "yes"], ["pin", "4826"]]);
    assert.equal(r.status, 400);
    assert.match(r.text, /Tick at least one place to publish/);
    r = await submit(token, [["csrf", csrfOf(view)], ["action", "approve"], ...ticks(r.text), ["platform", "facebook"], ["pin", "4826"]]);
    assert.equal(r.status, 303);
    const approves = (await commands()).filter((c) => c.kind === "approve");
    assert.deepEqual(approves.map((c) => [c.clips, c.platforms]), [[[11], ["facebook"]]]);
  });

  it("says to connect an account first when none is connected", async () => {
    await pc("POST", "/v1/accounts/disconnect", { platform: "facebook" });
    await pc("POST", "/v1/notify", offer());
    const view = (await page(tokenFrom(fake.messages.at(-1)))).text;
    assert.match(view, /No account is connected yet\. Connect one in Clips Kitty on your PC, then open this link again\./);
    assert.doesNotMatch(view, /name="platform"|What will be posted|Instagram|Facebook/);
  });
});

describe("accounts", () => {
  it("connects Facebook (with Instagram) and never returns a token", async () => {
    const { data } = await pc("POST", "/v1/accounts/connect", { platform: "facebook" });
    const url = new URL(data.url);
    assert.equal(url.hostname, "www.facebook.com");
    assert.match(url.searchParams.get("scope"), /instagram_content_publish/);
    const state = url.searchParams.get("state");
    let res = await call(new Request(`${BASE}/oauth/facebook/callback?state=${state}&code=abc`));
    assert.match(await res.text(), /Hoops Daily/);
    res = await call(new Request(`${BASE}/oauth/facebook/callback?state=${state}&code=abc`));
    assert.equal(res.status, 400); // a state is single use
    const status = (await pc("GET", "/v1/status")).data;
    assert.deepEqual(status.accounts.instagram, { connected: true, name: "@hoopsdaily" });
    assert.equal(status.accounts.facebook.name, "Hoops Daily");
    assert.doesNotMatch(JSON.stringify(status), /page-token|fb-long/);
    const row = env.DB.raw.prepare("SELECT token_enc FROM accounts WHERE platform = 'facebook'").get();
    assert.doesNotMatch(row.token_enc, /page-token/);
  });

  it("checks templates and creates the missing ones", async () => {
    const res = await pc("POST", "/v1/templates", {});
    assert.equal(Object.keys(res.data.templates).length, 4);
    assert.equal(fake.templates.length, 4);
    const ready = fake.templates.find((t) => t.name === "ck_highlights_ready");
    const body = ready.components.find((c) => c.type === "BODY").text;
    assert.doesNotMatch(body, /^\{\{|\}\}$/);
    assert.equal(body, "Clips Kitty finished your video {{1}}. Ready to review: {{2}}. Choose where to publish them before the link expires.");
    assert.equal(ready.category, "UTILITY");
    const failed = fake.templates.find((t) => t.name === "ck_processing_failed");
    assert.equal(failed.components[0].text, "Clips Kitty could not finish processing your video. Open the app for details.");
    // Every video, not only basketball: no template says what kind it was.
    assert.doesNotMatch(JSON.stringify(fake.templates), /basketball|dunk/i);
  });
});

describe("webhook helpers", () => {
  it("records template status updates", async () => {
    await handleWebhook(env, { entry: [{ changes: [{ field: "message_template_status_update", value: { message_template_name: "ck_highlights_ready", event: "APPROVED" } }] }] });
    assert.equal((await pc("GET", "/v1/status")).data.templates.ck_highlights_ready, "APPROVED");
  });
});
