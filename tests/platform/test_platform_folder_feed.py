"""Watching a folder (watch/folder.py), without ffmpeg.

Ported from this week's event branch (tests/test_folder_feed.py). What
matters: a file counts only once it has finished arriving (its size and time
unchanged for a minute), nothing hidden, temporary or half downloaded is ever
taken for a video, only a real local folder outside DATA_DIR can be watched,
and a found file is only read. Tests of the desktop app's copy-in and
/videos/local ids were left out: here a file is read where it is.
"""

import os
from pathlib import Path

import pytest
from platform_fakes import ffmpeg_ok, make_video

from gameday import media
from gameday.watch import folder as folder_feed
from gameday.watch.folder import FolderFeed, picked_folder
from gameday.watch.watcher import Watcher, check_folder

T0 = 1_800_000_000.0
WINDOWS = os.name == "nt"


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def feed(clock):
    return FolderFeed(clock=clock)


def write(folder: Path, name: str, data: bytes = b"video bytes") -> Path:
    path = folder / name
    path.write_bytes(data)
    return path


def names(found) -> list[str]:
    return [f.path.name for f in found]


# ---- only finished files ----------------------------------------------------------


def test_a_file_counts_only_once_it_has_stopped_changing_for_a_minute(tmp_path, clock, feed):
    path = write(tmp_path, "game.mp4", b"first half")
    present, settled = feed.look(str(tmp_path))
    assert names(present) == ["game.mp4"] and settled == []
    clock.now += folder_feed.SETTLE_SECONDS - 1
    assert feed.look(str(tmp_path))[1] == []
    # Still arriving: it grew, so its minute starts again.
    path.write_bytes(b"first half, second half")
    clock.now += 2
    assert feed.look(str(tmp_path))[1] == []
    clock.now += folder_feed.SETTLE_SECONDS - 1
    assert feed.look(str(tmp_path))[1] == []
    clock.now += 1
    assert names(feed.look(str(tmp_path))[1]) == ["game.mp4"]


def test_a_restart_costs_one_more_look_never_a_half_copied_file(tmp_path, clock):
    write(tmp_path, "game.mp4")
    first = FolderFeed(clock=clock)
    first.look(str(tmp_path))
    clock.now += 600
    assert names(first.look(str(tmp_path))[1]) == ["game.mp4"]
    restarted = FolderFeed(clock=clock)  # nothing remembered
    assert restarted.look(str(tmp_path))[1] == []
    clock.now += folder_feed.SETTLE_SECONDS
    assert names(restarted.look(str(tmp_path))[1]) == ["game.mp4"]


def test_the_first_look_lists_every_file_for_the_watch_to_note_as_already_there(tmp_path, clock, feed):
    """The first look lists what is already in the folder, finished or not,
    newest first; none of it counts as arrived until it has settled."""
    older, newer = write(tmp_path, "older.mp4"), write(tmp_path, "newer.mkv")
    os.utime(older, (T0 - 7200, T0 - 7200))
    os.utime(newer, (T0 - 60, T0 - 60))
    present, settled = feed.look(str(tmp_path))
    assert names(present) == ["newer.mkv", "older.mp4"]  # newest first, as every listing is
    assert settled == []


# ---- only video files, only the folder itself --------------------------------------


def test_hidden_temporary_and_half_downloaded_files_and_subfolders_are_passed_over(tmp_path, feed):
    for name in ("final.mp4", "Final Cut.MOV", "~$final.mp4", "~final.mp4", ".final.mp4", "final.mp4.part",
                 "final.mp4.crdownload", "render.tmp", "desktop.ini", "Thumbs.db", "notes.txt"):
        write(tmp_path, name)
    write(tmp_path, "nothing yet.mp4", b"")  # made, not written to yet
    (tmp_path / "Season 2").mkdir()
    write(tmp_path / "Season 2", "inside.mp4")
    (tmp_path / "folder.mp4").mkdir()  # a folder named like a video
    present, _ = feed.look(str(tmp_path))
    assert sorted(names(present)) == ["Final Cut.MOV", "final.mp4"]


@pytest.mark.skipif(not WINDOWS, reason="the hidden mark is Windows' own")
def test_a_file_windows_marks_hidden_is_passed_over(tmp_path, feed):
    import ctypes

    hidden = write(tmp_path, "hidden.mp4")
    write(tmp_path, "shown.mp4")
    assert ctypes.windll.kernel32.SetFileAttributesW(str(hidden), 0x2)  # FILE_ATTRIBUTE_HIDDEN
    assert names(feed.look(str(tmp_path))[0]) == ["shown.mp4"]


def test_skipped_names():
    for name in ("~$Game.mp4", ".DS_Store", "clip.MP4.PART", "x.crdownload", "y.tmp", "desktop.ini"):
        assert folder_feed.skipped_name(name), name
    for name in ("Game 3.mp4", "final.mov", "part 2.mp4"):
        assert not folder_feed.skipped_name(name), name


# ---- only a real folder on this machine -------------------------------------------------


@pytest.mark.parametrize("path", [r"\\server\share\videos", "//server/share/videos"])
def test_a_network_folder_is_refused(path, tmp_path):
    assert picked_folder(path) is None
    with pytest.raises(ValueError, match="network"):
        check_folder(path, tmp_path / "data")


def test_only_a_folder_that_is_there_can_be_watched(tmp_path):
    a_file = write(tmp_path, "game.mp4")
    data = tmp_path / "data"
    for bad in (str(a_file), str(tmp_path / "missing"), "relative/folder", "", "bad\x00name"):
        assert picked_folder(bad) is None, bad
        with pytest.raises(ValueError):
            check_folder(bad, data)
    (tmp_path / "sub").mkdir()
    assert check_folder(str(tmp_path / "sub" / ".."), data) == tmp_path.resolve()


def test_the_apps_own_data_folder_cant_be_watched(tmp_path):
    data = tmp_path / "data"
    (data / "uploads").mkdir(parents=True)
    for inside in (data, data / "uploads"):
        with pytest.raises(ValueError, match="outside the app's own data folder"):
            check_folder(str(inside), data)


def test_a_folder_that_is_gone_is_an_error_not_an_empty_folder(tmp_path, feed):
    drive = tmp_path / "drive"
    drive.mkdir()
    key = str(check_folder(str(drive), tmp_path / "data"))
    drive.rmdir()
    with pytest.raises(ValueError):
        feed.look(key)


# ---- is it ready to read? -------------------------------------------------------------


@pytest.mark.skipif(not WINDOWS, reason="a file open for writing is Windows' own answer")
def test_a_file_still_open_in_the_program_copying_it_in_is_waited_for_unread(tmp_path, clock, feed):
    """A paused copy has its full size and an unchanging time, so it settles.
    The program copying it still has it open to write, and that is what says
    it hasn't arrived."""
    path = write(tmp_path, "game.mp4")
    found = feed.look(str(tmp_path))[0][0]
    with open(path, "ab"):
        assert feed.not_ready(found) == folder_feed.STILL_COPYING
    with open(path, "rb"):  # only reading it, as a player does, is no copy
        assert feed.not_ready(found) == ""


@pytest.mark.skipif(not WINDOWS, reason="the online-only mark is Windows' own")
def test_a_file_only_online_is_waited_for_and_never_read(tmp_path, clock, db):
    """Reading a OneDrive or Dropbox file that is only online has the sync app
    download all of it first, with the watcher waiting. It waits for the sync
    app instead. FILE_ATTRIBUTE_OFFLINE stands in for the marks only a sync
    app can set."""
    import ctypes

    probed = []
    feed = FolderFeed(clock=clock)
    watcher = Watcher(db, feed=feed, probe=lambda p: probed.append(p) or media.MediaInfo(60, 1920, 1080, 30, "h264",
                                                                                         True))
    db.add_watch(str(tmp_path.resolve()))
    path = write(tmp_path, "game.mp4")
    watcher.pass_once()
    clock.now += folder_feed.SETTLE_SECONDS
    assert ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x1000)  # FILE_ATTRIBUTE_OFFLINE
    try:
        assert watcher.pass_once() == 0
        assert feed.not_ready(feed.look(str(tmp_path))[1][0]) == folder_feed.ONLINE_ONLY
        assert probed == []
    finally:
        ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x80)  # FILE_ATTRIBUTE_NORMAL
    # Downloaded since, as the same file: read once, and queued.
    assert watcher.pass_once() == 1
    assert probed == [path.resolve()]


def test_what_ffprobe_says_never_carries_the_files_path(tmp_path, clock, db, monkeypatch):
    """ffprobe names a file it can't read by its full path. The activity list
    shows the reason, and a path names whoever's machine it is on."""
    folder = tmp_path / "someone's drive"
    folder.mkdir()
    db.add_watch(str(folder.resolve()))
    write(folder, "game.mp4")
    watcher = Watcher(db, feed=FolderFeed(clock=clock))

    def unreadable(command, **kwargs):
        raise media.subprocess.CalledProcessError(1, command, "", f"{command[-1]}: Invalid data found")

    monkeypatch.setattr(media.subprocess, "run", unreadable)
    watcher.pass_once()
    clock.now += folder_feed.SETTLE_SECONDS
    watcher.pass_once()
    [event] = [e for e in db.events() if e["message"].startswith("Skipped")]
    assert event["message"] == "Skipped game.mp4: ffprobe couldn't read game.mp4"
    assert str(folder) not in event["message"]


def test_a_file_changed_or_gone_since_it_was_found_is_passed_over(tmp_path, feed):
    path = write(tmp_path, "game.mp4", b"take one")
    found = feed.look(str(tmp_path))[0][0]
    path.write_bytes(b"take two, which is longer")
    changed = feed.look(str(tmp_path))[0][0]
    assert changed.key != found.key  # a new video, by its size and time
    path.unlink()
    assert feed.not_ready(found) == "The file isn't in the folder any more."


# ---- with ffmpeg: the real probe, and the file read where it is ----------------------------


@pytest.mark.media
def test_a_real_file_is_read_and_the_original_stays_as_it_was(tmp_path, clock, db):
    if not ffmpeg_ok():
        pytest.skip("ffmpeg isn't available")
    drive = tmp_path / "drive"
    drive.mkdir()
    original = make_video(drive / "Blue vs Gold final.mp4")
    before = (original.read_bytes(), original.stat().st_mtime_ns)
    db.add_watch(str(drive.resolve()))
    watcher = Watcher(db, feed=FolderFeed(clock=clock))
    watcher.pass_once()
    clock.now += folder_feed.SETTLE_SECONDS
    assert watcher.pass_once() == 1
    [job] = db.jobs()
    video = db.get_video(job["video_id"])
    assert video["path"] == str(original.resolve()) and video["duration_s"] == pytest.approx(3.0, abs=0.2)
    assert (original.read_bytes(), original.stat().st_mtime_ns) == before
    assert [p.name for p in drive.iterdir()] == [original.name]  # nothing moved, added or removed
