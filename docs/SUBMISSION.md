# Submission draft: Clips Kitty Game Day

**Tagline:** Give it a basketball game: Meta's models cut vertical
highlights that follow the ball, and nothing is posted until you approve it
on your phone.

**Repository:** https://github.com/ColinGPT9/clips-kitty-gameday (public,
AGPL-3.0-or-later)

**Demo video:** TODO

**Try it:** TODO (the hosted address; the owner password goes to the judges
privately)

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

Highlights travel furthest while people are still talking about the game.
The person who runs a team's socials is usually at the game, not at a desk
with an editor. Cutting, reframing for a phone screen and posting each play
by hand takes too long.

## What it does

1. You give it a game: drop the file on the dashboard, or copy it into a
   watched folder.
2. Muse Voice transcribes the commentary. Muse Spark picks the plays and
   writes their titles and captions. SAM 3.1 finds the ball, and ffmpeg cuts
   9:16 clips whose crop follows it.
3. When the game is done, the clips are offered on your phone. Ask Muse
   what's ready (it doesn't message you first), or look at the dashboard,
   and Muse hands you the approval link.
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
| **Muse Spark** (`muse-spark-1.3`) | Reads the commentary and picks the moments, held to a strict JSON schema: each play's window, category, score and the commentary behind it. Writes each clip's title and caption |
| **Muse Voice** (`muse-voice-transcribe-1.0`) | Transcribes the game's commentary with times, for Muse Spark to read |
| **The Muse connector** | A custom connector for Muse, Meta's agent app, on the owner's relay: an MCP server with six tools, plus the same tools as HTTPS routes with an OpenAPI description. Muse says what's ready, hands over the approval link, says what got posted, and puts an offer aside or cancels it. No tool approves or posts. Ran in the Muse app on 2 Nov 2026, on the event branch's relay ([MUSE_CONNECTOR.md](MUSE_CONNECTOR.md)) |
| **Graph API Reels** | Instagram Reels (resumable upload, container status, publish, permalink) and Facebook Page Reels (start, upload, finish, status, permalink), made by the relay only for what was approved |
| **Facebook Login for Business** | Connects the Page and its Instagram account. The tokens stay on the relay |

Only Meta's models are called at runtime. `tests/test_meta_only.py` fails on
any other AI package, import or model host, and if the app's model list is
anything but these three. ffmpeg cuts and renders; it is a tool, not a model.

## Safety

- Nothing is posted without the approval page, the confirm box and the PIN.
  The PIN is never given to Muse or any assistant.
- The app is behind HTTPS and an owner password. The relay never calls the
  app; Meta's app secret and the account tokens live only on the relay.
- Every Meta call checks a budget first and is logged with its estimated
  cost.

More in [SECURITY.md](SECURITY.md).

## What has run

- **This web app on a real game (3 Nov 2026):** a 22.5-minute game made 8
  vertical clips in 227 s on a desktop PC.
  - Cost: about $0.65 of Meta usage, about 8 cents a clip. Muse Voice
    $0.07, Muse Spark $0.02, SAM 3.1 $0.56.
  - Ball-following: in the 7 clips of live play, SAM 3.1 kept the 9:16
    window on the ball 72-89% of the time. The 8th clip, the celebration
    after the buzzer, followed the players.
  - Details in [TEST_LOG.md](TEST_LOG.md).
- **The HD demo game (3 Nov 2026):** the second half of Navy vs Air Force at
  the 2024 Armed Forces Men's Basketball Championship (45 minutes, 1080p,
  public domain).
  - It made 8 clips in 347 s for about $0.87, about 11 cents a clip.
  - SAM 3.1 kept the window on the ball 43-81% of the time, and on the players the rest.
- **Cost to post:** Instagram and Facebook Reels go through Meta's Graph
  API at no charge. Muse Spark's captions for a one-clip offer cost
  $0.004.
- **Tests:** 415 Python tests (on Ubuntu and Windows) and 105 relay tests
  pass in CI, along with the provenance check, at commit b23ae37.
- On the event branch, on 1 Nov 2026, the relay and the phone's approval
  page ran live: one clip was approved on a phone with the PIN and posted
  through Meta's APIs as an Instagram Reel and a Facebook Page Reel.
- On the same event branch's relay, on 2 Nov 2026, the Muse app connected
  as a custom connector (the remote MCP server, with the key in a header)
  and handed over an offer's approval link, which opened on the phone.

## What's next

**Ball tracking trained for basketball.** SAM 3.1 is a general-purpose
model. It finds the ball well in clear shots, and loses it when the ball is
small, fast or in front of the backboard. The next step is to train a Meta
vision model, Muse Glimmer, on a dataset of basketball games, so the crop
follows the ball through every play.

## Demo footage

Navy vs Air Force, 2024 Armed Forces Men's Basketball Championship.
- Video by Justin Fairley, 2D Audiovisual Squadron, on DVIDS
  (https://www.dvidshub.net/video/939914).
- It is a U.S. government work, in the public domain.
- The appearance of U.S. Department of War (DoW) visual information does not
  imply or constitute DoW endorsement.

## How to try it

- **Hosted:** TODO (address and password, sent privately).
- **On your computer:** [LOCAL_SETUP.md](LOCAL_SETUP.md). Python 3.11 and
  ffmpeg; a Meta Model API key for the models.
- **Phone approval without any Meta account:** the relay's local mode with
  Meta faked (`cd meta-relay && npm run dev`), walked through in
  [LOCAL_SETUP.md](LOCAL_SETUP.md).
- **On a server:** [RUNNING.md](RUNNING.md) and [AWS_SETUP.md](AWS_SETUP.md).

This project is not made, endorsed or sponsored by Meta.
