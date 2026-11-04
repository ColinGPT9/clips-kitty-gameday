# Test log

Runs are added here as they happen, newest last. Every number is measured
on the run it describes: nothing is estimated, rounded up or carried over
from another run. A run that faked Meta's servers says so in its heading;
anything else called Meta's real services.

Each run gives:

- the date, and where it ran (machine or host, OS, Python, Node, ffmpeg);
- the commit it ran at;
- what ran, and the exact command;
- the result: tests passed and failed, or for a game, its length, the
  clips made, the time per stage, the estimated cost per model, the share of
  each clip that followed the ball, and what got posted, with the links;
- what was found, and what was fixed.

## 3 Nov 2026, about 02:00 ET: a real 22.5-minute game, engine only

- **Where:**
  - Windows 10 desktop (AMD Ryzen 5 5600X).
  - Python 3.11.0 and ffmpeg N-124941.
  - Meta's real services.
- **Commit:** 7c8dc77.
- **Command:** `python scripts/run_pipeline.py <game.mp4> <out> --max-clips 8`, with `META_API_KEY` set.
- **Result:**
  - A 1,347 s game made 8 clips in 208 s.
  - Time per stage: Muse Voice 88 s, Muse Spark 27 s, SAM 3.1 50 s, render 42 s.
  - Estimated cost: Muse Voice $0.067, Muse Spark $0.024, SAM 3.1 $0.543. Total $0.635.
  - Share of each clip that followed the ball: 87%, 0%, 86%, 68%, 79%, 93%, 75% and 93%.
    The 0% clip is the post-game celebration, where it followed the players.

## 3 Nov 2026, 02:30-03:00 ET: end to end on the real app, relay and Graph API faked

- **Where:** the same PC, Node 22.11.0.
- **Setup:** the app (`python -m gameday`) and the relay's local mock
  (`meta-relay/test/serve.mjs`), which fakes Meta sign-in and the Graph API.
- **Commit:** c3549ea.
- **Without a Meta key:**
  - A 90 s clip made 5 clips in 22-24 s, on the loudness fallback, with a plain warning.
  - The offer reached Muse.
  - The approval page played the preview.
  - One clip was approved to Instagram and Facebook with the PIN.
  - The app reported "2 of 2 posts published" with both links.
- **With Meta's real services (budget $1, 3 clips per game):**
  - Total $0.0952.
  - Muse Voice $0.0045 (16 s), Muse Spark moments $0.013 (25 s), SAM 3.1
    $0.0736 (2 calls, 368 frames), Muse Spark captions $0.0041.
  - It made 1 clip, which followed the ball 40% of the time and the players the rest.
- **Found:** a review found 36 issues (1 high, 9 medium, 26 low). 28 were
  fixed and merged in b23ae37. The high one: clips with a title card
  didn't render with the default relative `DATA_DIR`.

## 3 Nov 2026, about 03:20 ET: the same game on merged main

- **Where:** the same PC and command, Meta's real services.
- **Commit:** b23ae37.
- **Result:**
  - 8 clips in 227 s.
  - Time per stage: Muse Voice 95 s, Muse Spark 28 s, SAM 3.1 53 s, render 52 s.
  - Estimated cost: Muse Voice $0.067, Muse Spark $0.023, SAM 3.1 $0.558.
    Total $0.649.
  - Share of each clip that followed the ball: 0%, 84%, 80%, 79%, 88%, 89%, 89% and 72%.
    The 0% clip is the celebration after the buzzer, where it followed the players.
  - Disk: the game was 138 MB, and its clips came to 173 MB.
- **Tests at this commit (CI):**
  - 415 Python tests on Ubuntu and Windows (4 live tests left out).
  - 105 relay tests.
  - ruff and the provenance check, both clean.

## 3 Nov 2026: an HD game, the demo footage

- **Where:** the same PC and command, Meta's real services.
- **Commit:** 1a997c1.
- **Footage:** the second half of Navy vs Air Force at the 2024 Armed Forces
  Men's Basketball Championship: 45 minutes cut from the DVIDS video, 1080p,
  public domain, with live commentary.
- **Result:**
  - 2,700 s of game made 8 clips in 347 s.
  - Time per stage: Muse Voice 151 s, Muse Spark 31 s, SAM 3.1 94 s, render 70 s.
  - Estimated cost: Muse Voice $0.135, Muse Spark $0.031, SAM 3.1 $0.704.
    Total $0.870.
  - Share of each clip that followed the ball: 46%, 80%, 43%, 59%, 73%, 81%, 66% and 63%.
    The rest of each clip followed the players.
- **Found:** some clips ran past the play into the broadcast's close-ups. Each
  clip should end at the first camera cut after the play.

## 3 Nov 2026, evening: the hosted server and a live post

- **Where:** the hosted server, on AWS Lightsail in Ohio (2 vCPUs, 4 GB RAM),
  with Meta's real services. The numbers are from the server's own job
  records.
- **Commit:** f59dcec, for the three game runs.
- **What ran:**
  - The HD demo game, the second half of Navy vs Air Force (45.0 minutes),
    copied into the server's watched folder. The app found it and clipped it.
  - It ran three times, because the same file was copied in twice more by
    mistake (see Found).
  - Then one clip was approved on the phone with the PIN and posted.
- **Result:**
  - Each run made 8 clips.
  - Time and estimated cost of each whole run: 874 s and $0.8014, 824 s and
    $0.8345, 806 s and $0.7966.
  - Time per stage, run by run:
    - Muse Voice 183, 173 and 185 s.
    - Muse Spark 40, 40 and 38 s.
    - SAM 3.1 368, 314 and 320 s.
    - Render 257, 279 and 244 s.
    - Captions 25, 18 and 19 s.
  - Estimated cost per stage, over the three runs: Muse Voice $0.135 each
    time, Muse Spark $0.028-0.032, SAM 3.1 $0.624-0.658, captions (Muse
    Spark) $0.009-0.010.
  - Posted: the approved clip went up as an Instagram Reel and a Facebook
    Page Reel.
- **Found:**
  - The same game was clipped three times. A copy of a file keeps its name
    and size but gets a new time, and the watcher knew a file by its path,
    size and time, so it took each copy for a new video. Fixed since: a
    watched file with the name and size of one a watch has queued before is
    skipped, with one line in the activity list. An upload on Game day still
    clips a game again.
  - The first posts that evening were refused with a Meta permission error.
    Meta had paused the developer account for an account confirmation
    ("unusual activity"), and a paused account's apps are switched off. Once
    the owner had confirmed the account and reconnected Facebook and
    Instagram, the post went through. The app's message for that refusal
    now says to finish any confirmation Meta asks for on
    developers.facebook.com (82fc073).

## 3 Nov 2026, late evening: games from Google Drive on the hosted server

- **Where:** the hosted server (AWS Lightsail, 2 vCPU / 4 GB), with `scripts/setup-drive-sync.sh` set up
  on a read-only Drive sign-in, syncing the Drive folder "Game Day" every minute.
- **What ran:** a 90-second game file was uploaded to that Drive folder from a browser.
- **Result:** the server copied it in and queued it as a watched-folder job on its own, with nothing
  uploaded to the server by hand.
- **Found:** the first sign-in paste failed. rclone 1.75 on Windows prints the token packed in
  base64, and the server's rclone 1.60 wants the JSON. `scripts/drive_token.py` now accepts
  either form (2117d1e).

## 4 Nov 2026, early morning: from Google Drive to a post, on the hosted server

- **Where:**
  - The hosted server, on AWS Lightsail in Ohio (2 vCPUs, 4 GB RAM), with
    Meta's real services. It ran as job 6 there.
  - The owner's iPhone, for the phone alerts (ntfy, through ntfy.sh), the
    Muse app and the approval page.
- **Commit:** 8fe166c, with PR #1 merged. Muse Spark now writes each offer's
  one-line summary, and that line leads the "highlights ready" phone alert.
- **What ran:**
  - A 4-minute piece of the demo game, "Navy vs Air Force.mp4" (241 s, cut
    from the 45-minute second half), was uploaded to the shared Google Drive
    folder "Game Day" at about 04:19 ET.
  - The server found it and clipped it, with nothing uploaded to the server
    by hand.
  - Then the owner approved one clip on the phone with the PIN, and it was
    posted.
- **Result:**
  - The server found the file at 04:19:59. The job took 5 min 34 s and
    finished at 04:25:34.
  - It made 4 clips. Estimated cost: about $0.34 in all.
  - Share of each clip that followed the ball: 82% (a big moment), 70% (a
    dunk), 83% (a shot off the glass) and 76% (the 4th clip, titled only
    "Highlight").
  - The "highlights ready" alert reached the iPhone. It opened with Muse
    Spark's line, which counted the 4 clips and named three of the plays.
  - In the Muse app, "Clips Kitty, what highlights are ready?" listed the
    offer and handed over the approval link.
  - On the approval page, the owner ticked the big-moment clip, Instagram
    Reels and Facebook Page Reels. Then they confirmed, entered the PIN and
    pressed **Approve and publish**.
  - Posted: an Instagram Reel on @clipskitty2 and a Facebook Page Reel ("2
    published"). The "Posted to Instagram + Facebook" alert followed, and
    Muse answered "what got posted" with that post.
- **Tests at this commit (CI):**
  - 571 Python tests on Windows.
  - 568 Python tests on Ubuntu, where 3 Windows-only tests are skipped.
  - 4 live tests left out on both.
  - 105 relay tests.
  - ruff and the provenance check, both clean.
- **Found:** one clip got only the plain title "Highlight". The other three
  got a title about their play.
