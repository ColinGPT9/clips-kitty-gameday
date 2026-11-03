-- Clips Kitty Meta relay: D1 schema. Apply with
--   npx wrangler d1 execute clips-kitty-relay --remote --file=schema.sql
-- Every statement is IF NOT EXISTS, so running it again is harmless.
--
-- Nothing here is a plaintext credential: device secrets, link tokens kept
-- for reminders, and Meta access tokens are AES-GCM encrypted with the
-- TOKEN_KEY secret; link tokens, pairing codes and Muse keys are otherwise
-- stored only as SHA-256 hashes; the PIN only as a PBKDF2 hash.

-- A Clips Kitty PC, registered once with the relay owner's ENROLL_KEY.
CREATE TABLE IF NOT EXISTS devices (
  device_id    TEXT PRIMARY KEY,
  secret_enc   TEXT NOT NULL,
  name         TEXT NOT NULL DEFAULT '',
  prefs        TEXT NOT NULL DEFAULT '{}',
  pin_hash     TEXT NOT NULL DEFAULT '',
  pin_salt     TEXT NOT NULL DEFAULT '',
  pin_fails    INTEGER NOT NULL DEFAULT 0,
  pin_locked_until INTEGER NOT NULL DEFAULT 0,
  created_at   INTEGER NOT NULL,
  last_seen    INTEGER NOT NULL DEFAULT 0
);

-- The WhatsApp user a PC notifies. Paired by a code shown on the PC and sent
-- FROM the phone (so the person proves both ends), then confirmed on the PC.
-- state: pending (code issued) | awaiting_confirm (phone sent it) |
--        confirmed | opted_out
CREATE TABLE IF NOT EXISTS recipients (
  device_id     TEXT PRIMARY KEY,
  wa_id         TEXT NOT NULL DEFAULT '',
  user_id       TEXT NOT NULL DEFAULT '',
  state         TEXT NOT NULL,
  code_hash     TEXT NOT NULL DEFAULT '',
  code_expires  INTEGER NOT NULL DEFAULT 0,
  last_inbound  INTEGER NOT NULL DEFAULT 0,
  created_at    INTEGER NOT NULL,
  updated_at    INTEGER NOT NULL
);

-- Signed-request nonces, kept 15 minutes: a captured request can't be replayed.
CREATE TABLE IF NOT EXISTS nonces (
  device_id TEXT NOT NULL,
  nonce     TEXT NOT NULL,
  at        INTEGER NOT NULL,
  PRIMARY KEY (device_id, nonce)
);

-- One answer per notification event key: a PC retrying after a lost answer
-- gets the same answer back, and no second WhatsApp message.
CREATE TABLE IF NOT EXISTS events (
  device_id TEXT NOT NULL,
  event_key TEXT NOT NULL,
  response  TEXT NOT NULL,
  at        INTEGER NOT NULL,
  PRIMARY KEY (device_id, event_key)
);

-- An offer of clips, behind one link.
-- state: open | deferred | approved | cancelled | expired | rejected | done
CREATE TABLE IF NOT EXISTS approvals (
  id            TEXT PRIMARY KEY,
  device_id     TEXT NOT NULL,
  review_ref    INTEGER NOT NULL,
  token_hash    TEXT NOT NULL UNIQUE,
  token_enc     TEXT NOT NULL,
  fingerprint   TEXT NOT NULL,
  state         TEXT NOT NULL,
  payload       TEXT NOT NULL,
  remote_ok     INTEGER NOT NULL DEFAULT 0,
  expires_at    INTEGER NOT NULL,
  remind_at     INTEGER NOT NULL DEFAULT 0,
  message_id    TEXT NOT NULL DEFAULT '',
  delivery      TEXT NOT NULL DEFAULT '',
  decision      TEXT NOT NULL DEFAULT '{}',
  pc_answer     TEXT NOT NULL DEFAULT '{}',
  results       TEXT NOT NULL DEFAULT '{}',
  created_at    INTEGER NOT NULL,
  updated_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_approvals_device ON approvals(device_id, state);

CREATE TABLE IF NOT EXISTS thumbs (
  approval_id TEXT NOT NULL,
  clip_id     INTEGER NOT NULL,
  jpeg        TEXT NOT NULL,
  PRIMARY KEY (approval_id, clip_id)
);

-- The clips of an offer whose preview video is kept in KV (the optional
-- PREVIEWS binding), noted once the bytes are there: the page plays only
-- these. The bytes expire with the link; cron drops the rows once the offer
-- is no longer open.
CREATE TABLE IF NOT EXISTS previews (
  approval_id TEXT NOT NULL,
  clip_id     INTEGER NOT NULL,
  size        INTEGER NOT NULL,
  at          INTEGER NOT NULL,
  PRIMARY KEY (approval_id, clip_id)
);

-- Decisions waiting for the PC to collect them, and what it answered.
CREATE TABLE IF NOT EXISTS commands (
  id          TEXT PRIMARY KEY,
  device_id   TEXT NOT NULL,
  approval_id TEXT NOT NULL,
  kind        TEXT NOT NULL,
  body        TEXT NOT NULL,
  state       TEXT NOT NULL DEFAULT 'pending',
  outcome     TEXT NOT NULL DEFAULT '',
  reason      TEXT NOT NULL DEFAULT '',
  created_at  INTEGER NOT NULL,
  acked_at    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_commands_device ON commands(device_id, state);

-- WhatsApp webhook ids already handled (Meta retries for up to 7 days).
CREATE TABLE IF NOT EXISTS wa_seen (
  id TEXT PRIMARY KEY,
  at INTEGER NOT NULL
);

-- Messages this relay sent, for delivery statuses to find their offer.
CREATE TABLE IF NOT EXISTS messages (
  wamid       TEXT PRIMARY KEY,
  device_id   TEXT NOT NULL,
  approval_id TEXT NOT NULL DEFAULT '',
  kind        TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'accepted',
  at          INTEGER NOT NULL
);

-- Connected Meta accounts. platform: instagram | facebook.
CREATE TABLE IF NOT EXISTS accounts (
  device_id  TEXT NOT NULL,
  platform   TEXT NOT NULL,
  account_id TEXT NOT NULL,
  name       TEXT NOT NULL DEFAULT '',
  token_enc  TEXT NOT NULL,
  expires_at INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (device_id, platform)
);

-- OAuth sign-ins in progress (10 minutes), and Facebook Pages waiting for
-- the creator to choose one.
CREATE TABLE IF NOT EXISTS oauth_states (
  state      TEXT PRIMARY KEY,
  device_id  TEXT NOT NULL,
  platform   TEXT NOT NULL,
  pending    TEXT NOT NULL DEFAULT '',
  expires_at INTEGER NOT NULL
);

-- Every Graph object a publish created, bound to the approved command that
-- created it: a later step may only touch an object of its own command.
CREATE TABLE IF NOT EXISTS graph_refs (
  ref        TEXT PRIMARY KEY,
  command_id TEXT NOT NULL,
  clip_id    INTEGER NOT NULL,
  platform   TEXT NOT NULL,
  kind       TEXT NOT NULL,
  file_size  INTEGER NOT NULL DEFAULT 0,
  upload_uri TEXT NOT NULL DEFAULT '',
  uploaded   INTEGER NOT NULL DEFAULT 0,
  at         INTEGER NOT NULL
);

-- Keys for the creator's Muse agent (a custom connector in Muse chat), at
-- most one working per PC. Only the SHA-256 of a key is kept: the key is
-- shown once in Clips Kitty Settings and lives in Muse's credential store.
-- A key reads offers and posts, hands out approval links, defers and
-- cancels; it never approves or publishes (lib/muse.js). window_start and
-- window_count are its per-minute rate limit.
CREATE TABLE IF NOT EXISTS connector_tokens (
  token_hash   TEXT PRIMARY KEY,
  device_id    TEXT NOT NULL,
  created_at   INTEGER NOT NULL,
  last_used    INTEGER NOT NULL DEFAULT 0,
  revoked_at   INTEGER NOT NULL DEFAULT 0,
  window_start INTEGER NOT NULL DEFAULT 0,
  window_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_connector_tokens_device ON connector_tokens(device_id, revoked_at);

-- Each time five wrong PINs locked the approval page. Three within a day
-- block approving from the phone until a new PIN is set on the PC, which
-- clears them.
CREATE TABLE IF NOT EXISTS pin_lockouts (
  device_id TEXT NOT NULL,
  at        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pin_lockouts_device ON pin_lockouts(device_id, at);

-- Template statuses, as Meta last reported them.
CREATE TABLE IF NOT EXISTS templates (
  name       TEXT PRIMARY KEY,
  status     TEXT NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS audit (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id   TEXT NOT NULL DEFAULT '',
  approval_id TEXT NOT NULL DEFAULT '',
  actor       TEXT NOT NULL,
  action      TEXT NOT NULL,
  detail      TEXT NOT NULL DEFAULT '',
  at          INTEGER NOT NULL
);
