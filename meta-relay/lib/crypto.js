/**
 * Small WebCrypto helpers. Everything a secret touches is compared with
 * crypto.subtle.verify (constant time), never with ===.
 */

const enc = new TextEncoder();

export function hex(bytes) {
  return [...new Uint8Array(bytes)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export function b64url(bytes) {
  let s = "";
  for (const b of new Uint8Array(bytes)) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function b64(bytes) {
  let s = "";
  for (const b of new Uint8Array(bytes)) s += String.fromCharCode(b);
  return btoa(s);
}

export function unb64(text) {
  const s = atob(String(text).replace(/-/g, "+").replace(/_/g, "/"));
  const out = new Uint8Array(s.length);
  for (let i = 0; i < s.length; i++) out[i] = s.charCodeAt(i);
  return out;
}

export function randomToken(bytes = 32) {
  return b64url(crypto.getRandomValues(new Uint8Array(bytes)));
}

export function randomId(prefix, bytes = 12) {
  return `${prefix}_${hex(crypto.getRandomValues(new Uint8Array(bytes)))}`;
}

export async function sha256Hex(data) {
  const bytes = typeof data === "string" ? enc.encode(data) : data;
  return hex(await crypto.subtle.digest("SHA-256", bytes));
}

async function hmacKey(secret, usage) {
  return crypto.subtle.importKey("raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, [usage]);
}

export async function hmacHex(secret, message) {
  const key = await hmacKey(secret, "sign");
  return hex(await crypto.subtle.sign("HMAC", key, typeof message === "string" ? enc.encode(message) : message));
}

/** Constant-time check of a hex HMAC. */
export async function hmacVerify(secret, message, givenHex) {
  if (typeof givenHex !== "string" || !/^[0-9a-f]{64}$/i.test(givenHex)) return false;
  const key = await hmacKey(secret, "verify");
  const sig = new Uint8Array(givenHex.match(/../g).map((h) => parseInt(h, 16)));
  return crypto.subtle.verify("HMAC", key, sig, typeof message === "string" ? enc.encode(message) : message);
}

/** Constant-time string equality, through HMAC under a throwaway key. */
export async function safeEqual(a, b) {
  const key = crypto.getRandomValues(new Uint8Array(16));
  const k = await crypto.subtle.importKey("raw", key, { name: "HMAC", hash: "SHA-256" }, false, ["sign", "verify"]);
  const mac = await crypto.subtle.sign("HMAC", k, enc.encode(String(a)));
  return crypto.subtle.verify("HMAC", k, mac, enc.encode(String(b)));
}

// ---- AES-GCM for what must be stored and read back (tokens, device secrets) ----

async function aesKey(env) {
  if (!env.TOKEN_KEY) throw new Error("TOKEN_KEY is not set");
  const raw = unb64(env.TOKEN_KEY);
  if (raw.length !== 32) throw new Error("TOKEN_KEY must be 32 bytes, base64");
  return crypto.subtle.importKey("raw", raw, "AES-GCM", false, ["encrypt", "decrypt"]);
}

export async function seal(env, plaintext) {
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const data = await crypto.subtle.encrypt({ name: "AES-GCM", iv }, await aesKey(env), enc.encode(plaintext));
  return `${b64url(iv)}.${b64url(data)}`;
}

export async function unseal(env, sealed) {
  const [iv, data] = String(sealed || "").split(".");
  if (!iv || !data) return "";
  const out = await crypto.subtle.decrypt({ name: "AES-GCM", iv: unb64(iv) }, await aesKey(env), unb64(data));
  return new TextDecoder().decode(out);
}

// ---- the approval PIN: PBKDF2, never stored or logged as typed ----

export const PIN_ITERATIONS = 100000;

export async function pinHash(pin, saltB64) {
  const base = await crypto.subtle.importKey("raw", enc.encode(String(pin)), "PBKDF2", false, ["deriveBits"]);
  const bits = await crypto.subtle.deriveBits(
    { name: "PBKDF2", hash: "SHA-256", salt: unb64(saltB64), iterations: PIN_ITERATIONS }, base, 256);
  return b64url(bits);
}

/**
 * The exact text both sides sign: keys sorted at every level, no spaces.
 * Matches remote_approval/relay.py canonical_json (json.dumps with
 * sort_keys and compact separators). Numbers must be integers: Python
 * writes 1.0 where JavaScript writes 1.
 */
export function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map((k) => `${JSON.stringify(k)}:${canonical(value[k])}`).join(",")}}`;
  }
  return JSON.stringify(value === undefined ? null : value);
}
