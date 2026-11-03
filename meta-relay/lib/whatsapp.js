/**
 * WhatsApp Business Cloud API: the official one, and only that.
 *
 * Outside the 24-hour window after the creator last wrote to the business
 * number, only an approved UTILITY template may be sent. Inside it, an
 * interactive message is allowed too, which is how notifications still work
 * while the templates wait for Meta's review (it takes up to a day): the
 * creator paired by sending a message, so the window is open right after.
 *
 * Every message is built from what the PC sent (counts, titles), never from
 * free text a model wrote. No message ever carries a token other than the
 * link's own, an error log, a path or a phone number.
 */

import { now, one, run } from "./db.js";

export const LANGUAGE = "en";

/**
 * The templates, as submitted for review. Bodies never start or end with a
 * parameter, parameters never hold a newline, button labels stay under 25
 * characters, and the URL variable sits at the end of the URL: all Meta
 * rules that otherwise get a template rejected.
 */
export function templateDefs(publicUrl) {
  const base = `${publicUrl.replace(/\/$/, "")}/a/{{1}}`;
  return [
    {
      name: "ck_highlights_ready",
      category: "UTILITY",
      language: LANGUAGE,
      components: [
        {
          type: "BODY",
          text: "Clips Kitty finished your video {{1}}. Ready to review: {{2}}. Choose where to publish them before the link expires.",
          example: { body_text: [["Weekend stream", "4 highlights (best 4 of 9)"]] },
        },
        { type: "FOOTER", text: "Reply STOP to turn these messages off" },
        {
          type: "BUTTONS",
          buttons: [
            { type: "URL", text: "Review clips", url: base, example: ["k7Qm2xV9aZ3pL0wXyZ"] },
            { type: "QUICK_REPLY", text: "Remind me later" },
            { type: "QUICK_REPLY", text: "Cancel" },
          ],
        },
      ],
    },
    {
      name: "ck_processing_failed",
      category: "UTILITY",
      language: LANGUAGE,
      components: [
        { type: "BODY", text: "Clips Kitty could not finish processing your video. Open the app for details." },
      ],
    },
    {
      name: "ck_publish_update",
      category: "UTILITY",
      language: LANGUAGE,
      components: [
        {
          type: "BODY",
          text: "Publishing update for {{1}}: {{2}}. Open the status page for the confirmed links.",
          example: { body_text: [["Raptors vs Celtics", "3 of 4 posts published (1 failed)"]] },
        },
        { type: "BUTTONS", buttons: [{ type: "URL", text: "View status", url: base, example: ["k7Qm2xV9aZ3pL0wXyZ"] }] },
      ],
    },
    {
      name: "ck_test_message",
      category: "UTILITY",
      language: LANGUAGE,
      components: [
        { type: "BODY", text: "This is a test message from Clips Kitty. Your WhatsApp notifications are set up correctly." },
      ],
    },
  ];
}

export const TEMPLATE_NAMES = ["ck_highlights_ready", "ck_processing_failed", "ck_publish_update", "ck_test_message"];

/** What a WhatsApp error means to a creator. Never Meta's own text. */
const SEND_ERRORS = {
  131047: ["window_closed", "More than 24 hours have passed since you last messaged the business number, and the message template is not approved yet."],
  131026: ["undeliverable", "WhatsApp could not deliver to this number. Check that WhatsApp is installed and the number is current."],
  131049: ["undeliverable", "WhatsApp held this message back to protect the experience. It may go through later."],
  130429: ["rate_limited", "WhatsApp is rate limiting the business number. The message will be retried."],
  131056: ["rate_limited", "Too many messages to this number in a short time. The message will be retried."],
  132000: ["template_not_approved", "The message template's parameters did not match. Re-create the templates from Settings."],
  132001: ["template_missing", "The message template does not exist yet. Create the templates from Settings."],
  132012: ["template_not_approved", "The message template's parameters were rejected."],
  132015: ["template_not_approved", "The message template is paused for low quality."],
  132016: ["template_not_approved", "The message template is disabled."],
  190: ["token_expired", "The relay's WhatsApp access token has expired. Replace WHATSAPP_TOKEN on the relay."],
  200: ["forbidden", "The relay's WhatsApp token lacks permission to send."],
  10: ["forbidden", "The relay's WhatsApp token lacks permission to send."],
  131030: ["not_allowed", "This number is not on the test number's allowed list. Add it in the WhatsApp developer settings."],
};

export class WaError extends Error {
  constructor(code, message, metaCode = 0) {
    super(message);
    this.code = code;
    this.metaCode = metaCode;
  }
}

export function describeError(metaCode) {
  const known = SEND_ERRORS[Number(metaCode)];
  return known ? { code: known[0], message: known[1] } : { code: "send_failed", message: "WhatsApp did not accept the message." };
}

function graphBase(env) {
  return (env.GRAPH_BASE || "https://graph.facebook.com/v26.0").replace(/\/$/, "");
}

async function post(env, path, body) {
  const res = await fetch(`${graphBase(env)}${path}`, {
    method: "POST",
    headers: { Authorization: `Bearer ${env.WHATSAPP_TOKEN}`, "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  let data = {};
  try {
    data = await res.json();
  } catch {
    data = {};
  }
  if (!res.ok || data.error) {
    const metaCode = Number((data.error && data.error.code) || 0);
    const { code, message } = describeError(metaCode);
    console.error("whatsapp send refused", metaCode, data.error && data.error.error_subcode);
    throw new WaError(code, message, metaCode);
  }
  return data;
}

function to(recipient) {
  // A user who hides their number is reachable by BSUID alone.
  return recipient.wa_id ? { to: recipient.wa_id } : { recipient: recipient.user_id };
}

/** Whether a plain message can still reach the creator at `at`: within a day of their last message to us. */
export function windowOpen(recipient, at = now()) {
  return Boolean(recipient.last_inbound) && at - recipient.last_inbound < 23.5 * 3600;
}

export async function sendTemplate(env, recipient, name, params = [], buttons = []) {
  const components = [];
  if (params.length) {
    components.push({ type: "body", parameters: params.map((text) => ({ type: "text", text: clean(text) })) });
  }
  components.push(...buttons);
  const body = {
    messaging_product: "whatsapp",
    recipient_type: "individual",
    ...to(recipient),
    type: "template",
    template: { name, language: { code: LANGUAGE }, components },
  };
  const data = await post(env, `/${env.WHATSAPP_PHONE_NUMBER_ID}/messages`, body);
  return messageId(data);
}

export async function sendLink(env, recipient, { header, body, label, url, footer }) {
  const data = await post(env, `/${env.WHATSAPP_PHONE_NUMBER_ID}/messages`, {
    messaging_product: "whatsapp",
    recipient_type: "individual",
    ...to(recipient),
    type: "interactive",
    interactive: {
      type: "cta_url",
      ...(header ? { header: { type: "text", text: header.slice(0, 60) } } : {}),
      body: { text: body.slice(0, 1024) },
      ...(footer ? { footer: { text: footer.slice(0, 60) } } : {}),
      action: { name: "cta_url", parameters: { display_text: label.slice(0, 20), url } },
    },
  });
  return messageId(data);
}

export async function sendText(env, recipient, text) {
  const data = await post(env, `/${env.WHATSAPP_PHONE_NUMBER_ID}/messages`, {
    messaging_product: "whatsapp",
    recipient_type: "individual",
    ...to(recipient),
    type: "text",
    text: { preview_url: false, body: text.slice(0, 4096) },
  });
  return messageId(data);
}

function messageId(data) {
  return String((data.messages && data.messages[0] && data.messages[0].id) || "");
}

/** A template parameter: one line, no tabs, no runs of spaces, no more than 1,000 characters. */
export function clean(text) {
  return String(text ?? "").replace(/[\r\n\t]+/g, " ").replace(/ {4,}/g, "   ").trim().slice(0, 1000) || "-";
}

export async function rememberMessage(env, wamid, device, approval, kind) {
  if (!wamid) return;
  await run(env, "INSERT OR IGNORE INTO messages (wamid, device_id, approval_id, kind, at) VALUES (?, ?, ?, ?, ?)",
    wamid, device, approval || "", kind, now());
}

export async function templateStatus(env, name) {
  const row = await one(env, "SELECT status FROM templates WHERE name = ?", name);
  return row ? row.status : "";
}

/** Ask Meta for this account's templates and remember ours. */
export async function refreshTemplates(env) {
  const res = await fetch(`${graphBase(env)}/${env.WHATSAPP_WABA_ID}/message_templates?fields=name,status,category&limit=100`, {
    headers: { Authorization: `Bearer ${env.WHATSAPP_TOKEN}` },
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new WaError("templates_unavailable", "Couldn't read the message templates from Meta.");
  const out = {};
  for (const t of data.data || []) {
    if (!TEMPLATE_NAMES.includes(t.name)) continue;
    out[t.name] = String(t.status || "");
    await run(env, "INSERT INTO templates (name, status, updated_at) VALUES (?, ?, ?) ON CONFLICT(name) DO UPDATE SET status = excluded.status, updated_at = excluded.updated_at",
      t.name, out[t.name], now());
  }
  return out;
}

/** Submit any of our templates Meta does not have yet. */
export async function createTemplates(env) {
  const have = await refreshTemplates(env);
  const made = {};
  for (const def of templateDefs(env.PUBLIC_URL)) {
    if (have[def.name]) {
      made[def.name] = have[def.name];
      continue;
    }
    const res = await fetch(`${graphBase(env)}/${env.WHATSAPP_WABA_ID}/message_templates`, {
      method: "POST",
      headers: { Authorization: `Bearer ${env.WHATSAPP_TOKEN}`, "Content-Type": "application/json" },
      body: JSON.stringify(def),
    });
    const data = await res.json().catch(() => ({}));
    made[def.name] = res.ok ? String(data.status || "PENDING") : "REJECTED_ON_SUBMIT";
    if (!res.ok) console.error("template create refused", def.name, data.error && data.error.code);
    await run(env, "INSERT INTO templates (name, status, updated_at) VALUES (?, ?, ?) ON CONFLICT(name) DO UPDATE SET status = excluded.status, updated_at = excluded.updated_at",
      def.name, made[def.name], now());
  }
  return made;
}
