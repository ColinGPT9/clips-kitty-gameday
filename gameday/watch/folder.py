"""What has landed in a watched folder.

Ported from this week's event branch (sources/folder_feed.py, and
picked_folder from core/paths.py), without the desktop app's channel feed and
copy-in: here a found file is read where it is, by its own job.

A folder that a team drops games into, or a sync client (Google Drive,
Dropbox, OneDrive) fills. What a folder needs:

  * Only finished files. A sync client still downloading one, or a copy
    still running, changes its size or time from one look to the next. A file
    is reported once both have stayed the same since a look at least
    SETTLE_SECONDS earlier. That is remembered in memory, so a restart costs
    one extra minute, never a half-copied video. A copy paused for longer than
    that keeps both still (Windows sets the full size before it copies a
    byte), so before a file is clipped Windows is also asked whether another
    program still has it open to write to it.
  * Only files on this machine. A Files On-Demand file that is only online
    (OneDrive, Dropbox) is waited for, never read: reading one has the sync
    app download all of it first, which would hold up the watcher.
  * Only the folder itself: no subfolders, and nothing hidden, temporary or
    half downloaded (~$name, *.tmp, *.part, *.crdownload, desktop.ini).
  * Only a local folder: a network path is refused.
  * Read only. Nothing here moves, changes or deletes a file in the folder.
"""

from __future__ import annotations

import functools
import os
import stat
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# How long a file's size and time must stay the same before it counts as
# arrived. A sync client writes a big file in pieces seconds apart; a minute
# with no change is a file that has finished.
SETTLE_SECONDS = 60

#: The video files a folder is looked at for.
VIDEO_SUFFIXES = (".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".ts", ".mts", ".m2ts", ".mpg", ".mpeg",
                  ".wmv", ".flv")

# What a sync client, a browser or another app writes while a file is still
# on its way, or keeps for itself. None of it is anyone's finished video.
_PARTIAL = (".tmp", ".temp", ".part", ".partial", ".crdownload", ".download")
_SYSTEM = ("desktop.ini", "thumbs.db")
# Windows' own marks for a file nobody is meant to see or keep.
_HIDDEN = (getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 0x2) | getattr(stat, "FILE_ATTRIBUTE_SYSTEM", 0x4)
           | getattr(stat, "FILE_ATTRIBUTE_TEMPORARY", 0x100))
# Windows' marks for a file whose video is still only online: a OneDrive or
# Dropbox Files On-Demand file. The two RECALL marks are not in the stat
# module, so they are spelled out.
_ONLINE_ONLY = (getattr(stat, "FILE_ATTRIBUTE_OFFLINE", 0x1000)
                | 0x40000     # FILE_ATTRIBUTE_RECALL_ON_OPEN
                | 0x400000)   # FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS

ONLINE_ONLY = ("The file is only online so far. Make the folder available offline "
               "(in OneDrive, Always keep on this device).")
STILL_COPYING = "The file is still being copied into the folder."


@dataclass(frozen=True)
class Found:
    """One video file in a watched folder, as it was at the last look."""

    path: Path
    size: int
    mtime_ns: int

    @property
    def key(self) -> tuple[str, int, int]:
        """Its identity: changed in size or time, it is a new video."""
        return str(self.path), self.size, self.mtime_ns


def skipped_name(name: str) -> bool:
    """Whether a watched folder passes over a file by its name alone: hidden
    (a leading dot), an app's own working file (~$ and other ~ names), a
    download that hasn't finished, or Windows' own folder files."""
    lowered = name.lower()
    return lowered.startswith((".", "~")) or lowered in _SYSTEM or lowered.endswith(_PARTIAL)


def picked_folder(raw: str) -> Path | None:
    """A folder to watch, or None if `raw` isn't one: an absolute path,
    resolved (so `..` segments are collapsed before anything acts on it), a
    real directory, and a local one, never a `\\\\server\\share` network path,
    typed or reached through a mapped drive."""
    if not raw or "\x00" in raw or raw.startswith(("\\\\", "//")):
        return None
    try:
        if not Path(raw).is_absolute():
            return None
        path = Path(raw).resolve()
        st = path.stat()
    except (OSError, ValueError, RuntimeError):
        return None
    if not stat.S_ISDIR(st.st_mode):
        return None
    if str(path).startswith(("\\\\", "//")):
        return None  # a mapped network drive resolves to its share
    return path


def within(base: Path, candidate: Path) -> bool:
    """Whether `candidate` is `base` or inside it, both resolved."""
    try:
        base, candidate = Path(base).resolve(), Path(candidate).resolve()
    except (OSError, ValueError, RuntimeError):
        return False
    return candidate == base or base in candidate.parents


class FolderFeed:
    """Every watched folder. One for the app, because it remembers what each
    file looked like at the last look, which is how a finished file is told
    from one still arriving. `clock` is the seam tests use."""

    def __init__(self, *, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._lock = threading.Lock()
        # Per folder, each file as last seen: {path: (size, mtime_ns, since)},
        # `since` being when it was first seen at that size and time.
        self._seen: dict[str, dict[str, tuple[int, int, float]]] = {}

    def look(self, folder_text: str) -> tuple[list[Found], list[Found]]:
        """Every video file in the folder now, and those of them that have
        finished arriving, each newest first.

        Raises ValueError when the folder can't be read (removed, or a drive
        that isn't there yet), so the watch says so instead of looking empty."""
        folder = picked_folder(folder_text)
        if folder is None:
            raise ValueError("The folder isn't there or can't be opened.")
        try:
            with os.scandir(folder) as listing:
                entries = list(listing)
        except OSError as e:
            raise ValueError(f"The folder can't be read: {e.strerror or e}") from e
        files: list[tuple[Path, os.stat_result]] = []
        for entry in entries:
            name = entry.name
            if skipped_name(name) or not name.lower().endswith(VIDEO_SUFFIXES):
                continue
            try:
                if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                    continue  # a subfolder, or a link to a file somewhere else
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if not st.st_size:
                continue  # made, nothing written to it yet
            if getattr(st, "st_file_attributes", 0) & _HIDDEN:
                continue
            files.append((folder / name, st))
        now = self._clock()
        with self._lock:
            before = self._seen.get(str(folder), {})
            seen = {}
            for path, st in files:
                state = (st.st_size, st.st_mtime_ns)
                old = before.get(str(path))
                seen[str(path)] = (*state, old[2] if old is not None and old[:2] == state else now)
            self._seen[str(folder)] = seen
        files.sort(key=lambda found: found[1].st_mtime_ns, reverse=True)
        present = [Found(path, int(st.st_size), int(st.st_mtime_ns)) for path, st in files]
        settled = [f for f in present if now - seen[str(f.path)][2] >= SETTLE_SECONDS]
        return present, settled

    def not_ready(self, found: Found) -> str:
        """Why a settled file must wait before it is read, or "" when it can be
        read now: only online so far, or still open in the program copying it."""
        try:
            st = os.stat(found.path)
        except OSError:
            return "The file isn't in the folder any more."
        if getattr(st, "st_file_attributes", 0) & _ONLINE_ONLY:
            # Never read: the sync app would download all of it first, with
            # the watcher waiting on ffprobe. Once the sync app has it here,
            # the next look finds it ready.
            return ONLINE_ONLY
        if _being_written(found.path):
            return STILL_COPYING
        return ""


# CreateFileW's numbers, for _being_written.
_GENERIC_READ = 0x80000000
_FILE_SHARE_READ = 0x1
_FILE_SHARE_DELETE = 0x4
_OPEN_EXISTING = 3
_ERROR_SHARING_VIOLATION = 32


def _being_written(path: Path) -> bool:
    """Whether another program still has the file open to write to it: a
    copy or a download still going, even one paused for longer than
    SETTLE_SECONDS, whose size and time say nothing then. Windows refuses an
    open that won't share writing while one does (a sharing violation), so
    this asks for exactly that and closes the file at once, reading nothing.
    False where Windows can't be asked."""
    if sys.platform != "win32":
        return False
    import ctypes

    kernel32 = _kernel32()
    handle = kernel32.CreateFileW(str(path), _GENERIC_READ, _FILE_SHARE_READ | _FILE_SHARE_DELETE,
                                  None, _OPEN_EXISTING, 0, None)
    if handle is None or handle == ctypes.c_void_p(-1).value:  # INVALID_HANDLE_VALUE
        return ctypes.get_last_error() == _ERROR_SHARING_VIOLATION
    kernel32.CloseHandle(handle)
    return False


@functools.cache
def _kernel32():
    """kernel32 with CreateFileW typed, loaded once. Its own copy, so the
    types set here never change what another part of the app gets from
    ctypes.windll."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                     wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    return kernel32
