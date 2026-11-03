/**
 * The approval page a WhatsApp link opens. Plain HTML and a form: no
 * script, no external resource, nothing to load but this page, its
 * thumbnails and the clips' preview videos, which play in the browser's own
 * player. A GET never changes anything (link previews and crawlers are
 * harmless); a decision is a POST carrying the page's CSRF token, and an
 * approval also needs the creator's PIN and an explicit "post publicly" tick.
 */

import { safeLink } from "./meta.js";

const LABELS = { instagram: "Instagram Reels", facebook: "Facebook Page Reels" };
const NAMES = { instagram: "Instagram", facebook: "Facebook" };

/** What the page says once wrong PINs have blocked approving from the phone (worker.js pinBlocked). */
export const PIN_BLOCKED = "Approving from your phone is blocked after too many wrong PINs. Set a new PIN in Clips Kitty on your PC to unblock it. Remind me later and Cancel still work.";

export function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

const STYLE = `
:root{color-scheme:light dark;--bg:#0f1115;--card:#181b22;--text:#f2f3f5;--muted:#a3a9b6;--line:#2a2f3a;--accent:#e4572e;--ok:#2fbf71;--bad:#ef4444}
@media (prefers-color-scheme: light){:root{--bg:#f6f7f9;--card:#fff;--text:#14161a;--muted:#5b6170;--line:#e3e6ec}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:16px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:560px;margin:0 auto;padding:16px}
h1{font-size:1.3rem;margin:8px 0 4px}h2{font-size:1rem;margin:20px 0 8px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px;margin:10px 0}
.muted{color:var(--muted)}.small{font-size:.85rem}
.clip{display:flex;gap:12px;align-items:flex-start}.clip img{width:72px;height:128px;object-fit:cover;border-radius:8px;background:#000;flex:none}
.clip .ph{width:72px;height:128px;border-radius:8px;background:linear-gradient(160deg,#2a2f3a,#111);flex:none;display:flex;align-items:center;justify-content:center;font-size:1.6rem}
.clip video{width:96px;height:170px;object-fit:cover;border-radius:8px;background:#000;flex:none}
@media (pointer:fine){.clip video{width:144px;height:256px}}
label.row{display:flex;gap:10px;align-items:flex-start;cursor:pointer}input[type=checkbox]{width:22px;height:22px;margin-top:2px;accent-color:var(--accent);flex:none}
details{margin-top:6px}summary{cursor:pointer;color:var(--muted);font-size:.85rem}pre{white-space:pre-wrap;font:inherit;font-size:.85rem;margin:6px 0 0}
.pill{display:inline-block;padding:2px 8px;border-radius:999px;background:var(--line);font-size:.75rem;margin-right:4px}
input[type=password]{width:100%;font-size:1.3rem;padding:10px 12px;border-radius:10px;border:1px solid var(--line);background:var(--bg);color:var(--text);letter-spacing:.3em}
.buttons{display:grid;gap:10px;margin:16px 0 28px}.buttons p{margin:0}
button{font:inherit;font-weight:600;padding:14px;border-radius:12px;border:1px solid var(--line);background:var(--card);color:var(--text)}
button.go{background:var(--accent);border-color:var(--accent);color:#fff}
.ok{color:var(--ok)}.bad{color:var(--bad)}.notice{border-left:4px solid var(--accent)}
a{color:var(--accent)}footer{color:var(--muted);font-size:.75rem;text-align:center;margin:24px 0}
`;

function shell(title, body, refresh = 0) {
  return `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">${refresh ? `<meta http-equiv="refresh" content="${refresh}">` : ""}
<title>${esc(title)}</title><style>${STYLE}</style></head>
<body><main>${body}<footer>Clips Kitty remote approval. Nothing is posted without your approval on this page.</footer></main></body></html>`;
}

export function message(title, text, { tone = "" } = {}) {
  return shell(title, `<h1>${esc(title)}</h1><div class="card notice"><p class="${tone}">${esc(text)}</p></div>`);
}

function left(expiresAt, nowS) {
  const s = Math.max(0, expiresAt - nowS);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return h ? `${h} h ${m} min` : `${m} min`;
}

/** A wait, to the nearest 5 minutes: "2 h", "1 h 25 min", "40 min". */
function about(seconds) {
  const mins = Math.max(5, Math.round(seconds / 300) * 5);
  const h = Math.floor(mins / 60);
  const m = mins % 60;
  return h ? `${h} h${m ? ` ${m} min` : ""}` : `${m} min`;
}

/**
 * What brings an offer back after Remind me later, as worker.js laterChannel
 * found it. Only WhatsApp sends the link again, and only when it can send it
 * then. The relay can't message Muse, which reads offers only when it calls
 * in, so the page promises only that it gives the link when asked. Otherwise
 * nothing does. A wait, never a clock time: the relay doesn't know the
 * phone's time zone.
 */
function noReminder(later) {
  return later === "muse"
    ? "Clips Kitty will not remind you. Your Muse agent can give you this link again when you ask it."
    : "Nothing will remind you, so keep this link.";
}

function deferredText(later, approval, nowS, canDecide) {
  const wait = approval.remind_at - nowS;
  const text = later === "whatsapp"
    ? `Reminder set. WhatsApp sends you this link again ${wait > 0 ? `in about ${about(wait)}` : "in a few minutes"}.`
    : `Put aside for later. ${noReminder(later)}`;
  return canDecide ? `${text} You can still decide now.` : text;
}

function clipCard(c, token, { selectable, thumbs, previews, checked = true, places = [] }) {
  const thumb = `/a/${esc(token)}/thumb/${esc(c.clip_id)}`;
  let pic = thumbs.has(c.clip_id)
    ? `<img src="${thumb}" alt="">`
    : `<div class="ph" aria-hidden="true">&#9654;</div>`;
  if (previews.has(c.clip_id)) {
    // The browser's own player, with nothing loaded until it plays: no
    // playsinline, so an iPhone plays it fullscreen.
    pic = `<video controls preload="none"${thumbs.has(c.clip_id) ? ` poster="${thumb}"` : ""} src="/a/${esc(token)}/preview/${esc(c.clip_id)}" aria-label="Play ${esc(c.title || c.label || "Highlight")}">Your browser can't play this clip here.</video>`;
  }
  const posted = (c.published || []).map((p) => LABELS[p]).filter(Boolean);
  const already = posted.length ? `<br><span class="small muted">Already posted to ${esc(posted.join(", "))}, so it is not posted there again.</span>` : "";
  const texts = c.texts || {};
  // Only the connected accounts' texts: nothing else can be posted.
  const preview = places.length ? `<details><summary>What will be posted</summary>
<pre>${places.map((p) => `<b>${NAMES[p]}</b>\n${esc(texts[p])}`).join("\n\n")}</pre>
<p class="small muted">${c.text_source === "muse_spark" ? "Written by Muse Spark from what Clips Kitty found." : "From the clip's own title and hashtags."}</p></details>` : "";
  const head = `<div><b>${esc(c.title || c.label || "Highlight")}</b><br>
<span class="pill">${esc(c.label || c.category || "highlight")}</span><span class="small muted">${esc(Math.round(c.duration || 0))} s</span>${already}${preview}</div>`;
  if (!selectable) return `<div class="card clip">${pic}${head}</div>`;
  return `<label class="card row clip"><input type="checkbox" name="clip" value="${esc(c.clip_id)}"${checked ? " checked" : ""}>${pic}${head}</label>`;
}

/**
 * The page for an offer, in whatever state it is in. `later` is what can
 * bring it back after Remind me later (worker.js laterChannel): "whatsapp",
 * "muse" or "" for nothing. `ticked` is what the person ticked in the post
 * this page answers ({clips, platforms, confirm}), shown again as they left
 * it; without it, every clip, the preferred places and no confirm.
 * `previews` are the clips whose preview video the relay keeps
 * (worker.js uploadPreview): they play in place of their thumbnails.
 */
export function approvalPage({ approval, payload, token, csrf, accounts, thumbs, previews = new Set(), nowS, error = "", blocked = false, later = "", ticked = null }) {
  const title = payload.title || "Your video";
  const head = `<h1>${esc(title)}</h1><p class="muted">${esc(payload.summary_text || "")}</p>${payload.muse_line ? `<p class="small">${esc(payload.muse_line)} <span class="pill">Muse Spark</span></p>` : ""}`;
  const clips = payload.clips || [];
  const state = approval.state;

  if (state === "expired" || (["open", "deferred"].includes(state) && approval.expires_at <= nowS)) {
    return shell(title, `${head}<div class="card notice">This link has expired, so nothing can be approved from it. Open Clips Kitty on your PC to send a new one.</div>`);
  }
  if (state === "cancelled") {
    return shell(title, `${head}<div class="card notice">Cancelled. Nothing was posted.</div>`);
  }
  if (state === "approved" || state === "rejected") {
    const pc = approval.pc_answer || {};
    let line = "Approved. Waiting for your PC to pick this up. If it is off, it will start when it is back on, as long as that is within 48 hours.";
    let tone = "";
    if (pc.outcome === "accepted") line = `Your PC accepted it: ${pc.reason || "publishing is queued"}. This page shows the links once the posts are confirmed.`;
    if (pc.outcome === "rejected" || state === "rejected") { line = `Your PC did not publish this: ${pc.reason || "it refused the approval"}. Nothing was posted.`; tone = "bad"; }
    return shell(title, `${head}<div class="card notice"><p class="${tone}">${esc(line)}</p></div>`, pc.outcome === "accepted" || !pc.outcome ? 20 : 0);
  }
  if (state === "done") {
    return shell(title, `${head}${resultsHtml(approval.results || {})}`);
  }

  const remoteOk = Boolean(approval.remote_ok);
  // Where these can go: the connected accounts. One that isn't connected, or
  // whose sign-in ran out, isn't listed at all.
  const places = Object.keys(LABELS).filter((p) => ((accounts || {})[p] || {}).connected);
  const listed = clips.map((c) => clipCard(c, token, { selectable: remoteOk, thumbs, previews, checked: ticked ? ticked.clips.has(Number(c.clip_id)) : true, places })).join("");
  // The PC sends the videos one at a time after the offer. Said only in its
  // first ten minutes: a video that hasn't come by then isn't coming.
  const coming = payload.previews && clips.some((c) => !previews.has(c.clip_id)) && nowS - approval.created_at < 600
    ? `<p class="small muted">Videos of these clips are still coming from your PC. Reload this page in a minute to watch them.</p>`
    : "";
  const expires = `<p class="small muted">This link works for ${esc(left(approval.expires_at, nowS))} more.</p>`;
  // At the top, where the redirect after Remind me later lands.
  const deferred = state === "deferred" ? `<div class="card notice">${esc(deferredText(later, approval, nowS, remoteOk && !blocked))}</div>` : "";
  if (!remoteOk) {
    return shell(title, `${head}${expires}${deferred}<div class="card notice">Approving from your phone is switched off. Open Clips Kitty on your PC to publish these.</div>${coming}${listed}`);
  }
  const on = ticked ? ticked.platforms : new Set(payload.preferred || []);
  const dest = places.length
    ? places.map((p) => `<label class="card row"><input type="checkbox" name="platform" value="${p}"${on.has(p) ? " checked" : ""}><span>${esc(LABELS[p])}<br><span class="small muted">${esc(accounts[p].name || "")}</span></span></label>`).join("")
    : `<div class="card notice">No account is connected yet. Connect one in Clips Kitty on your PC, then open this link again.</div>`;
  // Blocked: no PIN box and no Approve button, until a new PIN is set on the
  // PC. Reminding and cancelling never needed the PIN, so they stay.
  const pinBox = blocked
    ? (error === PIN_BLOCKED ? "" : `<div class="card notice bad">${esc(PIN_BLOCKED)}</div>`)
    : `<div class="card"><label for="pin" class="small muted">Approval PIN (set in Clips Kitty)</label>
<input id="pin" type="password" name="pin" inputmode="numeric" autocomplete="off" maxlength="8">
<p class="small muted">Enter your PIN yourself. Never give it to an assistant.</p></div>`;
  // Said before the tap, where nothing will send the link again.
  const hint = state === "open" && later !== "whatsapp" ? `<p class="small muted">${esc(`Remind me later only puts this aside. ${noReminder(later)}`)}</p>\n` : "";
  const form = `<form method="post" action="/a/${esc(token)}">
<input type="hidden" name="csrf" value="${esc(csrf)}">
${error ? `<div class="card notice bad">${esc(error)}</div>` : ""}
<h2>Clips</h2>${coming}${listed}
<h2>Publish to</h2>${dest}
<h2>Confirm</h2>
<label class="card row"><input type="checkbox" name="confirm" value="yes"${ticked && ticked.confirm ? " checked" : ""}><span>Post the clips I ticked, publicly, to the places I ticked.</span></label>
${pinBox}
<div class="buttons">
${blocked ? "" : `<button class="go" name="action" value="approve">Approve and publish</button>\n`}<button name="action" value="defer">Remind me later</button>
${hint}<button name="action" value="cancel">Cancel, post nothing</button>
</div></form>`;
  return shell(title, `${head}${expires}${deferred}${form}`);
}

export function resultsHtml(results) {
  const clips = results.clips || [];
  if (!clips.length) return `<div class="card notice">Nothing was confirmed as posted.</div>`;
  const t = results.totals || {};
  const top = `<div class="card"><b>${esc(t.published || 0)}</b> published${t.failed ? `, <span class="bad">${esc(t.failed)} failed</span>` : ""}${t.unconfirmed ? `, ${esc(t.unconfirmed)} not confirmed yet` : ""}${t.skipped ? `, ${esc(t.skipped)} skipped` : ""}.</div>`;
  const rows = clips.map((c) => {
    const posts = (c.posts || []).map((p) => {
      const link = safeLink(p.url);
      if (p.state === "published") {
        return `<li class="ok">${esc(LABELS[p.platform] || p.platform)}: published${link ? ` · <a href="${esc(link)}" rel="noopener noreferrer">open</a>` : " (no link returned yet)"}</li>`;
      }
      return `<li class="${p.state === "failed" ? "bad" : "muted"}">${esc(LABELS[p.platform] || p.platform)}: ${esc(p.state)}${p.error ? ` · ${esc(p.error)}` : ""}</li>`;
    }).join("");
    return `<div class="card"><b>${esc(c.title || `Clip ${c.clip_id}`)}</b><ul>${posts}</ul></div>`;
  }).join("");
  return `${top}${rows}<p class="small muted">Only posts the platforms confirmed are shown as published, with the links they returned.</p>`;
}

export function connectedPage(text, ok = true) {
  return message(ok ? "Connected" : "Not connected", text, { tone: ok ? "ok" : "bad" });
}

export function choosePageHtml(state, pages) {
  const items = pages.map((p) => `<label class="card row"><input type="radio" name="page" value="${esc(p.id)}" required><span><b>${esc(p.name)}</b><br><span class="small muted">${p.instagram ? `Instagram: ${esc(p.instagram)}` : "No Instagram account linked"}</span></span></label>`).join("");
  return shell("Choose a Page", `<h1>Which Facebook Page?</h1><p class="muted">Clips Kitty publishes Reels to this Page, and to the Instagram account linked to it.</p>
<form method="post" action="/oauth/facebook/choose"><input type="hidden" name="state" value="${esc(state)}">${items}
<div class="buttons"><button class="go">Use this Page</button></div></form>`);
}
