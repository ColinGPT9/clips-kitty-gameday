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

All of these come from the same event branch at 0e811c3. "First added" is
the commit that created the source file. Every one of them is dated 29 Oct
2026 or later.

These files were ported whole and adapted to the new store and the Meta
client:

| Path here | Source | First added | Notes |
|---|---|---|---|
| `gameday/remote_approval/states.py` | `remote_approval/states.py` | 819f66f (29 Oct 2026) | unchanged |
| `gameday/remote_approval/relay.py` | `remote_approval/relay.py` | 819f66f (29 Oct 2026) | relay protocol unchanged |
| `gameday/remote_approval/{__init__,store,settings,summary,captions,usage,notifier,commands,service,publishing}.py` | same names under `remote_approval/` | 819f66f (29 Oct 2026) | gaming labels and other publishers dropped; Muse Spark through `gameday.meta_api` |
| `gameday/remote_approval/previews.py` | `remote_approval/previews.py` | 0e811c3 (2 Nov 2026) | |
| `gameday/routes/remote.py` | `server/remote_approval_api.py` | 05d0d67 (29 Oct 2026) | same paths and JSON; Basic auth instead of the desktop session |
| `gameday/publish/meta_graph.py` | `publish/meta_graph.py` | 819f66f (29 Oct 2026) | its error types were rewritten fresh in `gameday/publish/errors.py` |
| `gameday/watch/folder.py` | `sources/folder_feed.py` | 7fb3272 (30 Oct 2026) | plus the event's picked-folder lines from `core/paths.py` (7fb3272) |

For these older files, only the lines added during the event were ported:

| Path here | Source file | Event commits | What |
|---|---|---|---|
| `gameday/store.py` (the `remote_*` tables) | `core/state.py` | 819f66f (29 Oct), 0e811c3 (2 Nov) | the approval tables and their methods |
| `gameday/worker.py` | `server/jobs.py`, `server/publisher.py` | 819f66f, 17d63c5, 094057a, 05d0d67 (29–30 Oct), 56affc6 (2 Nov) | offer the clips when a job ends; post approved clips |
| `gameday/watch/watcher.py`, `notifier.why_not_offered` | `server/automation.py` | 7fb3272, f6099c1, 4d7078d (30 Oct) | folder polling, the phone checks, the data-folder check |

The event's tests were ported to the new store:

| Path here | Source | First added |
|---|---|---|
| `tests/platform/test_platform_remote_approval.py` | `tests/test_remote_approval.py` | 0ee8a02 (29 Oct 2026) |
| `tests/platform/test_platform_remote_approval_api.py` | `tests/test_remote_approval_api.py` | 05d0d67 (29 Oct 2026) |
| `tests/platform/test_platform_remote_approval_e2e.py` | `tests/test_remote_approval_e2e.py` | 0ee8a02 (29 Oct 2026) |
| `tests/platform/test_platform_remote_approval_previews.py` | `tests/test_remote_approval_previews.py` | 0e811c3 (2 Nov 2026) |
| `tests/platform/test_platform_meta_graph.py` | `tests/test_meta_graph.py` | 0ee8a02 (29 Oct 2026) |
| `tests/platform/test_platform_folder_feed.py` | `tests/test_folder_feed.py` | 7fb3272 (30 Oct 2026) |
| `tests/platform/platform_fakes.py` | `tests/remote_approval_fake.py` | 0ee8a02 (29 Oct 2026) |

The Muse Voice client (`gameday/meta_api/voice.py`) was written here. Its
request shape follows the event's Muse Voice code, first added in 9d5fb39
(29 Oct 2026).

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
