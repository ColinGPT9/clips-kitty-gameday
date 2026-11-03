# Setting up your relay

The relay (`meta-relay/`) is a small Cloudflare Worker with a D1 database that
you deploy once, for yourself. It sits between your phone, Meta's APIs and
the app:

- it serves the approval page your phone opens, and checks your PIN;
- it runs the Facebook sign-in and keeps the Page and Instagram tokens,
  sealed, so the app never holds them;
- it makes the Instagram and Facebook Graph API calls for an approved clip,
  with the file the app sends it;
- it hosts the connector for your Muse agent ([MUSE_CONNECTOR.md](MUSE_CONNECTOR.md)).

The relay can also send WhatsApp messages, and this app does not use that.

The app only ever calls out to the relay: once with the enrolment key to
register, then with signed requests. The relay never calls the app.

Allow about 30 minutes, once. You need a Meta developer account, a Meta
Business portfolio, a Facebook Page with an Instagram professional account
linked to it, a Cloudflare account (the free plan is enough for one owner),
and Node 22.5 or later on the computer you deploy from.

**Keep every token, secret and key out of chats, issues and screenshots.**
They go into `wrangler secret put` prompts only.

## 1. Create the Meta app

1. At [developers.facebook.com/apps](https://developers.facebook.com/apps),
   create an app of type **Business**, linked to your Business portfolio.
2. Note its **App ID** and **App secret** (*App settings → Basic*).
3. Add **Facebook Login for Business**, for publishing:
   - Valid OAuth redirect URI: `https://<your relay>/oauth/facebook/callback`
   - The relay asks for `pages_show_list`, `pages_read_engagement`,
     `pages_manage_posts`, `instagram_basic`, `instagram_content_publish` and
     `business_management`.
   - The Instagram account must be a professional (Business or Creator)
     account linked to the Facebook Page.

Without App Review, the app has Standard Access: these permissions work only
for people with a role on the app (admin, developer or tester). For an owner
posting to their own accounts with their own app, that is enough: give
yourself, and the Instagram account's owner, a role on the app. Posting for
anyone else needs Advanced Access through Meta's App Review and Business
Verification. This project doesn't have it and doesn't try to get around it.

## 2. Deploy the relay

From the `meta-relay/` folder:

```
npx wrangler login
npx wrangler d1 create clips-kitty-relay          # copy the database_id it prints into wrangler.toml
npx wrangler d1 execute clips-kitty-relay --remote --file=schema.sql
npx wrangler kv namespace create clips-kitty-previews   # optional, see below
npx wrangler deploy                                # prints https://clips-kitty-meta-relay.<you>.workers.dev
```

- Put the address `wrangler deploy` printed in `wrangler.toml` as
  `PUBLIC_URL`, with no trailing slash.
- The KV namespace is optional. With it, the approval page plays a small
  preview of each clip before you decide; without it, the page shows still
  thumbnails. Put its id in the commented `[[kv_namespaces]]` block of
  `wrangler.toml` (binding `PREVIEWS`) and take the `#` off those three lines.

Then set the secrets. Each command asks for the value: paste it there, not
anywhere else.

```
npx wrangler secret put ENROLL_KEY                 # any long random string; you type it once in the dashboard
npx wrangler secret put TOKEN_KEY                  # 32 random bytes in base64: openssl rand -base64 32
npx wrangler secret put META_APP_ID
npx wrangler secret put META_APP_SECRET
npx wrangler deploy
```

Check it: `https://<your relay>/health` answers `{"ok": true, ...}`, with
`"previews": true` when the KV binding is set.

If your relay was deployed before the Muse connector or the previews
existed, run the `schema.sql` line again: it only adds what is missing.

## 3. Connect the app

In the dashboard, under **Setup**:

1. **Relay**: the relay's address and the enrolment key, then **Connect**.
   The app makes its own device secret, keeps it encrypted in its data
   folder, and registers it with the relay. The enrolment key is used once
   and not kept. On a server, `RELAY_URL` and `RELAY_ENROLMENT_KEY` can fill
   in this form for you ([RUNNING.md](RUNNING.md)).
2. **Approval PIN**: 4 to 8 digits, not one digit repeated or a run like
   1234. Enter your PIN yourself. Never give it to an assistant. The relay
   keeps only a slow hash of it, and the app doesn't keep it at all.
3. **Facebook and Instagram**: **Connect** opens Meta's sign-in in a new tab.
   Sign in and pick the Page; it brings the Instagram account linked to it.
   The tokens go to the relay, sealed with `TOKEN_KEY`, never to the app.
   With more than one Page, the relay asks which one.
4. **Muse**: see [MUSE_CONNECTOR.md](MUSE_CONNECTOR.md).
5. **Let me approve publishing from my phone**: switch it on. It needs the PIN
   and a connected Muse agent. Muse doesn't message you when clips are
   ready: ask it what's ready (or set yourself a reminder in Muse), or look
   at the dashboard.

## What gets posted

| Platform | Graph API v26.0 steps, all made by the relay |
|---|---|
| Instagram Reels | `POST /{ig-user-id}/media` (`media_type=REELS`, resumable upload, caption) → the bytes to `rupload.facebook.com` → `GET /{container}?fields=status_code` until `FINISHED` → `POST /{ig-user-id}/media_publish` → `GET /{media-id}?fields=permalink` |
| Facebook Page Reels | `POST /{page-id}/video_reels` (`upload_phase=start`) → the bytes to `rupload.facebook.com` → `upload_phase=finish` with `video_state=PUBLISHED`, title and description → `GET /{video-id}?fields=status,permalink_url` until published |

A post counts as published only when the API returned its id, and the link
shown is the permalink the API returned, never one built from an id.

The relay runs a Graph step only for a clip and a place you approved on the
phone, under an approval the app accepted, within 24 hours.

## Limits worth knowing

- **Daily caps**: Instagram and Facebook limit how many posts an account can
  make through the API in a rolling 24 hours (Facebook Page Reels: 30 per
  Page). Instagram's `content_publishing_limit` reports what is left.
- **Upload size**: each request to the relay carries at most 100 MB of
  video, Cloudflare's request limit on the Free and Pro plans.
- **Tokens**: Page tokens from a long-lived sign-in don't expire while the
  app keeps its permissions. An expired or revoked one asks you to connect
  again.

## Turning it off

- From the dashboard: **Disconnect** under Relay. The relay cancels open
  offers and stops the Muse key, and the app forgets its
  device secret. The relay keeps the app's old record, its PIN hash and the
  accounts' sealed tokens: to remove those tokens, **Disconnect** each account
  first.

## Costs

Cloudflare Workers, D1 and KV fit in the free plan for one owner. The Graph
API charges nothing for posting.

## Trying it without Meta

```
cd meta-relay
npm test          # the relay's tests: D1 on node:sqlite, KV on a Map, Meta faked
npm run dev       # http://127.0.0.1:8787, prints an enrolment key
```

`npm run dev` runs the real `worker.js` with Meta faked. Its `/__mock/...`
routes finish a sign-in and list the fake posts. [LOCAL_SETUP.md](LOCAL_SETUP.md)
walks through a whole approval with it. The relay has no npm dependencies.
