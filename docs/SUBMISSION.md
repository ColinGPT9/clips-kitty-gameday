# Submission draft: Clips Kitty Game Day

**Tagline:** Give it a basketball game: Meta's models cut vertical
highlights that follow the ball, and nothing is posted until you approve it
on your phone.

**Repository:** https://github.com/ColinGPT9/clips-kitty-gameday (public,
AGPL-3.0-or-later)

**Demo video:** TODO

**Try it:** https://18-190-220-192.sslip.io, user `judge`, with the password
in the submission's private field. No account to make
([How to try it](#how-to-try-it)).

## What existed before this event, and what was built during it

- **Before this event (not in this repository):** Clips Kitty
  ([clips-studio](https://github.com/ColinGPT9/clips-studio)), a desktop
  clipping app with its own local vision models. It is not part of this
  repository, and nothing here depends on it.
- **Built during the event:** everything in this repository.
  - The phone relay (`meta-relay/`), the approval flow and the Instagram and
    Facebook publishing were written from 29 Oct to 2 Nov 2026 for this
    hackathon, on an event branch, and brought here.
  - The web app and its Meta-only engine were written here from 3 Nov 2026.
- [PROVENANCE.md](../PROVENANCE.md) lists every imported file with its source
  commit and date. CI compares the repository with clips-studio at 6245e02
  (28 Oct 2026) and fails on any shared block of five lines of code.

## The problem

Highlights travel furthest while the game is still trending. Posts that land
then get the team more reach. But the person who runs a team's socials is at
the game, not at an editing desk. Cutting, reframing for a phone screen and
posting each play by hand takes too long.

With Clips Kitty Game Day, the clips are ready in minutes. They go out once
that person approves them on their phone, while the game is still trending.

## What it does

1. You give it a game: drop the file on the dashboard, or copy it into a
   watched folder.
2. Muse Voice transcribes the commentary. Muse Spark picks the plays and
   writes their titles and captions. SAM 3.1 finds the ball, and ffmpeg cuts
   9:16 clips whose crop follows it.
3. When the game is done, the clips are offered on your phone. With phone
   alerts on, an alert says so, led by Muse Spark's one-line summary. Ask
   Muse what's ready (it doesn't message you first), or look at the
   dashboard, and Muse hands you the approval link.
4. On the approval page you watch them, tick the ones you want and where
   they go, confirm, and enter your PIN. Only then are they posted, as
   Instagram Reels and Facebook Page Reels, through Meta's Graph API.

The dashboard shows each stage as it runs, with its time and estimated
cost, the clips with how much of each followed the ball, the offers and what
got posted, and the spending against a budget.

## Meta's tools

| Meta tool | Use |
|---|---|
| **SAM 3.1** (`sam-3.1`, hosted by Meta) | Finds the ball in each play, so the 9:16 crop follows it. Each clip records how much of it followed the ball, and the dashboard shows the frame with SAM's box and the crop |
| **Muse Spark** (`muse-spark-1.3`) | Reads the commentary and picks the moments, held to a strict JSON schema: each play's window, category, score and the commentary behind it. Writes each clip's title and caption, and the one-line summary of each offer that Muse reads out, the approval page shows and the phone alert carries |
| **Muse Voice** (`muse-voice-transcribe-1.0`) | Transcribes the game's commentary with times, for Muse Spark to read |
| **The Muse connector** | A custom connector for Muse, Meta's agent app, on the owner's relay: an MCP server with six tools, plus the same tools as HTTPS routes with an OpenAPI description. Muse says what's ready, hands over the approval link, says what got posted, and puts an offer aside or cancels it. No tool approves or posts. Ran in the Muse app on 2 Nov 2026 on the event branch's relay, and on 4 Nov 2026 with this app on the hosted server ([MUSE_CONNECTOR.md](MUSE_CONNECTOR.md)) |
| **Graph API Reels** | Instagram Reels (resumable upload, container status, publish, permalink) and Facebook Page Reels (start, upload, finish, status, permalink), made by the relay only for what was approved |
| **Facebook Login for Business** | Connects the Page and its Instagram account. The tokens stay on the relay |

Only Meta's models are called at runtime. `tests/test_meta_only.py` fails on
any other AI package, import or model host, and if the app's model list is
anything but these three. ffmpeg cuts and renders; it is a tool, not a model.

**What calls Meta's models, and what's a regular service:**

| Kind | Parts |
|---|---|
| **Meta model calls (AI)** | Muse Voice, Muse Spark and SAM 3.1, through the Meta Model API. Nothing else runs AI. |
| **Meta services (not AI)** | The Instagram Graph API and Facebook Page Reels, Facebook Login for Business, and the Muse app's custom connector |
| **Regular services and tools (not AI)** | AWS Lightsail, Caddy, Cloudflare Workers with D1 and KV (the relay), ffmpeg, SQLite, Python, FastAPI. Optional: Google Drive with rclone, ntfy (phone alerts) |

## Safety

- Nothing is posted without the approval page, the confirm box and the PIN.
  The PIN is never given to Muse or any assistant.
- The app is behind HTTPS and an owner password. The relay never calls the
  app; Meta's app secret and the account tokens live only on the relay.
- Judges get a login of their own. It can upload and look, but it can't
  change settings or keys, or reach the owner's phone or accounts.
- Every Meta call checks a budget first and is logged with its estimated
  cost.

More in [SECURITY.md](SECURITY.md).

## What has run

- **This web app on a real game (3 Nov 2026):** a 22.5-minute game made 8
  vertical clips in 227 s on a desktop PC.
  - Cost: about $0.65 of Meta usage for the game. Muse Voice $0.07, Muse
    Spark $0.02, SAM 3.1 $0.56.
  - Ball-following: in the 7 clips of live play, SAM 3.1 kept the 9:16
    window on the ball 72-89% of the time. The 8th clip, the celebration
    after the buzzer, followed the players.
  - Details in [TEST_LOG.md](TEST_LOG.md).
- **The HD demo game (3 Nov 2026):** the second half of Navy vs Air Force at
  the 2024 Armed Forces Men's Basketball Championship (45 minutes, 1080p,
  public domain).
  - It made 8 clips in 347 s, for about $0.87 of Meta usage.
  - SAM 3.1 kept the window on the ball 43-81% of the time, and on the players the rest.
- **The same game on the hosted server (3 Nov 2026):** the 45-minute game
  went into the watched folder of an AWS Lightsail server with 2 vCPUs and
  4 GB RAM. It made 8 clips in about 14 minutes, for about $0.80 of Meta
  usage.
- **A live post from the hosted server (3 Nov 2026):** one clip was approved
  on a phone with the PIN and posted as an Instagram Reel and a Facebook
  Page Reel.
- **From Google Drive to a post, on the hosted server (4 Nov 2026):** at
  commit 8fe166c.
  - A 4-minute piece of the demo game was uploaded to a shared Google Drive
    folder that the server watches. The server found it on its own and made
    4 clips in 5 min 34 s. SAM 3.1 kept the window on the ball 70-83% of
    the time.
  - A phone alert reached the owner's iPhone, led by Muse Spark's one-line
    summary of the clips.
  - In the Muse app, "Clips Kitty, what highlights are ready?" listed the
    offer and handed over the approval link.
  - On the approval page, one clip was ticked for Instagram Reels and
    Facebook Page Reels, confirmed and approved with the PIN.
  - It went up as an Instagram Reel on @clipskitty2 and a Facebook Page
    Reel. A "Posted to Instagram + Facebook" alert followed, and Muse said
    what got posted.
- **Cost to post:** Instagram and Facebook Reels go through Meta's Graph
  API at no charge. Muse Spark's captions for a game's 8 clips cost about a
  cent.
- **Tests:** at commit 8fe166c, 571 Python tests (on Ubuntu and Windows,
  with 4 live tests left out) and 105 relay tests pass in CI, along with
  the provenance check.
- On the event branch, on 1 Nov 2026, the relay and the phone's approval
  page ran live: one clip was approved on a phone with the PIN and posted
  through Meta's APIs as an Instagram Reel and a Facebook Page Reel.
- On the same event branch's relay, on 2 Nov 2026, the Muse app connected
  as a custom connector (the remote MCP server, with the key in a header)
  and handed over an offer's approval link, which opened on the phone.

## How SAM 3.1 compares, and what's next

**An offline test, not part of the app.** Both models saw the same 1,170
frames of the demo game (eight stretches of live play, 640x360, 10 fps):
SAM 3.1, and E-BARD, a YOLOv8n detector trained on basketball.

| | SAM 3.1 (Meta) | E-BARD (basketball-trained YOLOv8n) |
|---|---|---|
| Frames where it found the ball | 60% | 32% (19% at its stricter setting) |
| When the two disagreed, it was on the ball | about 337 of 362 | 1 |
| Ball near the rim (78 frames) | 77 | 11 |

- An AI reviewer checked the ball calls on the disagreements.
- The app itself only ever calls SAM 3.1. E-BARD ran in a separate setup for this test only.
- E-BARD (https://github.com/GabrieleGiudic/E-BARD), used unmodified under CC BY 4.0.

**Next: ball tracking trained for basketball.** SAM 3.1 still found no ball
on about 40% of these frames. The next step is to fine-tune a Meta vision
model, Muse Glimmer, on a dataset of basketball games. That needs more time
and a bigger GPU than this project had.

**Next: WhatsApp alerts.** The relay can already send a WhatsApp message when
highlights are ready.
- On the night of 3 Nov I set it up with Meta's WhatsApp test number. The
  token, the webhook and my own phone as a recipient were all in place.
- Meta then locked the new WhatsApp Business Account (error 131031) before a
  message went out, so I couldn't launch with it in time.
- The entry ships with Muse and the approval page as the phone flow, plus a
  phone alert app (ntfy) for "highlights ready".
- Once the account is reviewed and unlocked, WhatsApp is a setting away.

**Next: posting to Threads.** Threads would join Instagram and Facebook as a
place to post. I couldn't get its API working in time.

## Demo footage

Navy vs Air Force, 2024 Armed Forces Men's Basketball Championship.
- Video by Justin Fairley, 2D Audiovisual Squadron, on DVIDS
  (https://www.dvidshub.net/video/939914).
- It is a U.S. government work, in the public domain.
- The appearance of U.S. Department of War (DoW) visual information does not
  imply or constitute DoW endorsement.

## How to try it

- **Hosted, with nothing to sign up for:** open
  https://18-190-220-192.sslip.io and sign in as `judge`, with the password
  in the submission's private field. The dashboard then says **Judge view**.
  There you can:
  - upload a short game on **Game day** (a few minutes of a basketball
    broadcast is plenty), or open the sample game already clipped there;
  - watch the stepper run Muse Voice, Muse Spark, SAM 3.1 and the 9:16
    render, each with its time and estimated cost;
  - watch and download the clips on **Clips**, and open **How it follows the
    ball** under a clip to see SAM 3.1's box on the ball and the crop;
  - see the spending on **Usage**, and each step the app took under
    **Activity**.

  The judge login can't change settings or keys, and a game it uploads is
  never offered on the owner's phone, so its Captions stage is skipped.
  Posting and phone approval are shown in the demo video instead, because
  they post to the owner's real Instagram and Facebook accounts.
- **On your computer:** [LOCAL_SETUP.md](LOCAL_SETUP.md). Python 3.11 and
  ffmpeg; a Meta Model API key for the models.
- **Phone approval without any Meta account:** the relay's local mode with
  Meta faked (`cd meta-relay && npm run dev`), walked through in
  [LOCAL_SETUP.md](LOCAL_SETUP.md).
- **On a server:** [RUNNING.md](RUNNING.md) and [AWS_SETUP.md](AWS_SETUP.md).

This project is not made, endorsed or sponsored by Meta.
