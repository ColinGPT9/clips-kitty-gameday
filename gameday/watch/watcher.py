"""The folder watcher's pass: every settled new video file gets one job.

A file is found by FolderFeed once its size and time have stayed the same
for a minute. It is then checked once (not only online, not still open for
writing, a video ffprobe can read), recorded as a video of its watch, and
queued with the job settings of that moment. The file is only ever read, in
place, by its own job. One changed later (new size or time) is a new video.

Also the rules for adding a watch (check_folder), shared with the routes.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

from gameday import media
from gameday.watch.folder import FolderFeed, Found, picked_folder, within

log = logging.getLogger("gameday")


def check_folder(raw: str, data_dir: Path) -> Path:
    """The folder to watch, from what the dashboard sent. Raises ValueError
    with a sentence for people when it can't be watched."""
    text = str(raw or "").strip()
    if text.startswith(("\\\\", "//")):
        raise ValueError("Choose a folder on this machine. A network folder can't be watched.")
    folder = picked_folder(text)
    if folder is None:
        raise ValueError("Choose a folder that is there and can be opened, by its full path.")
    try:
        with os.scandir(folder):
            pass
    except OSError:
        raise ValueError("That folder can't be read by the app.") from None
    if within(data_dir, folder):
        # Its own uploads and clips would be found and clipped again, without end.
        raise ValueError("Choose a folder outside the app's own data folder.")
    return folder


class Watcher:
    """One pass over every enabled watch (pass_once), run every ten seconds
    by the worker's watcher thread."""

    def __init__(self, store, *, feed: FolderFeed | None = None, probe: Callable | None = None):
        self.store = store
        self.feed = feed or FolderFeed()
        self._probe = probe
        # Files that could not be used, by identity, with why: said once, not
        # every ten seconds. In memory: a restart looks at each once more.
        self._bad: dict[tuple[str, int, int], str] = {}
        self._waiting: dict[tuple[str, int, int], str] = {}

    def probe(self, path: Path):
        return (self._probe or media.probe)(path)

    def pass_once(self) -> int:
        """Look at every enabled watch once. Returns how many jobs it queued."""
        queued = 0
        for watch in self.store.watches(enabled_only=True):
            queued += self._look(watch)
        return queued

    def _look(self, watch) -> int:
        now = time.time()
        try:
            _present, settled = self.feed.look(watch["path"])
        except ValueError as e:
            message = str(e)
            if watch["error"] != message:  # said once, not on every pass
                self.store.add_event("warn", "watch", f"Couldn't check {watch['path']}: {message}")
            self.store.set_watch(watch["id"], error=message, last_checked_at=now)
            return 0
        queued = 0
        for found in settled:
            if self._take(watch, found):
                queued += 1
        self.store.set_watch(watch["id"], error="", last_checked_at=now)
        return queued

    def _take(self, watch, found: Found) -> bool:
        key = found.key
        if key in self._bad or self.store.found_video(*key) is not None:
            return False
        why = self.feed.not_ready(found)
        if why:
            if self._waiting.get(key) != why:
                self._waiting[key] = why
                self.store.add_event("info", "watch", f"Waiting for {found.path.name}: {why}")
            return False
        self._waiting.pop(key, None)
        try:
            info = self.probe(found.path)
        except media.MediaError as e:
            self._bad[key] = str(e)
            self.store.add_event("warn", "watch", f"Skipped {found.path.name}: {e}")
            return False
        try:
            video_id = self.store.add_video(name=found.path.name, path=str(found.path), source="watch",
                                            watch_id=watch["id"], size=found.size, mtime_ns=found.mtime_ns,
                                            duration_s=info.duration_s)
        except sqlite3.IntegrityError:
            return False  # found by another pass in the meantime
        job_id = self.store.add_job(video_id, self.store.job_settings())
        self.store.add_event("info", "watch", f"Found {found.path.name}: job {job_id} is queued.", job_id=job_id)
        log.info("Watched folder: %s queued as job %s.", found.path.name, job_id)
        return True
