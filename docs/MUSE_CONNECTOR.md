# Connecting your Muse agent

Muse is Meta's personal agent app. In Muse chat you can add a private
**custom connector** for a service that offers an API or an MCP server, and
Muse keeps its credential in its Secure Credentials Store. Your relay offers
one, so whoever runs the socials can ask Muse, from anywhere:

- "What highlights are ready?"
- "Send me the approval link."
- "What got posted?"
- "Put that one aside for later."
- "Cancel that one."

**Muse can never post anything.** Publishing happens only on the approval
page (`/a/<link>` on your relay), where you tick the clips and the places to
post and enter your own PIN. Muse can hand you that link. No tool takes or
shows the PIN, and the connector tells Muse never to ask for it or type it.

Muse doesn't message you when clips are ready. Offers wait on the relay for
Muse to find: ask it what's ready (or set yourself a reminder in Muse), or
look at the dashboard. No message is sent anywhere.

## How it works

Muse runs on Meta's servers, so it can't reach the app. The connector lives
on your relay, next to the approval page:

```
Muse (Meta's servers) --HTTPS, Muse key--> your relay /mcp --> offers, links, results
                                                 ^
the app --signed requests, outbound only---------+
```

The app sends each finished game's offer to the relay. Muse reads it there.
When you approve on the page, the app picks the approval up, checks it
against its own record of the offer, and publishes.

## Set it up

1. **Deploy the relay** ([RELAY_SETUP.md](RELAY_SETUP.md)). Publishing
   needs `META_APP_ID` and `META_APP_SECRET`.
2. **Connect the app** to it: **Setup → Relay** in the dashboard, with the
   relay's address and enrolment key.
3. **Make the key**: **Setup → Muse → Make a Muse key**. The key (`ckm_...`)
   is shown once, with a **Copy** button. The app doesn't keep it: once you
   leave, it is gone from the dashboard. If you lose it, make a new one.
4. **Tell Muse.** In Muse chat, say:

   > Create a custom connector called Clips Kitty: a remote MCP server at
   > https://&lt;your relay&gt;/mcp that uses the header
   > Authorization: Bearer &lt;your key&gt;

   When Muse asks for the credential, paste the key into **Muse's credential
   prompt, not into the chat**. The sentence itself holds no secret.
5. **If Muse asks for API details instead** of an MCP server, give it the
   OpenAPI description at `https://<your relay>/muse/openapi.json`. It
   describes the same tools as plain HTTPS routes with the same key.
6. **Set an approval PIN and connect your accounts** in **Setup**, then
   switch on **Let me approve publishing from my phone**. It needs Muse
   connected and the PIN.

Try it: "Clips Kitty, what highlights are ready?"

The relay is shared with the desktop edition of Clips Kitty, so some of its
own wording, which Muse may repeat, doesn't fit this app: another way of
being messaged, or a Settings screen. Neither applies here. This app sends
no messages, and Muse keys are made in **Setup → Muse**.

## The tools

Every tool works only for the app whose key it is.

| Tool | Changes anything? | What it does |
|---|---|---|
| `list_ready_highlights` | No | Offers waiting for a decision: the title, what was found, each clip (title, play, length, where it is already posted), the places it can post, when the link expires, any reminder, and when the app last checked in. Optional: `match`, `since`, `limit` (1 to 20), `include_captions`. Never a link token or a thumbnail. |
| `get_approval_link` | No | The approval link for one offer (`approval_id`, `match` to pick by name, or the one open offer). When none or several match, it gives the choices instead, so Muse asks which one. Each link handed out is logged as Muse's. |
| `list_recent_posts` | No | Offers approved in the last 1 to 7 days and what became of each clip, with only the links Instagram and Facebook returned. |
| `remind_me_later` | The offer | What the page's **Remind me later** does (15 to 1440 minutes, or your usual wait). Nothing is sent then: Muse gets the time back (`remind_at`) and is asked to remind you itself, which isn't confirmed to work yet (below). |
| `cancel_offer` | The offer | What the page's **Cancel** does. It can't be undone, so it needs `confirm: true`, which Muse should only send after you say so. |
| `relay_status` | No | When the app last checked in, whether a PIN is set, whether wrong PINs have blocked approving from the phone, the connected accounts and how many offers are open. |

No tool approves, publishes, takes a PIN, connects an account or changes
a setting.

The same tools over plain HTTPS (same key, same answers):

| Route | Tool |
|---|---|
| `GET /muse/v1/offers`, optional query `match`, `since`, `limit`, `include_captions` | `list_ready_highlights` |
| `GET /muse/v1/offers/{approval_id}/link` | `get_approval_link` |
| `GET /muse/v1/posts`, optional query `days`, `limit` | `list_recent_posts` |
| `POST /muse/v1/offers/{approval_id}/remind` `{"minutes": 60}` | `remind_me_later` |
| `POST /muse/v1/offers/{approval_id}/cancel` `{"confirm": true}` | `cancel_offer` |
| `GET /muse/v1/status` | `relay_status` |

To check the connector yourself, with the key in an environment variable
rather than typed into a command:

```
curl https://<your relay>/muse/openapi.json
curl -H "Authorization: Bearer $MUSE_KEY" https://<your relay>/muse/v1/offers
curl -X POST https://<your relay>/mcp -H "Authorization: Bearer $MUSE_KEY" \
  -H "Content-Type: application/json" -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

## What Muse can't do

- **Approve or publish.** Approving takes the approval page, your PIN and
  the "post publicly" tick. Then the app checks the approval against its own
  record of the offer, and the relay runs each Instagram or Facebook step
  only under an approval the app accepted. Muse is in none of those steps.
- **See or use your PIN.** The page says next to the PIN box: "Enter your PIN
  yourself. Never give it to an assistant." If Muse ever asks for it, don't
  give it.
- **Call the app, or the relay's routes for the app.** A Muse key works on
  `/mcp` and `/muse/v1/` only. The app's signature works on the relay's other
  routes, never on those.

Everything Muse does is marked as Muse's: a reminder or a cancel reaches the
app as a signed command marked `via: "muse"`, and the audit logs on the relay
and in the app say `muse`. The app refuses any approval that says it came
from Muse, even one the relay signed.

## The PIN

Five wrong PINs lock the approval page for 15 minutes, and wrong PINs sent
all at once count the same as one at a time. Three of those lockouts within
a day **block approving from the phone** until you set a new PIN in the
dashboard, and the page says so (so does Muse, if you ask it for the link).
Remind me later and Cancel still work while it is blocked.

## The key

- It starts with `ckm_`. The relay keeps only its SHA-256, never the key.
- Keep it like a password. Whoever has it can read your offers and get their
  approval links, so they could put one aside or cancel it, but approving
  still takes your PIN.
- It is accepted only in the `Authorization: Bearer` header, never in the
  address.
- About 60 calls a minute per key; more get `429` with `Retry-After`.
- One key works per app. **Make a Muse key** again replaces the old one at
  once.
- **Revoke** stops it at once. Disconnecting the app from the relay stops it
  too.

If approving from the phone was on only because of Muse, revoking the key
switches it off as well: nothing would reach you any more.

## Not confirmed yet

The connector is built from Meta's description of custom connectors and the
MCP specification, and runs in the relay's tests. It has not been tried in
the Muse app itself:

- **Which MCP revision Muse speaks.** The relay answers both the
  `initialize` handshake (2025-03-26, 2025-06-18 and 2025-11-25, in plain
  JSON, never a session) and the stateless 2026-07-28 revision. It offers no
  Server-Sent Events (`GET /mcp` answers 405) and no JSON-RPC batches.
- **OAuth or a header key.** The relay takes a key in a header. If Muse
  connects to remote MCP servers only through OAuth, this connector won't
  connect as it is: the OpenAPI routes with the same key are the fallback to
  try.
- **The `Origin` header.** A request that carries an `Origin` other than the
  relay's own is refused, so web pages can't call it. Muse's servers are not
  expected to send one.
- **Whether Muse can remind you later by itself.** Nothing sends a
  reminder, and the relay can't reach Muse first: it only answers Muse's
  calls. Until that is tried, the approval page says the app
  will not remind you, and to ask Muse for the link.

## Where it is

| File | What |
|---|---|
| `meta-relay/lib/muse.js` | The MCP server, the tools, the REST routes, the OpenAPI description, keys and their rate limit |
| `meta-relay/worker.js` | Making and revoking keys for the app (`/v1/muse/token`, `/v1/muse/revoke`), offers kept for Muse alone, the PIN lockouts |
| `meta-relay/test/muse.test.mjs` | The connector's tests, including that nothing Muse sends ever makes an approval |
| The app's API | `POST /remote-approval/muse/token` and `POST /remote-approval/muse/revoke` ([API.md](API.md)), behind the owner password and the `X-Gameday-Action` header |
