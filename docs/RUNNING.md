# Running it on a server

For whoever hosts Clips Kitty Game Day, for example in a container. Write
the Dockerfile from this page.

The app is one Python process. The web server, the job worker, the folder
watcher and the phone-approval loop all run inside it. It needs no
GPU: the models run on Meta's side, and the app only runs ffmpeg and ffprobe.

## What goes in the image

- Python 3.11.
- Two Debian or Ubuntu packages:
  - `ffmpeg`, which brings `ffmpeg` and `ffprobe`, the only programs the app
    runs;
  - `fonts-dejavu-core`, a font for the title cards when `GAMEDAY_FONT` isn't
    set.
- The repository, then `pip install -r requirements.txt` (exact versions).
- Start command: `python -m gameday`.

It needs no root. Run it as an ordinary user that owns `DATA_DIR`.

## Environment

Every variable is optional. The ones that hold secrets (`GAMEDAY_PASSWORD`,
`GAMEDAY_JUDGE_PASSWORD`, `GAMEDAY_SECRET_KEY`, `META_API_KEY`,
`RELAY_ENROLMENT_KEY`) belong in the host's secret store, never in the image
or in a file in git.

| Variable | Default | Meaning, and what to set when hosting |
|---|---|---|
| `DATA_DIR` | `./data` | The database, secrets, uploads and media. Set it to the volume, for example `/data`. |
| `HOST` | `127.0.0.1` | Set `0.0.0.0` in a container, so the proxy can reach the app. |
| `PORT` | `8080` | |
| `GAMEDAY_PASSWORD` | generated | The owner password (user `owner`). If unset, one is made on the first start and saved to `DATA_DIR/owner-password.txt` (mode 0600). The log says where it is, never what it is. If you set it, use at least 12 characters: with a shorter one the app refuses to start unless `HOST` is `127.0.0.1`. |
| `GAMEDAY_JUDGE_PASSWORD` | | The judge login's password (user `judge`), for people who should try the app without the owner password, such as the event's judges. The judge can upload games and see everything the dashboard shows, and change nothing else: no settings, keys, watched folders or phone setup. Its games are never offered on your phone ([API.md](API.md#the-judge-login)). Use at least 12 characters, not the owner password, or the app refuses to start. If unset, there is no judge login. |
| `GAMEDAY_SECRET_KEY` | generated | The Fernet key that encrypts stored secrets, such as a saved Meta key. If unset, it is kept in `DATA_DIR/secret.key` (0600). If you set it, never change it: a new key can't read what the old one saved. |
| `META_API_KEY` | | If set, used instead of a key saved in the dashboard, which then shows "from the environment". |
| `META_BUDGET_USD` | `25` | Meta calls are refused once estimated spending passes this. |
| `WATCH_DIR` | | If set, this folder is watched from the first start. |
| `MAX_UPLOAD_MB` | `4096` | The biggest upload the dashboard accepts. |
| `RELAY_URL`, `RELAY_ENROLMENT_KEY` | | Optional. They fill in the relay form in the dashboard. |
| `PUBLIC_URL` | | Optional. The dashboard's address as people open it, for example `https://203-0-113-7.sslip.io`. With it, tapping a phone alert opens the right page of the dashboard (**Game day** or **Phone offers**). `scripts/setup-lightsail.sh` sets it. |
| `NTFY_SERVER` | `https://ntfy.sh` | Where phone alerts go: ntfy.sh, or your own ntfy server. It must be `https://` (`http://` only on this computer), because each alert's address holds the secret channel name. Alerts are off until the owner makes a channel in **Setup**. |
| `FFMPEG`, `FFPROBE` | `ffmpeg`, `ffprobe` | Paths to the programs, if they aren't on `PATH`. |
| `GAMEDAY_KEEP_TMP` | `0` | `1` keeps each job's scratch files, for debugging. |
| `LOG_LEVEL` | `INFO` | |
| `GAMEDAY_FONT` | | Optional path to a `.ttf` font for the title cards. |

## Port and HTTPS

- The app listens on `PORT` (8080). Put an HTTPS reverse proxy in front of
  it, and never publish 8080 to the internet. The browser sends the owner
  password with every request (HTTP Basic auth), so it must only travel over
  HTTPS.
- The proxy must:
  - pass the `Authorization`, `X-Gameday-Action` and `Range` headers through;
  - allow request bodies up to `MAX_UPLOAD_MB` (4096 MB by default) and
    long requests on `POST /api/upload`: a game of a few GB can take many
    minutes to send;
  - stream the upload to the app rather than buffer it all first, if it can;
  - leave the app's own security headers (`Content-Security-Policy` and the
    rest) as they are;
  - set `X-Forwarded-For` to the visitor's address (Caddy does by default).
    After 10 wrong passwords from one address in 10 minutes, the app refuses
    that address for 15 minutes. It takes the address from the last
    `X-Forwarded-For` entry when the request comes from a private address
    (the proxy), and from the connection otherwise.
- Health check: `GET /health`, with no password, answers
  `{"ok": true, "app": "Clips Kitty Game Day", "version": "0.1.0"}`.
  Everything else needs the password.

## The volume to keep

`DATA_DIR` is the only thing to keep between restarts and upgrades. Back it
up, and treat the backup as secret: it holds the encrypted secrets and, unless
`GAMEDAY_SECRET_KEY` is set elsewhere, the key that opens them.

| Path | What |
|---|---|
| `DATA_DIR/gameday.db` | SQLite: settings, videos, jobs, clips, usage, offers |
| `DATA_DIR/owner-password.txt` | the generated owner password (0600), when `GAMEDAY_PASSWORD` isn't set |
| `DATA_DIR/secret.key` | the generated Fernet key (0600), when `GAMEDAY_SECRET_KEY` isn't set |
| `DATA_DIR/uploads/` | games uploaded through the dashboard |
| `DATA_DIR/media/jobs/<job_id>/clips/` | each job's clips, thumbnails and debug images, served at `/media/...` |
| `DATA_DIR/media/jobs/<job_id>/tmp/` | each job's scratch files, never served, removed at the end unless `GAMEDAY_KEEP_TMP=1` |

On the first start, read the owner password from `owner-password.txt` in the
volume, then sign in at `https://<your host>/` as `owner`. The judge login
has no file: its password is the `GAMEDAY_JUDGE_PASSWORD` you set. Give that
one to the judges, privately, and never the owner password.

Disk: plan for every uploaded game plus its clips. How much a game takes:
a 22.5-minute game was a 138 MB upload and made 173 MB of clips (8 clips,
measured 3 Nov 2026). CPU and memory: a server with 2 vCPUs and 4 GB RAM
(AWS Lightsail) clipped a 45-minute game in about 14 minutes (measured
3 Nov 2026).

## Getting games in

- **Through the dashboard.** Drag and drop on **Game day**, up to
  `MAX_UPLOAD_MB`. The file is streamed to `DATA_DIR/uploads/`. A bigger file
  gets 413, and a file ffprobe can't read as a video gets 400 and is removed.
- **Through a watched folder.** Mount a folder from the host, read-only is
  enough (watched files are only read), at a path outside `DATA_DIR`, for
  example `/watch`. Set `WATCH_DIR=/watch`, or add it under **Watched folders**
  in the dashboard. A folder inside `DATA_DIR` is refused. Copy games into it
  with any tool you like. A new video counts once it has stopped changing for
  60 seconds, and each one gets one job. A copy of a game already clipped (the
  same name and size) is skipped; upload it to clip it again.

## Network

- Inbound: HTTPS to the proxy only.
- Outbound: HTTPS to `api.meta.ai` (Muse Voice, Muse Spark, SAM 3.1), to
  your relay's address and, once phone alerts are on, to `NTFY_SERVER`
  (`ntfy.sh` by default). Nothing else. The relay, not the app, talks to the
  Graph API.
- With the shared-folder sync on (`scripts/setup-drive-sync.sh`): HTTPS to
  Google Drive, from rclone, not the app. Every minute it copies new games
  from one Drive folder into the watched folder, with a read-only sign-in.

## One process only

Run exactly one process, in one container, for each `DATA_DIR`. No extra
web server workers and no scaling out: the job worker, the watcher and the
phone-approval loop live inside the process, and SQLite is its database.

## Checking it

1. `curl https://<your host>/health` answers `{"ok": true, ...}`.
2. Sign in as `owner`. In **Setup**, save the Meta key (or set
   `META_API_KEY`) and press **Test**.
3. Upload a short clip and watch the stepper on **Game day**.
4. With `GAMEDAY_JUDGE_PASSWORD` set, sign in as `judge` in a private
   window: the dashboard says **Judge view** and has no **Setup**.
