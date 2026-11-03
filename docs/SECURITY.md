# Security

What protects the owner's accounts and the app, and what each piece defends
against.

## Principles

- **Nothing is posted without an explicit approval** on the approval page:
  the clips and places ticked, a confirm box, and the PIN. The dashboard has
  no "post now".
- **Only the owner reaches the app.** Every route except `GET /health` needs
  the owner password, and the app sits behind HTTPS.
- **The relay never calls the app.** The app calls out to the relay (signed
  requests) and to `api.meta.ai`. The sign-in redirects, Muse's calls and
  the phone's pages all land on the relay.
- **No Meta app secret or account token in the app.** They live on the
  owner's relay only: Wrangler secrets, and account tokens sealed with
  AES-GCM under `TOKEN_KEY` in D1. The app holds the owner password, its
  Meta Model API key and its relay device secret. A key saved in the
  dashboard and the device secret are encrypted at rest.
- **A link alone authorises nothing.** Approving needs the link
  (single-purpose, expiring), the PIN, and the app's own checks.
- **The relay has the least power that works.** It can't read the app's
  files or call its API. It can only answer the app's signed requests, and
  only run Graph steps for a clip and place the phone approved, under a
  command the app accepted, within 24 hours.
- **Only Meta's models are called.** `tests/test_meta_only.py` fails on any
  other AI package, import or model host.

## The app

| Threat | Protection |
|---|---|
| Someone else opens the dashboard | HTTP Basic auth as `owner` on every route except `GET /health`. The password comes from `GAMEDAY_PASSWORD`, or is generated on the first start into `DATA_DIR/owner-password.txt` (mode 0600). The log says where it is, never what it is |
| Someone guessing the password | 10 wrong passwords from one address in 10 minutes lock that address out for 15 minutes (429), without its password being checked. A `GAMEDAY_PASSWORD` shorter than 12 characters stops the app from starting unless it listens on `127.0.0.1` only |
| The password seen on the wire | HTTPS at the proxy; port 8080 is never published to the internet ([RUNNING.md](RUNNING.md)) |
| Another site posting to the app with the owner's saved password | Every POST, PUT, PATCH and DELETE needs the header `X-Gameday-Action: 1`, or it gets 403. A page on another site can't add that header to a request to the app |
| Script injection in the dashboard | `Content-Security-Policy: default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; style-src 'self'; script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'`. The dashboard has no inline scripts or styles and loads nothing from other sites (`tests/test_static.py`). Also `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer` |
| Stored secrets read from a copy of the database | Stored secrets, such as the saved Meta key, are encrypted with a Fernet key from `GAMEDAY_SECRET_KEY`, or from `DATA_DIR/secret.key` (0600). A backup of `DATA_DIR` holds that file too, so keep backups private |
| The Meta key leaking through the dashboard | No route ever sends it back: the dashboard only learns whether one is saved, or comes from the environment |
| Reading files through the media route | `/media/...` serves only `.mp4`, `.jpg` and `.png`, only under `DATA_DIR/media/jobs/*/clips/`. Scratch files (`tmp/`) are never served |
| A huge or bogus upload | Uploads are streamed to disk, capped at `MAX_UPLOAD_MB` (413), and checked by ffprobe; a file that isn't a readable video gets 400 and is removed |
| A watched folder used to change files, or to feed the app its own output | Watched files are only read. A folder inside `DATA_DIR` is refused |
| Running other programs | The app runs only ffmpeg and ffprobe, with argument lists, never through a shell |
| Runaway spending | Each Meta call checks the budget first and is refused once estimated spending passes `META_BUDGET_USD`. Every call, ok or not, is logged with its estimated cost |

## The approval page

| Threat | Protection |
|---|---|
| Someone else gets the link | The link token is 32 random bytes, found by its SHA-256 and otherwise kept only sealed with AES-GCM under `TOKEN_KEY` (so Muse can hand it over). It expires with the offer (1 to 72 hours, 24 by default). Approving also needs the PIN |
| PIN guessing | 4 to 8 digits, not one digit repeated or a run; stored as PBKDF2-SHA256 (100,000 rounds, salted). Five wrong tries lock approvals for 15 minutes, and each try is counted before its PIN is checked, so tries sent all at once get no more. Three lockouts within a day block approving from the phone until a new PIN is set in the dashboard |
| Cross-site form posts | A CSRF token bound to the approval and link (HMAC with `TOKEN_KEY`) and an `Origin` check |
| A tampered form | Clips must be in the offer and places must be connected; anything else is refused and nothing is approved |
| Script injection | Server-rendered HTML with everything escaped, no JavaScript, `Content-Security-Policy: default-src 'none'` (images and videos only from the relay itself), `frame-ancestors 'none'`, `Cache-Control: no-store` |
| Someone else watches the previews | A preview is served only under its offer's link token, and only while the offer is open or put aside and its link hasn't expired. The relay takes one only from the app that made the offer, for a clip in it, as an MP4 of at most 4 MiB, and keeps it until five minutes after the link expires |

## The Muse connector

The owner's Muse agent reads offers on the relay with a key made in the
dashboard ([MUSE_CONNECTOR.md](MUSE_CONNECTOR.md)).

| Threat | Protection |
|---|---|
| Someone else gets the Muse key | 32 random bytes, stored only as its SHA-256; accepted only in the `Authorization` header; one per app, replaced or revoked at once from the dashboard, and on disconnect. It reads offers and hands out their links; approving still needs the PIN |
| Muse, or a prompt it read, tries to publish | No tool approves, publishes, takes a PIN or changes a setting. A reminder or cancel reaches the app signed and marked `via: "muse"`, and the app refuses any approval marked so |
| A Muse key on the app's relay routes | Refused on `/v1/`; the app's signature is refused on `/mcp` and `/muse/v1/` |
| A web page calling the connector | A request carrying another site's `Origin` is refused |
| Flooding | About 60 calls a minute per key, then `429` |

## The app and the relay

| Threat | Protection |
|---|---|
| Pretending to be the app | HMAC-SHA256 over the method, path and query, a timestamp, a nonce and the body's hash, with the device secret the app registered once using the relay owner's enrolment key. Uploaded video is signed as `UNSIGNED-PAYLOAD`, with what it is (command, clip, place, offset, size) in the signed query |
| Replaying a request | Timestamp within 5 minutes either way; nonce remembered for 15 minutes |
| Forging a decision | Commands are signed by the relay over canonical JSON; the app checks the signature before acting |
| A command delivered twice | Recorded by id in the app; applied once |
| An old approval for new clips | The offer's fingerprint covers each clip and its file; any change refuses the approval and the upload |
| A stale approval | Decided after the link expired, or too old when the app sees it: the offer expires and nothing is posted late. **Ask again** makes a new offer |
| Posting without the app agreeing | The relay allows Graph steps only after the app acknowledges the command as accepted, only for that command's clips and places, at most 3 container creations each, within 24 hours |
| A compromised relay | It can't read the app's files or reach the app. It gets a clip only for a command the app accepted, and before that a small preview of each offered clip if the app sends previews. It does hold the account tokens and signs commands, so a compromised relay could post to the connected accounts or, while phone approval is on, approve an open offer without the PIN. Deploy it from this repository and keep its secrets to yourself |

## What is shown and logged

- Meta's raw error text stays on the relay. The relay logs error codes only,
  never tokens.
- Every state change of an offer is in an audit log in the app and on the
  relay, with who made it (the app, the relay, the phone, Muse, the
  publisher).
- The phone-approval reads (`GET /remote-approval/...`) return no secret. A
  new Muse key is in one answer
  only, and is not stored or logged by the app.

## Phone approval is off by default

**Let me approve publishing from my phone** starts off, and can only be
switched on once a PIN is set and a Muse agent is connected.

## Reporting a problem

Please report a security problem privately to the repository's owner on
GitHub, not in a public issue.
