"""A game copied into a watched folder again is not clipped again
(watch/watcher.py, Store.clipped_before).

A copy keeps the file's name and size and gets a new time. The watcher knows
a file by its path, size and time, so each copy used to be clipped as a new
video, and Meta was paid again for the same clips. Now a watched file with
the name and size of one a watch has queued before is skipped before it is
read, and said once. An upload on Game day is never held back."""

from __future__ import annotations

import os

import pytest
from conftest import VIDEO_BYTES, fake_probe

from gameday.watch import folder as folder_mod
from gameday.watch.folder import FolderFeed
from gameday.watch.watcher import Watcher

GAME = VIDEO_BYTES + b" the second half, all of it"


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def watched(make_app, tmp_path):
    """The app watching tmp_path/drop, with every ffprobe read noted."""
    clock = Clock()
    folder = tmp_path / "drop"
    folder.mkdir()
    probed = []

    def probe(path):
        probed.append(path.name)
        return fake_probe(path)

    app = make_app(feed=FolderFeed(clock=clock), probe=probe)
    assert app.post("/api/watches", json={"path": str(folder)}).status_code == 200
    return app, folder, clock, probed


def arrive(app, clock, watcher=None) -> int:
    """Two passes a minute apart, so what is in the folder now has settled.
    How many jobs the second one queued."""
    watcher = watcher or app.worker.watcher
    watcher.pass_once()
    clock.now += folder_mod.SETTLE_SECONDS
    return watcher.pass_once()


def copy_in_again(path):
    """The same file copied in once more: the same name and size, a new time."""
    before = path.stat().st_mtime_ns
    path.write_bytes(path.read_bytes())
    later = before + 3600 * 10**9
    os.utime(path, ns=(later, later))
    assert path.stat().st_mtime_ns != before


def upload(app, name, body) -> int:
    r = app.post(f"/api/upload?filename={name}", content=body, headers={"Content-Type": "application/octet-stream"})
    assert r.status_code == 200, r.text
    return r.json()["job_id"]


def skipped(app) -> list[str]:
    return [e["message"] for e in app.get("/api/events").json() if e["message"].startswith("Skipped")]


def said(job_id: int) -> str:
    return (f"Skipped game.mp4: the same file (name and size) was already clipped as job {job_id}. "
            "Upload it on Game day to clip it again.")


def jobs(app) -> dict[int, str]:
    """Every job, with where its video came from."""
    return {j["id"]: j["video"]["source"] for j in app.get("/api/jobs").json()}


def test_a_game_copied_in_again_is_skipped_unread_with_one_line_and_no_job(watched):
    app, folder, clock, probed = watched
    game = folder / "game.mp4"
    game.write_bytes(GAME)
    assert arrive(app, clock) == 1
    [first] = jobs(app)
    copy_in_again(game)
    assert arrive(app, clock) == 0
    assert list(jobs(app)) == [first]
    assert probed == ["game.mp4"]  # once, for the first: the copy was never read
    assert skipped(app) == [said(first)]
    [event] = [e for e in app.get("/api/events").json() if e["message"].startswith("Skipped")]
    assert event["level"] == "warn" and event["kind"] == "watch"
    assert event["job_id"] == first  # the dashboard links it to the clips the game already has


def test_the_skip_is_said_once_not_on_every_pass(watched):
    app, folder, clock, _ = watched
    game = folder / "game.mp4"
    game.write_bytes(GAME)
    arrive(app, clock)
    copy_in_again(game)
    arrive(app, clock)
    for _ in range(6):  # a minute of the watcher looking every ten seconds
        clock.now += 10
        assert app.worker.watcher.pass_once() == 0
    assert len(skipped(app)) == 1
    assert len(jobs(app)) == 1


def test_a_copy_in_another_watch_or_after_a_restart_is_skipped_too(watched, tmp_path):
    """Any watch, any time: the store remembers what was clipped, not the
    watcher's memory, which a restart empties."""
    app, folder, clock, _ = watched
    (folder / "game.mp4").write_bytes(GAME)
    assert arrive(app, clock) == 1
    [first] = jobs(app)
    assert app.worker.run_next_job() == first  # clipped and done
    other = tmp_path / "second drive"
    other.mkdir()
    assert app.post("/api/watches", json={"path": str(other)}).status_code == 200
    (other / "game.mp4").write_bytes(GAME)
    restarted = Watcher(app.store, feed=FolderFeed(clock=clock), probe=fake_probe)
    assert arrive(app, clock, restarted) == 0
    assert list(jobs(app)) == [first]
    assert skipped(app) == [said(first)]


@pytest.mark.parametrize("name, body", [
    ("game.mp4", GAME + b", and overtime"),  # the same name, another size
    ("game 2.mp4", GAME),  # another name, the same size
])
def test_another_size_or_another_name_is_a_new_game(watched, name, body):
    app, folder, clock, _ = watched
    (folder / "game.mp4").write_bytes(GAME)
    assert arrive(app, clock) == 1
    (folder / name).write_bytes(body)
    assert arrive(app, clock) == 1
    assert len(jobs(app)) == 2
    assert skipped(app) == []


def test_uploads_are_never_held_back_and_never_hold_a_watched_file_back(watched):
    """What the skip says to do works: an upload on Game day always makes a
    new job. And an upload is not a watched file, so it never makes one look
    like a copy."""
    app, folder, clock, _ = watched
    upload(app, "game.mp4", GAME)
    game = folder / "game.mp4"
    game.write_bytes(GAME)
    assert arrive(app, clock) == 1  # the upload of the same file held nothing back
    [watched_job] = [job_id for job_id, source in jobs(app).items() if source == "watch"]
    copy_in_again(game)
    assert arrive(app, clock) == 0
    assert skipped(app) == [said(watched_job)]  # the watched run, never the upload
    upload(app, "game.mp4", GAME)  # clipped again, from Game day
    assert sorted(jobs(app).values()) == ["upload", "upload", "watch"]


def test_the_lookup_counts_queued_watched_files_only_and_names_a_done_job_first(db):
    def video(source, *, mtime_ns=1, name="game.mp4", size=2048):
        return db.add_video(name=name, path=f"/{source}/{name}", source=source, size=size, mtime_ns=mtime_ns,
                            watch_id=1 if source == "watch" else None)

    db.add_job(video("upload"), {})
    assert db.clipped_before("game.mp4", 2048) is None  # an upload is not a watched file
    done = db.add_job(video("watch"), {})
    db.update_job(done, state="done")
    cancelled = db.add_job(video("watch", mtime_ns=2), {})  # copied in again, before the guard
    db.cancel_job(cancelled)
    assert db.clipped_before("game.mp4", 2048) == done  # the run that made the clips
    db.update_job(done, state="failed")
    assert db.clipped_before("game.mp4", 2048) == cancelled  # none done: the latest
    assert db.clipped_before("game.mp4", 2049) is None
    assert db.clipped_before("game 2.mp4", 2048) is None
    video("watch", name="never queued.mp4", size=10)  # recorded, but no job was made for it
    assert db.clipped_before("never queued.mp4", 10) is None
