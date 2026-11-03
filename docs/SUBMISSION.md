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
3. When the game is done, the clips are offered on your phone, through Muse,
   WhatsApp, or both.
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
| **The Muse connector** | A custom connector for Muse, Meta's agent app, on the owner's relay: an MCP server with six tools, plus the same tools as HTTPS routes with an OpenAPI description. Muse says what's ready, hands over the approval link, says what got posted, and puts an offer aside or cancels it. No tool approves or posts. Not yet tried in the Muse app itself |
| **Graph API Reels** | Instagram Reels (resumable upload, container status, publish, permalink) and Facebook Page Reels (start, upload, finish, status, permalink), made by the relay only for what was approved |
| **Facebook Login for Business** | Connects the Page and its Instagram account. The tokens stay on the relay |
| **WhatsApp Business Cloud API** (optional) | Messages with the approval link, quick replies, pairing a number with a one-time code, STOP and START |

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

- This web app on a real game: TODO(measured) (game length, clips, time per
  stage, cost per model, ball coverage). See [TEST_LOG.md](TEST_LOG.md).
- Tests: TODO(measured) (Python and relay counts, from CI).
- On the event branch, on 1 Nov 2026, the relay and the phone's approval
  page ran live: one clip was approved on a phone with the PIN and posted
  through Meta's APIs as an Instagram Reel and a Facebook Page Reel.

## How to try it

- **Hosted:** TODO (address and password, sent privately).
- **On your computer:** [LOCAL_SETUP.md](LOCAL_SETUP.md). Python 3.11 and
  ffmpeg; a Meta Model API key for the models.
- **Phone approval without any Meta account:** the relay's local mode with
  Meta faked (`cd meta-relay && npm run dev`), walked through in
  [LOCAL_SETUP.md](LOCAL_SETUP.md).
- **On a server:** [RUNNING.md](RUNNING.md) and [AWS_SETUP.md](AWS_SETUP.md).

This project is not made, endorsed or sponsored by Meta.
