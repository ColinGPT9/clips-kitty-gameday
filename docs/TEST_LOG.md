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
    Total $0.649, about $0.081 a clip.
  - Share of each clip that followed the ball: 0%, 84%, 80%, 79%, 88%, 89%, 89% and 72%.
    The 0% clip is the celebration after the buzzer, where it followed the players.
  - Disk: the game was 138 MB, and its clips came to 173 MB.
- **Tests at this commit (CI):**
  - 415 Python tests on Ubuntu and Windows (4 live tests left out).
  - 105 relay tests.
  - ruff and the provenance check, both clean.
