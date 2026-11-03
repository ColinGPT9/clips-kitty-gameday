/**
 * Meta accounts (Facebook Login for Instagram and Pages) and the Graph steps
 * of a publish.
 *
 * The app secret is used here and nowhere else: the code-for-token exchanges
 * and appsecret_proof on every Graph call. Access tokens are stored sealed
 * (AES-GCM) and never leave this worker; the PC asks for a step by name and
 * gets back ids, statuses and permalinks.
 */

import { hmacHex, randomToken, seal, unseal } from "./crypto.js";
import { all, now, one, parse, run } from "./db.js";

export const FB_SCOPES = [
  "pages_show_list", "pages_read_engagement", "pages_manage_posts",
  "instagram_basic", "instagram_content_publish", "business_management",
];

export function graph(env) {
  return (env.GRAPH_BASE || "https://graph.facebook.com/v26.0").replace(/\/$/, "");
}
function rupload(env) {
  return (env.RUPLOAD_BASE || "https://rupload.facebook.com").replace(/\/$/, "");
}
function version(env) {
  return env.GRAPH_VERSION || "v26.0";
}

export class MetaError extends Error {
  constructor(code, message, { metaCode = 0, subcode = 0, platform = "", transient = false } = {}) {
    super(message);
    this.code = code;
    this.metaCode = metaCode;
    this.subcode = subcode;
    this.platform = platform;
    this.transient = transient;
  }
}

async function call(env, url, { method = "GET", form = null, token = "", platform = "", proof = true } = {}) {
  const u = new URL(url);
  const body = form ? new URLSearchParams() : null;
  const put = (k, v) => (body ? body.set(k, v) : u.searchParams.set(k, v));
  if (form) for (const [k, v] of Object.entries(form)) if (v !== undefined && v !== null && v !== "") body.set(k, String(v));
  if (token) {
    put("access_token", token);
    if (proof && env.META_APP_SECRET) put("appsecret_proof", await hmacHex(env.META_APP_SECRET, token));
  }
  const res = await fetch(u.toString(), { method, body });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.error) {
    const e = data.error || {};
    console.error("graph refused", platform, e.code, e.error_subcode);
    throw new MetaError("meta_error", friendly(platform, e), {
      metaCode: Number(e.code || 0), subcode: Number(e.error_subcode || 0), platform,
      transient: Boolean(e.is_transient) || [1, 2, 4, 17, 341].includes(Number(e.code)),
    });
  }
  return data;
}

function friendly(platform, e) {
  const name = { instagram: "Instagram", facebook: "Facebook" }[platform] || "Meta";
  if (Number(e.code) === 190) return `${name} sign-in has expired. Reconnect it in Clips Kitty.`;
  if (Number(e.code) === 10 || (Number(e.code) >= 200 && Number(e.code) < 300)) return `${name} refused the permission this needs.`;
  return `${name} refused the request.`;
}

// ---- connecting accounts ---------------------------------------------------

export async function startConnect(env, device, platform) {
  if (platform !== "facebook") throw new MetaError("bad_request", "Unknown platform.");
  const state = randomToken(24);
  await run(env, "INSERT INTO oauth_states (state, device_id, platform, expires_at) VALUES (?, ?, ?, ?)",
    state, device, platform, now() + 600);
  const redirect = `${env.PUBLIC_URL}/oauth/${platform}/callback`;
  if (!env.META_APP_ID) throw new MetaError("not_configured", "META_APP_ID is not set on the relay.");
  const u = new URL(`https://www.facebook.com/${version(env)}/dialog/oauth`);
  u.searchParams.set("client_id", env.META_APP_ID);
  u.searchParams.set("redirect_uri", redirect);
  u.searchParams.set("state", state);
  u.searchParams.set("response_type", "code");
  u.searchParams.set("scope", FB_SCOPES.join(","));
  return u.toString();
}

async function takeState(env, state, platform) {
  const row = await one(env, "SELECT * FROM oauth_states WHERE state = ? AND platform = ?", String(state || ""), platform);
  if (!row || row.expires_at < now()) return null;
  return row;
}

/** Facebook Login: code -> long-lived user token -> Pages (with their Instagram accounts). */
export async function finishFacebook(env, state, code) {
  const row = await takeState(env, state, "facebook");
  if (!row) throw new MetaError("expired", "This sign-in link has expired. Start again from Clips Kitty.");
  await run(env, "DELETE FROM oauth_states WHERE state = ?", state);
  const redirect = `${env.PUBLIC_URL}/oauth/facebook/callback`;
  const short = await call(env, `${graph(env)}/oauth/access_token?client_id=${enc(env.META_APP_ID)}&redirect_uri=${enc(redirect)}&client_secret=${enc(env.META_APP_SECRET)}&code=${enc(code)}`, { platform: "facebook" });
  const long = await call(env, `${graph(env)}/oauth/access_token?grant_type=fb_exchange_token&client_id=${enc(env.META_APP_ID)}&client_secret=${enc(env.META_APP_SECRET)}&fb_exchange_token=${enc(short.access_token)}`, { platform: "facebook" });
  const pages = await call(env, `${graph(env)}/me/accounts?fields=id,name,access_token,instagram_business_account{id,username}&limit=50`, { token: long.access_token, platform: "facebook" });
  const list = (pages.data || []).filter((p) => p.id && p.access_token);
  if (!list.length) throw new MetaError("no_pages", "No Facebook Page came back. Choose at least one Page when Facebook asks.");
  if (list.length === 1) {
    await savePage(env, row.device_id, list[0]);
    return { device: row.device_id, chose: describePage(list[0]) };
  }
  // More than one Page: the creator picks one on the next screen.
  const choice = randomToken(24);
  await run(env, "INSERT INTO oauth_states (state, device_id, platform, pending, expires_at) VALUES (?, ?, ?, ?, ?)",
    choice, row.device_id, "facebook_choose", await seal(env, JSON.stringify(list)), now() + 600);
  return { device: row.device_id, choose: { state: choice, pages: list.map(describePage) } };
}

export async function choosePage(env, state, pageId) {
  const row = await takeState(env, state, "facebook_choose");
  if (!row) throw new MetaError("expired", "This sign-in has expired. Start again from Clips Kitty.");
  const list = parse(await unseal(env, row.pending), []);
  const page = list.find((p) => p.id === String(pageId));
  if (!page) throw new MetaError("bad_request", "That Page was not one of the choices.");
  await run(env, "DELETE FROM oauth_states WHERE state = ?", state);
  await savePage(env, row.device_id, page);
  return { device: row.device_id, chose: describePage(page) };
}

function describePage(p) {
  const ig = p.instagram_business_account;
  return { id: p.id, name: p.name || "", instagram: ig ? `@${ig.username || ig.id}` : "" };
}

async function savePage(env, device, page) {
  // A Page token made from a long-lived user token does not expire. It
  // publishes to the Page and to the Instagram account linked to it.
  const sealed = await seal(env, page.access_token);
  await upsertAccount(env, device, "facebook", page.id, page.name || "Facebook Page", sealed, 0);
  const ig = page.instagram_business_account;
  if (ig && ig.id) {
    await upsertAccount(env, device, "instagram", ig.id, `@${ig.username || ig.id}`, sealed, 0);
  } else {
    await run(env, "DELETE FROM accounts WHERE device_id = ? AND platform = 'instagram'", device);
  }
}

async function upsertAccount(env, device, platform, id, name, tokenEnc, expires) {
  await run(env, `INSERT INTO accounts (device_id, platform, account_id, name, token_enc, expires_at, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(device_id, platform) DO UPDATE SET account_id = excluded.account_id,
    name = excluded.name, token_enc = excluded.token_enc, expires_at = excluded.expires_at, updated_at = excluded.updated_at`,
  device, platform, String(id), name, tokenEnc, expires, now());
}

export async function accountsFor(env, device) {
  const rows = await all(env, "SELECT platform, name, expires_at FROM accounts WHERE device_id = ?", device);
  const out = {
    instagram: { connected: false, name: "" },
    facebook: { connected: false, name: "" },
  };
  for (const r of rows) {
    if (!out[r.platform]) continue; // only the places above
    const expired = r.expires_at && r.expires_at < now();
    out[r.platform] = { connected: !expired, name: r.name, ...(expired ? { expired: true } : {}) };
  }
  return out;
}

export async function disconnect(env, device, platform) {
  const which = platform === "facebook" ? ["facebook", "instagram"] : [platform];
  for (const p of which) await run(env, "DELETE FROM accounts WHERE device_id = ? AND platform = ?", device, p);
}

// ---- publishing: one Graph step per call -------------------------------------

async function account(env, device, platform) {
  const row = await one(env, "SELECT * FROM accounts WHERE device_id = ? AND platform = ?", device, platform);
  if (!row) throw new MetaError("not_connected", `${platform[0].toUpperCase()}${platform.slice(1)} is not connected on the relay.`, { platform });
  if (row.expires_at && row.expires_at < now()) throw new MetaError("token_expired", "That account's sign-in has expired. Reconnect it.", { platform, metaCode: 190 });
  return { id: row.account_id, token: await unseal(env, row.token_enc) };
}

async function ref(env, value, cmd, platform, kind) {
  const row = await one(env, "SELECT * FROM graph_refs WHERE ref = ?", `${platform}:${kind}:${value}`);
  if (!row || row.command_id !== cmd.command.id || row.clip_id !== cmd.clipId) {
    throw new MetaError("not_approved", "That upload does not belong to this approval.", { platform });
  }
  return row;
}

async function bind(env, cmd, platform, kind, value, fileSize = 0, uri = "") {
  await run(env, "INSERT OR IGNORE INTO graph_refs (ref, command_id, clip_id, platform, kind, file_size, upload_uri, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
    `${platform}:${kind}:${value}`, cmd.command.id, cmd.clipId, platform, kind, fileSize, uri, now());
}

const MAX_CREATES = 3; // per clip, platform and command: a retry, not a loop

async function creates(env, cmd, platform, kind) {
  const row = await one(env, "SELECT COUNT(*) AS n FROM graph_refs WHERE command_id = ? AND clip_id = ? AND platform = ? AND kind = ?",
    cmd.command.id, cmd.clipId, platform, kind);
  if ((row ? row.n : 0) >= MAX_CREATES) throw new MetaError("too_many", "Too many attempts for this clip under one approval.", { platform });
}

/** The steps publish/meta_graph.py takes, by name. `cmd` is the approved command. */
export const STEPS = {
  async ig_create(env, cmd, p) {
    await creates(env, cmd, "instagram", "container");
    const acc = await account(env, cmd.device, "instagram");
    const made = await call(env, `${graph(env)}/${acc.id}/media`, {
      method: "POST", token: acc.token, platform: "instagram",
      form: { media_type: "REELS", upload_type: "resumable", caption: String(p.caption || "").slice(0, 2200) },
    });
    await bind(env, cmd, "instagram", "container", made.id, Number(p.file_size) || 0, made.uri || `${rupload(env)}/ig-api-upload/${version(env)}/${made.id}`);
    return { container_id: String(made.id) };
  },
  async ig_status(env, cmd, p) {
    await ref(env, p.container_id, cmd, "instagram", "container");
    const acc = await account(env, cmd.device, "instagram");
    const s = await call(env, `${graph(env)}/${p.container_id}?fields=status_code,status`, { token: acc.token, platform: "instagram" });
    return { status_code: s.status_code || "", status: String(s.status || "").slice(0, 120) };
  },
  async ig_publish(env, cmd, p) {
    await ref(env, p.container_id, cmd, "instagram", "container");
    const acc = await account(env, cmd.device, "instagram");
    const out = await call(env, `${graph(env)}/${acc.id}/media_publish`, { method: "POST", token: acc.token, platform: "instagram", form: { creation_id: p.container_id } });
    await bind(env, cmd, "instagram", "media", out.id);
    return { media_id: String(out.id || "") };
  },
  async ig_permalink(env, cmd, p) {
    await ref(env, p.media_id, cmd, "instagram", "media");
    const acc = await account(env, cmd.device, "instagram");
    const m = await call(env, `${graph(env)}/${p.media_id}?fields=permalink,shortcode`, { token: acc.token, platform: "instagram" });
    return { permalink: safeLink(m.permalink), shortcode: m.shortcode || "" };
  },
  async fb_start(env, cmd, p) {
    await creates(env, cmd, "facebook", "video");
    const acc = await account(env, cmd.device, "facebook");
    const s = await call(env, `${graph(env)}/${acc.id}/video_reels`, { method: "POST", token: acc.token, platform: "facebook", form: { upload_phase: "start" } });
    await bind(env, cmd, "facebook", "video", s.video_id, Number(p.file_size) || 0, s.upload_url || `${rupload(env)}/video-upload/${version(env)}/${s.video_id}`);
    return { video_id: String(s.video_id) };
  },
  async fb_finish(env, cmd, p) {
    await ref(env, p.video_id, cmd, "facebook", "video");
    const acc = await account(env, cmd.device, "facebook");
    const out = await call(env, `${graph(env)}/${acc.id}/video_reels`, {
      method: "POST", token: acc.token, platform: "facebook",
      form: { upload_phase: "finish", video_id: p.video_id, video_state: "PUBLISHED", title: String(p.title || "").slice(0, 100), description: String(p.description || "").slice(0, 2000) },
    });
    return { success: Boolean(out.success), post_id: String(out.post_id || "") };
  },
  async fb_status(env, cmd, p) {
    await ref(env, p.video_id, cmd, "facebook", "video");
    const acc = await account(env, cmd.device, "facebook");
    const v = await call(env, `${graph(env)}/${p.video_id}?fields=status,permalink_url,post_id`, { token: acc.token, platform: "facebook" });
    const st = v.status || {};
    let link = String(v.permalink_url || "");
    if (link.startsWith("/")) link = `https://www.facebook.com${link}`;
    return {
      video_status: String(st.video_status || ""),
      publish_status: String((st.publishing_phase && st.publishing_phase.publish_status) || ""),
      permalink_url: safeLink(link),
      post_id: String(v.post_id || ""),
    };
  },
};

export const STEP_PLATFORM = {
  ig_create: "instagram", ig_status: "instagram", ig_publish: "instagram", ig_permalink: "instagram",
  fb_start: "facebook", fb_finish: "facebook", fb_status: "facebook",
};

/** Stream one upload to Meta's upload host, with the account's token. */
export async function upload(env, cmd, platform, value, offset, fileSize, body, length) {
  const kind = platform === "instagram" ? "container" : "video";
  const row = await ref(env, value, cmd, platform, kind);
  if (row.file_size && row.file_size !== fileSize) throw new MetaError("bad_request", "The file size does not match the upload that was opened.", { platform });
  if (row.uploaded) throw new MetaError("bad_request", "This clip was already uploaded for this approval.", { platform });
  const acc = await account(env, cmd.device, platform);
  let stream = body;
  if (typeof FixedLengthStream === "function" && length) {
    // Cloudflare: keep a Content-Length on the way out instead of chunked encoding.
    const fixed = new FixedLengthStream(length);
    body.pipeTo(fixed.writable).catch(() => {});
    stream = fixed.readable;
  }
  const res = await fetch(row.upload_uri, {
    method: "POST",
    headers: {
      Authorization: `OAuth ${acc.token}`,
      offset: String(offset),
      file_size: String(fileSize),
      "Content-Type": "application/octet-stream",
    },
    body: stream,
    duplex: "half",
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.success === false || data.debug_info) {
    console.error("upload refused", platform, res.status, data.debug_info && data.debug_info.type);
    throw new MetaError("meta_error", `${platform === "instagram" ? "Instagram" : "Facebook"} did not accept the upload.`, {
      platform, transient: Boolean(data.debug_info && data.debug_info.retriable),
    });
  }
  await run(env, "UPDATE graph_refs SET uploaded = 1 WHERE ref = ?", row.ref);
  return { success: true };
}

/** Only links on Meta's own hosts are ever shown or sent. */
export function safeLink(url) {
  try {
    const u = new URL(String(url || ""));
    const ok = ["instagram.com", "www.instagram.com", "facebook.com", "www.facebook.com", "m.facebook.com", "fb.watch"];
    return u.protocol === "https:" && ok.includes(u.hostname) ? u.toString() : "";
  } catch {
    return "";
  }
}

function enc(v) {
  return encodeURIComponent(String(v ?? ""));
}
