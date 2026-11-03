# Running it from source

On Windows, macOS or Linux, on your own computer. To host it for others, see
[RUNNING.md](RUNNING.md).

## What you need

- **Python 3.11.**
- **ffmpeg** (with ffprobe) on your `PATH`:
  - Windows: `winget install Gyan.FFmpeg`, then open a new terminal.
  - macOS: `brew install ffmpeg`
  - Debian or Ubuntu: `sudo apt install ffmpeg fonts-dejavu-core`
- **A Meta Model API key**, for Muse Voice, Muse Spark and SAM 3.1. Without
  one the app still runs: a job finishes on simple fallbacks (loudness picks,
  a centre crop) and says so in its warnings.
- **Node 22.5 or later**, only for the relay: its tests, and the mock relay
  below.

## Install

Windows (PowerShell):

```
git clone https://github.com/ColinGPT9/clips-kitty-gameday
cd clips-kitty-gameday
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
```

macOS or Linux:

```
git clone https://github.com/ColinGPT9/clips-kitty-gameday
cd clips-kitty-gameday
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

`requirements-dev.txt` is `requirements.txt` plus pytest, httpx and ruff.

## Start it

```
python -m gameday
```

1. Open http://127.0.0.1:8080. The browser asks for a user name and
   password: `owner`, and the password in `data/owner-password.txt`. The app
   makes that file on its first start, and the log says where it is.
   To choose your own password instead, set `GAMEDAY_PASSWORD` before
   starting.
2. **Setup → Meta key**: paste the key, press **Save**, then **Test**. Or set
   `META_API_KEY` before starting; the dashboard then shows "from the
   environment".
3. **Setup → Options**: Basketball, how many clips per game, "Teams or
   players" if you want to give Muse Spark a hint, and title cards.
4. **Game day**: drop a game, or a few minutes of one, on the upload box.
   The stepper shows Muse Voice, Muse Spark, SAM 3.1, 9:16 and Captions, each
   with its time and estimated cost.
5. **Clips**: play each clip. **How it follows the ball** shows the frame
   SAM 3.1 worked from, with its box and the crop.

Settings such as the port are environment variables, all listed in
[RUNNING.md](RUNNING.md). To set one for a single run:

- PowerShell: `$env:PORT = "8090"; python -m gameday`
- macOS or Linux: `PORT=8090 python -m gameday`

## A watched folder

1. **Setup → Watched folders**: add a folder on your computer that is not
   inside `data/`. (Or set `WATCH_DIR` before starting.)
2. Copy a game into it. The app takes a file once it has stopped changing for
   60 seconds, and makes one job for it. The file is only read.

## Phone approval, with the mock relay

The relay has a local mode with Meta faked: nothing is sent to WhatsApp and
nothing is posted anywhere.

1. In a second terminal: `cd meta-relay`, then `npm run dev`. It prints its
   address (`http://127.0.0.1:8787`) and an enrolment key.
2. **Setup → Relay**: that address and key, then **Connect**. (The app takes a
   plain `http://` address only for a relay on this computer.)
3. **Setup → PIN**: type a PIN of 4 to 8 digits, not one digit
   repeated or a run like 1234. Enter your PIN yourself. Never give it to an
   assistant.
4. **Setup → Muse**: **Make a Muse key**. It is shown once: copy it into an
   environment variable (`MUSE_KEY` below) rather than into a command.
5. **Setup → Facebook + Instagram**: **Connect** opens Meta's real sign-in
   page in a new tab, which can't finish against the mock. Copy that tab's
   address and finish the sign-in with the mock instead (from Git Bash, macOS
   or Linux):

   ```
   curl -X POST http://127.0.0.1:8787/__mock/oauth -H "Content-Type: application/json" \
     -d '{"url":"<that address>"}'
   ```

6. Optional, **Setup → WhatsApp**: start pairing to get a code like
   `LINK K7QM2X`, play the phone sending it, then confirm the number in the
   dashboard:

   ```
   curl -X POST http://127.0.0.1:8787/__mock/phone -H "Content-Type: application/json" \
     -d '{"text":"LINK K7QM2X"}'
   ```

7. Switch on **Let me approve publishing from my phone**.
8. Upload a short clip. When the job is done, the offer shows under **Phone
   offers**. Ask the relay for it the way Muse would:

   ```
   curl -H "Authorization: Bearer $MUSE_KEY" http://127.0.0.1:8787/muse/v1/offers
   curl -H "Authorization: Bearer $MUSE_KEY" http://127.0.0.1:8787/muse/v1/offers/<approval_id>/link
   ```

9. Open that link: watch the clips, tick them and the places, confirm, and
   enter your PIN. The app picks the approval up and publishes to the fake
   Graph API. `http://127.0.0.1:8787/__mock/state` lists the fake posts, and
   **Phone offers** shows the result.

## Tests

```
python -m ruff check .
python -m pytest -q
cd meta-relay && npm test
```

The provenance check needs a clone of clips-studio (the app as it was before
the event). It only reads that clone through git, at the default commit
6245e02, and fails on any block of five lines of code shared with it:

```
git clone https://github.com/ColinGPT9/clips-studio ../clips-studio
python scripts/provenance_check.py --reference ../clips-studio
```
