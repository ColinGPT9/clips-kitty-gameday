/**
 * The relay on this machine, with Meta faked: for the end-to-end test and
 * the demo (scripts/meta_demo.py), and for trying the approval page in a
 * browser without a Cloudflare or Meta account.
 *
 *   npm run dev                                  # http://127.0.0.1:8787
 *   PORT=9000 ENROLL_KEY=... npm run dev
 *   PREVIEWS=0 npm run dev                       # a relay without KV: thumbnails only
 *
 * (npm run dev adds --experimental-sqlite, which node:sqlite needs before
 * Node 22.13.)
 *
 * It runs the real worker.js. Only its surroundings are stand-ins: D1 is
 * node:sqlite, KV (the clips' preview videos) is a Map, and
 * graph.facebook.com / rupload are the FakeMeta in harness.mjs, so nothing
 * is sent to WhatsApp or posted anywhere.
 * /__mock/* plays the phone and the browser:
 *
 *   GET  /__mock/state                 messages sent, uploads, posts
 *   POST /__mock/phone   {text}        the creator writes to the business number
 *   POST /__mock/tap     {payload, context}   a quick-reply button
 *   POST /__mock/status  {wamid, status, code}  a delivery status
 *   POST /__mock/oauth   {url}         finish an OAuth sign-in the PC started
 *   POST /__mock/cron                  run the 10-minute job now
 *   POST /__mock/clock   {offset}      move the relay's clock (seconds)
 */

import http from "node:http";
import { Readable } from "node:stream";

import worker, { cron, handleWebhook } from "../worker.js";
import { FakeMeta, inboundButton, inboundText, makeCtx, makeEnv, statusUpdate } from "./harness.mjs";

const PORT = Number(process.env.PORT || 8787);
const PHONE = process.env.MOCK_PHONE || "15551234567";
const PUBLIC_URL = `http://127.0.0.1:${PORT}`;

const env = makeEnv({
  PUBLIC_URL,
  ...(process.env.ENROLL_KEY ? { ENROLL_KEY: process.env.ENROLL_KEY } : {}),
  ...(process.env.PREVIEWS === "0" ? { PREVIEWS: undefined } : {}),
});
const fake = new FakeMeta(globalThis.fetch);
globalThis.fetch = fake.fetch;

const realNow = Date.now;
let offset = 0;
Date.now = () => realNow() + offset * 1000;

async function send(req, res, response) {
  res.writeHead(response.status, Object.fromEntries(response.headers));
  res.end(Buffer.from(await response.arrayBuffer()));
}

async function readJson(req) {
  const chunks = [];
  for await (const c of req) chunks.push(c);
  try {
    return JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}");
  } catch {
    return {};
  }
}

function reply(res, data, status = 200) {
  res.writeHead(status, { "Content-Type": "application/json" });
  res.end(JSON.stringify(data));
}

async function mock(req, res, path) {
  if (path === "/__mock/state") {
    return reply(res, { messages: fake.messages, posts: fake.posts, uploads: fake.uploads, templates: fake.templates, phone: PHONE });
  }
  const body = await readJson(req);
  if (path === "/__mock/phone") {
    await handleWebhook(env, inboundText(body.from || PHONE, String(body.text || "")));
    return reply(res, { ok: true, last: fake.messages.at(-1) || null });
  }
  if (path === "/__mock/tap") {
    await handleWebhook(env, inboundButton(body.from || PHONE, String(body.payload || ""), String(body.context || "")));
    return reply(res, { ok: true, last: fake.messages.at(-1) || null });
  }
  if (path === "/__mock/status") {
    await handleWebhook(env, statusUpdate(String(body.wamid || ""), String(body.status || "delivered"), body.code));
    return reply(res, { ok: true });
  }
  if (path === "/__mock/oauth") {
    const url = new URL(String(body.url || ""));
    const state = url.searchParams.get("state");
    const ctx = makeCtx();
    const out = await worker.fetch(new Request(`${PUBLIC_URL}/oauth/facebook/callback?state=${state}&code=mock-code`), env, ctx);
    await ctx.settle();
    return reply(res, { ok: out.status === 200, status: out.status });
  }
  if (path === "/__mock/cron") {
    await cron(env);
    return reply(res, { ok: true });
  }
  if (path === "/__mock/clock") {
    offset = Number(body.offset || 0);
    return reply(res, { ok: true, offset });
  }
  return reply(res, { error: "unknown mock route" }, 404);
}

const server = http.createServer(async (req, res) => {
  try {
    const path = new URL(req.url, PUBLIC_URL).pathname;
    if (path.startsWith("/__mock/")) return await mock(req, res, path);
    const hasBody = !["GET", "HEAD"].includes(req.method);
    const request = new Request(new URL(req.url, PUBLIC_URL), {
      method: req.method,
      headers: Object.entries(req.headers).flatMap(([k, v]) => (Array.isArray(v) ? v.map((x) => [k, x]) : [[k, v]])),
      body: hasBody ? Readable.toWeb(req) : undefined,
      duplex: hasBody ? "half" : undefined,
    });
    const ctx = makeCtx();
    const response = await worker.fetch(request, env, ctx);
    await ctx.settle();
    await send(req, res, response);
  } catch (e) {
    console.error("serve error", e);
    reply(res, { error: "serve error" }, 500);
  }
});

server.listen(PORT, "127.0.0.1", () => {
  console.log(`Clips Kitty relay (mock Meta) on ${PUBLIC_URL}`);
  console.log(`Enrolment key: ${env.ENROLL_KEY}`);
});
