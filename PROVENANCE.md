# Provenance

The organizers asked for a fresh repository that holds only work done during
the event. This file says where every part of this one came from.

## Imported from event work (written 29 Oct to 2 Nov 2026)

These files were written during the event on the earlier submission's event
branch, `feat/clip-previews` of ColinGPT9/Clips-Kitty-Meta-Basketball. They
were copied here without code from before the event.

| Path here | Source | Source commit | Notes |
|---|---|---|---|
| `meta-relay/**` | `meta-relay/**` | 0e811c3 (2 Nov 2026) | Byte for byte (commit e605c63 here). First written in 819f66f on 29 Oct 2026. |

### Event code brought in for the web app's phone approval

The lead fills this in at merge, one row per file, from the same event branch.

| Path here | Source | Source commit | Notes |
|---|---|---|---|

## Written in this repository (from 3 Nov 2026)

Everything else.

These docs were written here for the web app, adapted from the event's own
docs on the same event branch. Each source doc was first added on or after
29 Oct 2026, after clips-studio 6245e02.

| Path here | Adapted from (event branch at 0e811c3) | Source first added |
|---|---|---|
| `docs/LOCAL_SETUP.md` | `docs/hackathon/meta/JUDGE_SETUP.md` | 1ad70ed (29 Oct 2026) |
| `docs/RELAY_SETUP.md` | `docs/hackathon/meta/WHATSAPP_SETUP.md`, `META_PUBLISHING.md`, `meta-relay/README.md` | 1ad70ed (29 Oct 2026) |
| `docs/MUSE_CONNECTOR.md` | `docs/hackathon/meta/MUSE_CONNECTOR.md` | 137efea (30 Oct 2026) |
| `docs/SECURITY.md` | `docs/hackathon/meta/SECURITY.md`, `REMOTE_APPROVAL.md` | 1ad70ed (29 Oct 2026) |
| `docs/ARCHITECTURE.md` (the relay's part) | `docs/hackathon/meta/ARCHITECTURE.md` | 1ad70ed (29 Oct 2026) |
| `docs/SUBMISSION.md` (the Meta tools table) | `docs/hackathon/meta/SUBMISSION.md` | 1ad70ed (29 Oct 2026) |

## Not here

There is no code here from Clips Kitty (clips-studio) as it was before the event.
`scripts/provenance_check.py` compares this repository with clips-studio at
6245e02 (28 Oct 2026, the point the event branch started from), and CI fails
on any shared block of five lines of code.

## Third-party

- Python packages from PyPI, listed in `requirements.txt`. None is copied into this repository.
  One of them is Meta's `meta-sam-parser`, which reads SAM 3.1's answers.
- ffmpeg, run as a separate program.
