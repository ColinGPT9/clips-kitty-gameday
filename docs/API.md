# The app's HTTP API

This is the contract between the platform (routes) and the dashboard
(`gameday/static/`). Both follow it exactly.

## Rules for every route

- Everything except `GET /health` needs HTTP Basic auth: user `owner`, and the
  password from `GAMEDAY_PASSWORD` (or the generated one, see Environment).
  When `GAMEDAY_JUDGE_PASSWORD` is set, user `judge` can sign in too, with
  less it may do (see The judge login).
  A wrong or missing password gets 401 with `WWW-Authenticate: Basic`.
  After 10 wrong passwords (not missing ones, either user's) from one address
  within 10 minutes, that address gets 429 with `Retry-After` for 15 minutes,
  whatever password it sends. Behind a proxy on a private address, the address
  is the last `X-Forwarded-For` entry.
- Every POST, PUT, PATCH and DELETE also needs the header `X-Gameday-Action: 1`.
  Without it the answer is 403. That way a page on another site can't post to the app.
- An error answer is `{"error": "<a sentence for people>"}` with a 4xx or 5xx status.
- Times are Unix seconds (float). Money is in USD and is always an estimate.
- Every answer carries these headers:
  - `Content-Security-Policy: default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; style-src 'self'; script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'`
  - `X-Content-Type-Options: nosniff`
  - `Referrer-Policy: no-referrer`

  So the dashboard has no inline scripts, no inline styles, no `style=`
  attributes, and loads nothing from other sites.

## The judge login

The event's judges sign in as `judge`, with the password in
`GAMEDAY_JUDGE_PASSWORD`. Without that variable there is no judge login. The
judge can try the app, but can't change its setup or reach the owner's phone
and accounts.

| The judge may | Routes |
|---|---|
| Open the dashboard | `GET /`, `GET /static/{file}` |
| Read what the dashboard shows | `GET /api/status`, `/api/settings`, `/api/watches`, `/api/jobs`, `/api/jobs/{id}`, `/api/clips`, `/api/usage`, `/api/events`, `/remote-approval/status`, `/remote-approval/reviews`, and the clips under `GET /media/...` |
| Upload a game | `POST /api/upload` |
| Cancel or retry a game uploaded with the judge login | `POST /api/jobs/{id}/cancel`, `POST /api/jobs/{id}/retry` |

- Everything else gets 403 before any route runs:
  `{"error": "The judge login can't change this."}` for a change, and
  `{"error": "The judge login can't open this."}` for a read. That covers the
  settings, the Meta key, watched folders, the phone alerts, an offer's
  details, the audit log, and every change under `/remote-approval/` (the
  relay, the PIN, the Muse key, the accounts and the offers). A route added
  later is the owner's alone until `gameday/access.py` lists it.
- Cancelling or retrying the owner's game gets 403: "The judge login can only
  cancel or retry games uploaded with it."
- None of these reads returns a secret: no answer ever holds the Meta key,
  the relay's device secret, the phone alert channel or a password.
- A game uploaded with the judge login has `"created_by": "judge"`, and so has
  a retry of it. It runs on the same models, so `META_BUDGET_USD` caps its
  spending as before. It is never offered on the owner's phone: its `captions`
  stage is skipped with the note "Uploaded with the judge login, so it isn't
  offered on the phone.", and its failure sends no message.
- The judge's wrong passwords count toward the same lockout as the owner's,
  and a right judge password forgives none of them.
- `GET /api/status` says who signed in (`"role"`). For the judge the dashboard
  shows "Judge view" and hides Setup and every control the judge can't use.

## Pages and files

| Route | What |
|---|---|
| `GET /health` | `{"ok": true, "app": "Clips Kitty Game Day", "version": "0.1.0"}`. No auth. |
| `GET /` | `gameday/static/index.html` |
| `GET /static/{file}` | files in `gameday/static/` (`app.js`, `app.css`, `icon.svg`) |
| `GET /media/jobs/{job_id}/clips/{file}` | a job's clips, thumbnails and debug images. Only `.mp4`, `.jpg` and `.png`, only under `DATA_DIR/media/jobs/*/clips/`. Byte ranges are supported. |

## The app

### `GET /api/status`
```json
{"version": "0.1.0",
 "role": "owner",
 "meta_key": {"saved": true, "from_env": false},
 "worker": {"busy": true, "job_id": 4, "queued": 1},
 "budget": {"limit_usd": 25.0, "spent_usd": 1.37},
 "watches": 1,
 "upload_limit_mb": 4096,
 "alerts": {"ntfy": true}}
```
`role` is who signed in: `"owner"` or `"judge"` (see The judge login).
`alerts.ntfy` is whether phone alerts are on (see Phone alerts); the channel's
name is never in it.
The relay, PIN, Muse and accounts are in `GET /remote-approval/status` (below).

### Settings
- `GET /api/settings` gives `{"mode": "basketball", "max_clips": 8, "hint": "", "title_cards": true, "caption_credit": ""}`.
  `mode` is `"basketball"` or `"talk"`, `max_clips` is 1 to 12, and `hint` is "Teams or players" (at most 200 characters).
- `caption_credit` is the credit line added to the end of every Instagram and Facebook caption, after
  the hashtags and a blank line (`""` for none). It is trimmed, each line too, and may have at most
  300 characters and 3 lines. Muse Spark never sees it. A caption too long for its platform is shortened
  in its body, never in the credit line. Each offer keeps the line it was made with, so the approval page
  shows exactly what is posted: the line its job was queued with, or, for a job queued without one, the
  line set when its clips are offered.
- `PATCH /api/settings` takes any of those fields and returns the whole object.
  New jobs use the settings at the time they are queued. A value out of range is refused with 400
  and a sentence saying why.

### The Meta key
- `PUT /api/meta/key` with `{"key": "..."}` gives `{"saved": true}`.
  The key is never sent back by any route.
- `DELETE /api/meta/key` gives `{"saved": false}`.
- `POST /api/meta/test` gives `{"ok": true}`, or `{"ok": false, "error": "..."}`.
  It makes one cheap call. When `META_BUDGET_USD` stopped the call before it
  reached Meta, the answer also has `"budget": true`.

### Phone alerts

Optional push alerts on the owner's phone through ntfy (https://ntfy.sh), a
free, open-source notification app for iPhone and Android with no account.
It is a notification service, not a model. The phone subscribes to a channel
(an ntfy topic). Anyone who knows its name can read the alerts, so the name is
long, random and kept like a password.

| Route | Answer |
|---|---|
| `POST /api/alerts/ntfy` | `{"topic": "clipskitty-...", "subscribe": {"web": "https://ntfy.sh/clipskitty-...", "app": "ntfy://ntfy.sh/clipskitty-..."}, "alerts": {"ntfy": true}}`. A new channel, replacing any older one. This answer is the only one that ever holds its name |
| `DELETE /api/alerts/ntfy` | `{"alerts": {"ntfy": false}}`. Alerts stop |
| `POST /api/alerts/test` | `{"ok": true}`, or `{"ok": false, "error": "..."}`. Sends "Test alert from Clips Kitty Game Day" |

- The name is `clipskitty-` and 24 random URL-safe characters, kept encrypted
  with the other secrets. No log line, event, error or other answer holds it;
  `GET /api/status` says only whether alerts are on.
- The judge login gets 403 on all three.
- Each alert is one HTTPS POST to `NTFY_SERVER/<channel>`, with a 10-second
  timeout, from a thread of its own, so a slow ntfy never holds up a game or
  the phone loop. An alert that isn't sent adds one `warn` event, "Phone alert
  not sent: <reason>", and nothing else.

What alerts, for the owner's games only (a game uploaded with the judge login
never alerts):

| When | Title | Message | Opens |
|---|---|---|---|
| A game finishes with clips | `<video name>: 5 highlights ready` | Offered on the phone: Muse Spark's one-line summary of the offer (the best clip's title when it wrote none), then "Ask Muse what's ready, or open Phone offers." Otherwise the best clip's title, then "Open Game day to watch them." | Game day |
| A game fails | `Game didn't finish: <video name>` | The error, with any file path cut to the file's name | Game day |
| An approval's uploads have all finished | `Posted to Instagram + Facebook` (the places that worked), or `Posting failed` | The clips' titles, and why a post didn't go up, with Meta's error code | Phone offers |

`<video name>` is the file's name without its extension. An approval sends
one alert, once all its uploads have finished. Tapping an alert opens the page
in the last column when `PUBLIC_URL` is set.

### Upload
`POST /api/upload?filename=game.mp4`, where the body is the raw file
(`Content-Type: application/octet-stream`). The body is streamed to
`DATA_DIR/uploads/`.

| Result | Status | Answer |
|---|---|---|
| Accepted | 200 | `{"video_id": 7, "job_id": 9}`; a job is queued with the current settings |
| Over `MAX_UPLOAD_MB` | 413 | |
| Not a video ffprobe can read | 400 | the file is removed |

The dashboard sends it with `XMLHttpRequest` so it can show upload progress.

### Watched folders
| Route | Body | Answer |
|---|---|---|
| `GET /api/watches` | | `[Watch]` |
| `POST /api/watches` | `{"path": "/watch"}` | `Watch` |
| `PATCH /api/watches/{id}` | `{"enabled": false}` | `Watch` |
| `DELETE /api/watches/{id}` | | `{"deleted": true}` |

`Watch`:
```json
{"id": 1, "path": "/watch", "enabled": true, "last_checked_at": 1762230000.0,
 "videos": 3, "error": ""}
```
- The folder must exist and be readable, and must not be inside `DATA_DIR`; otherwise the answer is 400.
- A new video file counts once it has stopped changing for 60 seconds. Each one gets one job.
- A file with the same name and size as one already clipped from a watched folder is a copy: it is
  skipped, with one event naming the earlier job. To clip a game again, upload it.
- Watched files are only read.

### Jobs
| Route | Answer |
|---|---|
| `GET /api/jobs?limit=20` | `[Job]`, newest first, without `clips_list` |
| `GET /api/jobs/{id}` | `Job` with `"clips_list": [Clip]` |
| `POST /api/jobs/{id}/cancel` | `Job` (queued or running) |
| `POST /api/jobs/{id}/retry` | the new `Job` (failed or cancelled only; 409 while an earlier retry of it is queued, running or done) |

`Job`:
```json
{"id": 9, "video": {"id": 7, "name": "game.mp4", "duration_s": 1350.0, "source": "upload"},
 "state": "running",
 "stage": "sam", "stage_progress": 0.4, "message": "Following the ball in clip 3 of 8",
 "stages": {"voice": {"state": "done", "seconds": 41.2, "cost_usd": 0.07, "note": ""},
            "spark": {"state": "done", "seconds": 12.0, "cost_usd": 0.02, "note": ""},
            "sam": {"state": "running", "seconds": 30.5, "cost_usd": 0.31, "note": ""},
            "render": {"state": "waiting", "seconds": 0, "cost_usd": 0, "note": ""},
            "captions": {"state": "waiting", "seconds": 0, "cost_usd": 0, "note": ""}},
 "cost_usd": 0.40, "clips": 0, "warnings": [], "error": "",
 "created_at": 1762230000.0, "started_at": 1762230002.0, "finished_at": null,
 "created_by": "owner"}
```
- `state` is one of `queued`, `running`, `done`, `failed` or `cancelled`.
- `stage` is null or one of `voice`, `spark`, `sam`, `render` or `captions`.
- A stage's `state` is one of `waiting`, `running`, `done`, `skipped` or `failed`.
- `source` is `upload` or `watch`.
- `created_by` is `judge` for a game uploaded with the judge login (or a retry
  of one), and `owner` for every other game, a watched folder's included.

`Clip`:
```json
{"id": 31, "job_id": 9, "index": 1, "title": "Corner three ties it", "category": "threes",
 "label": "Corner three to tie it", "score": 92.0, "team": "", "player": "", "evidence": "...",
 "start_s": 412.3, "end_s": 431.0,
 "follow": {"source": "ball", "ball_coverage": 0.74, "note": "Followed the ball 74% of the time"},
 "video_url": "/media/jobs/9/clips/clip_01.mp4", "thumb_url": "/media/jobs/9/clips/clip_01.jpg",
 "debug_url": "/media/jobs/9/clips/clip_01_debug.jpg"}
```
`GET /api/clips?job_id=9&limit=50` gives `[Clip]`. Without `job_id` it returns the newest jobs' clips first.

### Usage and activity
`GET /api/usage`:
```json
{"total_usd": 1.37, "budget_usd": 25.0,
 "by_model": {"muse-spark-1.3": {"calls": 12, "usd": 0.11},
              "muse-voice-transcribe-1.0": {"calls": 3, "usd": 0.07},
              "sam-3.1": {"calls": 8, "usd": 0.48, "frames": 2400}},
 "recent": [{"at": 1762230000.0, "model": "sam-3.1", "purpose": "track", "ok": true, "status": 200,
             "ms": 17300, "cost_usd": 0.08, "job_id": 9, "error": ""}]}
```
`recent` holds the last 50 calls. The Graph API counts stay in `GET /remote-approval/status`, as this week's code reports them.

`GET /api/events?after=0&limit=100`:
```json
[{"id": 1, "at": 1762230000.0, "level": "info", "kind": "job", "message": "...", "job_id": 9}]
```
Events come oldest first. `level` is `info`, `warn` or `error`.

## Phone approval, Muse and accounts

These are the `/remote-approval/...` routes from this week's
`server/remote_approval_api.py` (event branch at 0e811c3), with the same paths
and the same JSON in and out. That file is the reference for each body and
answer:

- `GET /remote-approval/status`
- `PATCH /remote-approval/settings`
- `POST /remote-approval/register`
- `POST /remote-approval/refresh`
- `POST /remote-approval/pair/start` (WhatsApp pairing; not used by the dashboard)
- `POST /remote-approval/pair/confirm` (WhatsApp pairing; not used by the dashboard)
- `POST /remote-approval/pin`
- `POST /remote-approval/muse/token`
- `POST /remote-approval/muse/revoke`
- `POST /remote-approval/test` (a WhatsApp test message; not used by the dashboard)
- `POST /remote-approval/disconnect`
- `POST /remote-approval/accounts/connect`
- `POST /remote-approval/accounts/disconnect`
- `POST /remote-approval/templates` (WhatsApp message templates; not used by the dashboard)
- `GET /remote-approval/reviews`
- `GET /remote-approval/reviews/{id}`
- `POST /remote-approval/reviews/{id}/cancel`
- `POST /remote-approval/reviews/{id}/retry`
- `POST /remote-approval/reviews/{id}/ask-again`
- `POST /remote-approval/reviews/{id}/resend`
- `GET /remote-approval/audit`

Only two things change:
- The rules above (Basic auth and `X-Gameday-Action`) replace the desktop session.
- Any file path in an answer becomes a `/media/...` URL.

## Environment

The app reads these in `gameday/config.py`. `docs/RUNNING.md` explains them for hosting.

| Variable | Default | Meaning |
|---|---|---|
| `DATA_DIR` | `./data` | the database, secrets, uploads and media |
| `HOST` | `127.0.0.1` | |
| `PORT` | `8080` | |
| `GAMEDAY_PASSWORD` | generated | the owner password. If unset, one is generated on first start and saved to `DATA_DIR/owner-password.txt` (mode 0600). The log says where it is, never what it is. |
| `GAMEDAY_JUDGE_PASSWORD` | | the password of user `judge` (see The judge login). If unset, there is no judge login. Shorter than 12 characters, or the same as the owner password, and the app refuses to start. |
| `GAMEDAY_SECRET_KEY` | generated | the Fernet key that encrypts stored secrets. If unset, it is kept in `DATA_DIR/secret.key` (0600). |
| `META_API_KEY` | | if set, used instead of the saved key; the dashboard shows "from the environment" |
| `META_BUDGET_USD` | `25` | Meta calls are refused once estimated spending passes this |
| `WATCH_DIR` | | if set, this folder is watched from the first start |
| `MAX_UPLOAD_MB` | `4096` | |
| `RELAY_URL`, `RELAY_ENROLMENT_KEY` | | optional; they fill in the relay form |
| `PUBLIC_URL` | | optional; the dashboard's address, such as `https://203-0-113-7.sslip.io`. With it, tapping a phone alert opens the right page. `scripts/setup-lightsail.sh` sets it |
| `NTFY_SERVER` | `https://ntfy.sh` | the ntfy server phone alerts go to. `https://` only (`http://` on this computer) |
| `FFMPEG`, `FFPROBE` | `ffmpeg`, `ffprobe` | |
| `GAMEDAY_KEEP_TMP` | `0` | `1` keeps each job's scratch files |
| `LOG_LEVEL` | `INFO` | |

- **Start:** `python -m gameday` runs the app, one process with its background threads.
- **Data:**
  - `DATA_DIR/gameday.db` (SQLite)
  - `DATA_DIR/uploads/`
  - `DATA_DIR/media/jobs/<job_id>/clips/`, which is served
  - `DATA_DIR/media/jobs/<job_id>/tmp/`, which is never served
