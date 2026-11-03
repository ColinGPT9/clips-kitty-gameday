# Clips Kitty Game Day

Clips Kitty Game Day turns a basketball game into vertical highlight clips
using Meta's models. Before anything is posted to Instagram or Facebook, it
asks you on your phone.

## What existed before this event, and what was built during it

- **Before this event (not in this repository):** [Clips Kitty](https://github.com/ColinGPT9/clips-studio),
  a desktop clipping app with its own local vision models. It is not part of
  this repository, and nothing here depends on it.
- **Built during the event:** everything in this repository.
  - The phone relay (`meta-relay/`), the approval flow and the Instagram and
    Facebook publishing were written from 29 Oct to 2 Nov 2026 for this
    hackathon, on an event branch, and brought here.
  - The web app and its Meta-only engine were written here from 3 Nov 2026.

  [PROVENANCE.md](PROVENANCE.md) lists every imported file with its source
  commit and date.

## The Meta models it calls, and nothing else

| Model | What it does here |
|---|---|
| Muse Voice (`muse-voice-transcribe-1.0`) | listens to the commentary |
| Muse Spark (`muse-spark-1.3`) | picks the plays, and writes the titles and captions |
| SAM 3.1 (`sam-3.1`, hosted by Meta) | follows the ball for the 9:16 crop |

ffmpeg cuts and renders the clips. It is a tool, not a model.

## License

AGPL-3.0-or-later. See [LICENSE](LICENSE).
