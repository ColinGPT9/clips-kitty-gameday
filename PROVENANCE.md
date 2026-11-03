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

## Written in this repository (from 3 Nov 2026)

Everything else.

## Not here

There is no code here from Clips Kitty (clips-studio) as it was before the event.
`scripts/provenance_check.py` compares this repository with clips-studio at
6245e02 (28 Oct 2026, the point the event branch started from), and CI fails
on any shared block of five lines of code.

## Third-party

- Python packages from PyPI, listed in `requirements.txt`. None is copied into this repository.
  One of them is Meta's `meta-sam-parser`, which reads SAM 3.1's answers.
- ffmpeg, run as a separate program.
