# Clips Kitty Meta relay

A small Cloudflare Worker (with a D1 database) that sits between a
creator's phone, Meta's APIs, and the Clips Kitty PC. New for the Meta
hackathon edition. The creator deploys their own; it is single-tenant.

- Sends WhatsApp messages (Cloud API) and receives WhatsApp webhooks.
- Serves the phone's approval page.
- Keeps a small preview video of each offered clip in Workers KV until the
  link expires, so the page can play it before anything is approved. The KV
  binding (`PREVIEWS`) is optional: without it the page shows thumbnails.
- Runs Facebook Login and keeps the tokens (sealed).
- Makes the Instagram and Facebook Graph API calls for an approved publish,
  streaming the rendered file the PC sends it.
- Hosts the connector for the creator's Muse agent: an MCP server at
  `POST /mcp`, the same tools at `/muse/v1/...`, their OpenAPI description at
  `/muse/openapi.json`, and a setup guide at `/muse`. Muse reads offers and
  results and can defer or cancel an offer; it can't approve or publish, and
  only a WhatsApp number gets a reminder. See
  [MUSE_CONNECTOR.md](../docs/hackathon/meta/MUSE_CONNECTOR.md).

The PC only ever calls out to it: once with the enrolment key to register,
then with signed requests. The relay never calls the PC. See [docs/hackathon/meta/ARCHITECTURE.md](../docs/hackathon/meta/ARCHITECTURE.md)
and [SECURITY.md](../docs/hackathon/meta/SECURITY.md).

## Deploy

Full steps, including the Meta app: [WHATSAPP_SETUP.md](../docs/hackathon/meta/WHATSAPP_SETUP.md)
and [META_PUBLISHING.md](../docs/hackathon/meta/META_PUBLISHING.md). In short:

```
npx wrangler login
npx wrangler d1 create clips-kitty-relay        # put database_id in wrangler.toml
npx wrangler d1 execute clips-kitty-relay --remote --file=schema.sql
npx wrangler kv namespace create clips-kitty-previews   # optional: its id in wrangler.toml, binding PREVIEWS
# set PUBLIC_URL (and optionally WHATSAPP_DISPLAY_NUMBER) in wrangler.toml
for s in ENROLL_KEY TOKEN_KEY WHATSAPP_TOKEN WHATSAPP_PHONE_NUMBER_ID WHATSAPP_WABA_ID \
         WEBHOOK_VERIFY_TOKEN META_APP_ID META_APP_SECRET; do
  npx wrangler secret put $s
done
npx wrangler deploy
```

Webhook: `https://<relay>/webhook` (fields: `messages`,
`message_template_status_update`). OAuth redirect:
`https://<relay>/oauth/facebook/callback`.

## Run it locally with Meta faked

```
npm test                      # 105 tests: node --test, D1 on node:sqlite, KV on a Map, Meta faked
npm run dev                   # http://127.0.0.1:8787, prints an enrolment key
```

`npm run dev` runs the real `worker.js` with `test/serve.mjs`, whose
`/__mock/*` routes play the phone (send a message, tap a button, a delivery
status), finish an OAuth sign-in, run the cron, and move the clock. Nothing
is sent to WhatsApp or posted anywhere. `scripts/meta_demo.py` uses it for
the end-to-end demo.

Needs Node 22.5+ (for `node:sqlite`; the npm scripts pass `--experimental-sqlite`,
which it needs before Node 22.13). No npm dependencies.

## Files

| File | What |
|---|---|
| `worker.js` | Routes, pairing, notify (offers kept for Muse when WhatsApp is off), the approval page's actions, the PIN lockouts, Muse keys for the PC, commands, the clips' preview videos (KV, byte ranges), the Graph step gate, cron |
| `lib/whatsapp.js` | Cloud API sends, the four templates, error meanings, the 24-hour window |
| `lib/meta.js` | Facebook Login, the Graph steps, uploads to rupload |
| `lib/page.js` | The approval and status pages (server-rendered, no JavaScript; each clip's preview plays in the browser's own player) |
| `lib/crypto.js` | HMAC, AES-GCM sealing, PBKDF2 PIN hashing, canonical JSON |
| `lib/muse.js` | The Muse connector: MCP, the REST routes, the OpenAPI description, keys and their rate limit |
| `lib/db.js` | D1 helpers, the audit log, and the PIN lockout block |
| `schema.sql` | The D1 schema (run it again on a relay deployed before the Muse connector or the preview videos: it only adds what is missing) |
| `test/` | The harness (D1 on node:sqlite, KV on a Map, FakeMeta), the tests, and the local server |
