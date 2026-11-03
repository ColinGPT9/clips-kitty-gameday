"""The app's SQLite database: one file, DATA_DIR/gameday.db, in WAL mode.

Every thread gets its own connection (Store.conn), so the API's request
threads, the folder watcher, the pipeline and the relay loop never share a
cursor. A change that must happen as one step runs inside BEGIN IMMEDIATE.

Times here are Unix seconds (float), as the HTTP API gives them, except in
the remote_* tables, which keep the ISO stamps the ported approval code
(remote_approval/store.py) writes and the /remote-approval routes return.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from gameday.types import STAGES, ApiCall, BudgetExceeded

SCHEMA = """
CREATE TABLE IF NOT EXISTS app_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Fernet tokens (secrets.py). The plain value is never written anywhere.
CREATE TABLE IF NOT EXISTS secrets (
    name       TEXT PRIMARY KEY,
    token      TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS watches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    path            TEXT NOT NULL UNIQUE,
    enabled         INTEGER NOT NULL DEFAULT 1,
    error           TEXT NOT NULL DEFAULT '',
    last_checked_at REAL,
    created_at      REAL NOT NULL
);

-- A game to clip: an upload (a copy in DATA_DIR/uploads) or a file found in
-- a watched folder, which is only ever read where it is. A watched file is
-- known by its path, size and time: changed, it is a new video.
CREATE TABLE IF NOT EXISTS videos (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    path       TEXT NOT NULL,
    source     TEXT NOT NULL,
    watch_id   INTEGER,
    size       INTEGER NOT NULL DEFAULT 0,
    mtime_ns   INTEGER NOT NULL DEFAULT 0,
    duration_s REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_videos_found ON videos(path, size, mtime_ns) WHERE source = 'watch';

CREATE TABLE IF NOT EXISTS jobs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id         INTEGER NOT NULL,
    state            TEXT NOT NULL DEFAULT 'queued',
    stage            TEXT,
    stage_progress   REAL NOT NULL DEFAULT 0,
    message          TEXT NOT NULL DEFAULT '',
    stages           TEXT NOT NULL DEFAULT '{}',
    options          TEXT NOT NULL DEFAULT '{}',
    clips            INTEGER NOT NULL DEFAULT 0,
    warnings         TEXT NOT NULL DEFAULT '[]',
    error            TEXT NOT NULL DEFAULT '',
    stats            TEXT NOT NULL DEFAULT '{}',
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    retry_of         INTEGER,
    created_at       REAL NOT NULL,
    started_at       REAL,
    finished_at      REAL
);

CREATE TABLE IF NOT EXISTS clips (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     INTEGER NOT NULL,
    video_id   INTEGER NOT NULL,
    idx        INTEGER NOT NULL,
    title      TEXT NOT NULL DEFAULT '',
    category   TEXT NOT NULL DEFAULT 'other',
    label      TEXT NOT NULL DEFAULT '',
    score      REAL NOT NULL DEFAULT 0,
    team       TEXT NOT NULL DEFAULT '',
    player     TEXT NOT NULL DEFAULT '',
    evidence   TEXT NOT NULL DEFAULT '',
    transcript TEXT NOT NULL DEFAULT '',
    start_s    REAL NOT NULL DEFAULT 0,
    end_s      REAL NOT NULL DEFAULT 0,
    path       TEXT NOT NULL,
    thumb      TEXT NOT NULL DEFAULT '',
    debug      TEXT NOT NULL DEFAULT '',
    follow     TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_clips_job ON clips(job_id);

-- One upload of one approved clip. A row left 'running' by a restart becomes
-- 'interrupted' and is never sent again by itself: it may be up already.
CREATE TABLE IF NOT EXISTS publish_jobs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    clip_id    INTEGER NOT NULL,
    video_id   INTEGER NOT NULL DEFAULT 0,
    start_s    REAL NOT NULL DEFAULT 0,
    end_s      REAL NOT NULL DEFAULT 0,
    request    TEXT NOT NULL DEFAULT '{}',
    status     TEXT NOT NULL DEFAULT 'queued',
    error      TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

-- Where each clip went: one row per clip and platform, as the API confirmed it.
CREATE TABLE IF NOT EXISTS clip_publishes (
    clip_id    INTEGER NOT NULL,
    platform   TEXT NOT NULL,
    provider   TEXT NOT NULL DEFAULT 'meta',
    video_id   INTEGER NOT NULL DEFAULT 0,
    start_s    REAL NOT NULL DEFAULT 0,
    end_s      REAL NOT NULL DEFAULT 0,
    state      TEXT NOT NULL DEFAULT 'queued',
    post_id    TEXT NOT NULL DEFAULT '',
    post_url   TEXT NOT NULL DEFAULT '',
    error      TEXT NOT NULL DEFAULT '',
    request_id TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (clip_id, platform)
);

-- Remote approval (remote_approval/): a finished video's clips, offered to the
-- creator on WhatsApp, approved on their phone, and published from this PC.
--
-- One row per offer, not per job: an offer that expired is followed by a new
-- one for the same job, and the old row stays as the record of what was
-- offered. `clips` is a snapshot (ids, times, file size and mtime) and
-- `fingerprint` its hash, so an approval is only ever applied to exactly the
-- files that were offered: a clip re-rendered since is a different file and
-- needs a new approval. PROCESSING and RENDERING are not stored here; they
-- are the jobs row's own state (remote_approval/states.py).
CREATE TABLE IF NOT EXISTS remote_reviews (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      INTEGER NOT NULL,
    video_id    INTEGER NOT NULL DEFAULT 0,
    title       TEXT NOT NULL DEFAULT '',
    state       TEXT NOT NULL,
    clips       TEXT NOT NULL DEFAULT '[]',
    fingerprint TEXT NOT NULL DEFAULT '',
    summary     TEXT NOT NULL DEFAULT '{}',
    captions    TEXT NOT NULL DEFAULT '{}',  -- Muse Spark's text, made once and kept
    approval_id TEXT NOT NULL DEFAULT '',    -- the relay's id for this offer
    decision    TEXT NOT NULL DEFAULT '{}',  -- what the phone approved
    results     TEXT NOT NULL DEFAULT '{}',  -- per clip, per platform, as the APIs confirmed
    error       TEXT NOT NULL DEFAULT '',
    expires_at  TEXT NOT NULL DEFAULT '',
    remind_at   TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_remote_reviews_job ON remote_reviews(job_id);

-- What has to reach the relay, kept until it has. A notification decided while
-- the relay or the internet is down is sent when they come back, once:
-- event_key is unique, so the same job announced twice queues one message.
CREATE TABLE IF NOT EXISTS remote_outbox (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key   TEXT NOT NULL UNIQUE,
    kind        TEXT NOT NULL,               -- ready | failed | result | test | preview
    review_id   INTEGER NOT NULL DEFAULT 0,
    body        TEXT NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending | sent | failed | dropped
    attempts    INTEGER NOT NULL DEFAULT 0,
    error       TEXT NOT NULL DEFAULT '',
    next_try_at TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- Every command the relay handed over, by the relay's id. A command delivered
-- twice (a poll that timed out after the relay answered) is found here and
-- acknowledged again, never applied again.
CREATE TABLE IF NOT EXISTS remote_commands (
    command_id  TEXT PRIMARY KEY,
    review_id   INTEGER NOT NULL DEFAULT 0,
    kind        TEXT NOT NULL,
    outcome     TEXT NOT NULL,               -- accepted | rejected | ignored
    reason      TEXT NOT NULL DEFAULT '',
    received_at TEXT NOT NULL
);

-- Who did what to an offer, in order: the audit log Settings shows. Never
-- holds a token, a phone number or a PIN.
CREATE TABLE IF NOT EXISTS remote_audit (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id INTEGER NOT NULL DEFAULT 0,
    actor     TEXT NOT NULL,                 -- desktop | relay | phone | publisher
    action    TEXT NOT NULL,
    detail    TEXT NOT NULL DEFAULT '',
    at        TEXT NOT NULL
);

-- What the app did, for the dashboard's activity list.
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at      REAL NOT NULL,
    level   TEXT NOT NULL,
    kind    TEXT NOT NULL,
    message TEXT NOT NULL,
    job_id  INTEGER
);

-- Every Meta model call, worked or not: the usage ledger and the budget.
CREATE TABLE IF NOT EXISTS api_calls (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    at       REAL NOT NULL,
    model    TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    purpose  TEXT NOT NULL,
    ok       INTEGER NOT NULL,
    status   INTEGER NOT NULL,
    ms       INTEGER NOT NULL,
    cost_usd REAL NOT NULL,
    units    TEXT NOT NULL DEFAULT '{}',
    job_id   INTEGER,
    error    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_api_calls_job ON api_calls(job_id);
"""

JOB_STATES = ("queued", "running", "done", "failed", "cancelled")
STAGE_STATES = ("waiting", "running", "done", "skipped", "failed")

#: What New jobs are made with (GET/PATCH /api/settings).
DEFAULT_SETTINGS = {"mode": "basketball", "max_clips": 8, "hint": "", "title_cards": True}
SETTINGS_KEY = "job_settings"


def loads(value, default):
    """A JSON column, or `default` when it is empty, broken or another type."""
    try:
        out = json.loads(value) if value else default
    except (TypeError, ValueError):
        return default
    return out if isinstance(out, type(default)) else default


def media_url(data_dir: Path, path: str | Path | None) -> str | None:
    """The /media URL of a file under DATA_DIR/media/jobs/<id>/clips/, or None
    for anything else (no other file is ever served)."""
    if not path:
        return None
    try:
        file = Path(path).resolve()
        jobs = (Path(data_dir) / "media" / "jobs").resolve()
        clips = file.parent
        job_dir = clips.parent
    except (OSError, ValueError):
        return None
    if clips.name != "clips" or job_dir.parent != jobs or not (job_dir.name.isascii() and job_dir.name.isdigit()):
        return None
    if file.suffix.lower() not in (".mp4", ".jpg", ".png"):
        return None
    return f"/media/jobs/{int(job_dir.name)}/clips/{file.name}"


def fresh_stages() -> dict:
    return {s: {"state": "waiting", "seconds": 0.0, "cost_usd": 0.0, "note": ""} for s in STAGES}


def clean_settings(patch: dict, current: dict | None = None) -> dict:
    """The job settings with `patch` merged in. Raises ValueError with a
    sentence for people on anything out of range or unknown."""
    out = dict(current or DEFAULT_SETTINGS)
    for key, value in patch.items():
        if key == "mode":
            if value not in ("basketball", "talk"):
                raise ValueError('mode must be "basketball" or "talk".')
            out["mode"] = value
        elif key == "max_clips":
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 12:
                raise ValueError("max_clips must be a whole number from 1 to 12.")
            out["max_clips"] = value
        elif key == "hint":
            if not isinstance(value, str) or len(value) > 200:
                raise ValueError("hint must be text of at most 200 characters.")
            out["hint"] = value.strip()
        elif key == "title_cards":
            if not isinstance(value, bool):
                raise ValueError("title_cards must be true or false.")
            out["title_cards"] = value
        else:
            raise ValueError(f"There is no setting called {str(key)[:40]!r}.")
    return out


class Store:
    """The database. `data_dir` is where the files it names live."""

    def __init__(self, path: Path, *, data_dir: Path | None = None, budget_usd: float = 25.0):
        self.path = Path(path)
        self.data_dir = Path(data_dir) if data_dir is not None else self.path.parent
        self.budget_usd = float(budget_usd)
        self.secrets = None  # gameday.secrets.Secrets, attached by whoever opens the store
        self._local = threading.local()
        self._all: dict[sqlite3.Connection, threading.Thread] = {}  # each open connection, and its thread
        self._all_lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._all_lock:
            first = sqlite3.connect(self.path, timeout=30)
        first.execute("PRAGMA journal_mode=WAL")
        first.executescript(SCHEMA)
        first.commit()
        first.close()

    # ---- connections --------------------------------------------------------

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout = 30000")
            conn.execute("PRAGMA synchronous = NORMAL")
            self._local.conn = conn
            with self._all_lock:
                # A pool thread (Muse Voice pieces, SAM clips) opens one and ends without
                # closing it: those are closed here, so they can't pile up job after job.
                dead = [c for c, thread in self._all.items() if not thread.is_alive()]
                for c in dead:
                    del self._all[c]
                self._all[conn] = threading.current_thread()
            for c in dead:
                try:
                    c.close()
                except sqlite3.Error:
                    pass
        return conn

    def close(self) -> None:
        """Close this thread's connection (a new one opens on the next use)."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            self._local.conn = None
            with self._all_lock:
                self._all.pop(conn, None)
            conn.close()

    def close_all(self) -> None:
        with self._all_lock:
            conns, self._all = list(self._all), {}
        for conn in conns:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        self._local = threading.local()

    def _write(self, sql: str, params=()) -> sqlite3.Cursor:
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return cur

    # ---- app_state ----------------------------------------------------------

    def get_flag(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_flag(self, key: str, value: str) -> None:
        self._write("INSERT INTO app_state (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))

    def job_settings(self) -> dict:
        saved = loads(self.get_flag(SETTINGS_KEY), {})
        try:
            return clean_settings({k: v for k, v in saved.items() if k in DEFAULT_SETTINGS})
        except ValueError:
            return dict(DEFAULT_SETTINGS)

    def save_job_settings(self, patch: dict) -> dict:
        merged = clean_settings(patch, self.job_settings())
        self.set_flag(SETTINGS_KEY, json.dumps(merged))
        return merged

    # ---- secrets (the Fernet tokens; secrets.py does the crypto) -------------

    def secret_token(self, name: str) -> str | None:
        row = self.conn.execute("SELECT token FROM secrets WHERE name = ?", (name,)).fetchone()
        return row["token"] if row else None

    def put_secret_token(self, name: str, token: str) -> None:
        self._write("INSERT INTO secrets (name, token, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(name) DO UPDATE SET token = excluded.token, updated_at = excluded.updated_at",
                    (name, token, time.time()))

    def delete_secret_token(self, name: str) -> bool:
        return self._write("DELETE FROM secrets WHERE name = ?", (name,)).rowcount > 0

    # ---- watches ------------------------------------------------------------

    def add_watch(self, path: str) -> int:
        return int(self._write("INSERT INTO watches (path, enabled, created_at) VALUES (?, 1, ?)",
                               (path, time.time())).lastrowid)

    def get_watch(self, watch_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM watches WHERE id = ?", (int(watch_id),)).fetchone()

    def watch_by_path(self, path: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM watches WHERE path = ?", (path,)).fetchone()

    def watches(self, *, enabled_only: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM watches" + (" WHERE enabled = 1" if enabled_only else "") + " ORDER BY id"
        return self.conn.execute(sql).fetchall()

    def set_watch(self, watch_id: int, **fields) -> None:
        allowed = {"enabled", "error", "last_checked_at"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"unknown watch fields: {sorted(bad)}")
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        self._write(f"UPDATE watches SET {sets} WHERE id = ?", (*fields.values(), int(watch_id)))

    def delete_watch(self, watch_id: int) -> bool:
        return self._write("DELETE FROM watches WHERE id = ?", (int(watch_id),)).rowcount > 0

    def videos_of_watch(self, watch_id: int) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM videos WHERE watch_id = ?",
                                     (int(watch_id),)).fetchone()[0])

    # ---- videos -------------------------------------------------------------

    def add_video(self, *, name: str, path: str, source: str, watch_id: int | None = None, size: int = 0,
                  mtime_ns: int = 0, duration_s: float = 0.0) -> int:
        if source not in ("upload", "watch"):
            raise ValueError(f"unknown video source {source!r}")
        return int(self._write(
            "INSERT INTO videos (name, path, source, watch_id, size, mtime_ns, duration_s, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (name, path, source, watch_id, int(size), int(mtime_ns), float(duration_s), time.time())).lastrowid)

    def get_video(self, video_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM videos WHERE id = ?", (int(video_id),)).fetchone()

    def found_video(self, path: str, size: int, mtime_ns: int) -> sqlite3.Row | None:
        """The watched file with exactly this path, size and time, if it was found before."""
        return self.conn.execute(
            "SELECT * FROM videos WHERE source = 'watch' AND path = ? AND size = ? AND mtime_ns = ?",
            (path, int(size), int(mtime_ns))).fetchone()

    def set_video_duration(self, video_id: int, duration_s: float) -> None:
        self._write("UPDATE videos SET duration_s = ? WHERE id = ?", (float(duration_s), int(video_id)))

    # ---- jobs ---------------------------------------------------------------

    def add_job(self, video_id: int, options: dict, *, retry_of: int | None = None) -> int:
        return int(self._write(
            "INSERT INTO jobs (video_id, state, stages, options, retry_of, created_at) "
            "VALUES (?, 'queued', ?, ?, ?, ?)",
            (int(video_id), json.dumps(fresh_stages()), json.dumps(options), retry_of, time.time())).lastrowid)

    def get_job(self, job_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM jobs WHERE id = ?", (int(job_id),)).fetchone()

    def jobs(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()

    def update_job(self, job_id: int, **fields) -> None:
        allowed = {"state", "stage", "stage_progress", "message", "stages", "clips", "warnings", "error", "stats",
                   "cancel_requested", "started_at", "finished_at"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"unknown job fields: {sorted(bad)}")
        if not fields:
            return
        cells = {k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in fields.items()}
        sets = ", ".join(f"{k} = ?" for k in cells)
        self._write(f"UPDATE jobs SET {sets} WHERE id = ?", (*cells.values(), int(job_id)))

    def claim_next_job(self) -> sqlite3.Row | None:
        """The oldest queued job, now running. One step, so two workers can never take one job."""
        conn = self.conn
        if conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT id FROM jobs WHERE state = 'queued' ORDER BY id LIMIT 1").fetchone()
            if row is not None:
                conn.execute("UPDATE jobs SET state = 'running', started_at = ?, message = ? WHERE id = ?",
                             (time.time(), "Starting", row["id"]))
        except BaseException:
            conn.rollback()
            raise
        conn.commit()
        return self.get_job(row["id"]) if row is not None else None

    def cancel_job(self, job_id: int) -> str | None:
        """"cancelled" for a queued job (it never starts), "stopping" for a
        running one (the pipeline stops at its next safe point), None for a
        job that has already finished."""
        cur = self._write("UPDATE jobs SET state = 'cancelled', message = 'Cancelled', finished_at = ?, "
                          "cancel_requested = 0 WHERE id = ? AND state = 'queued'", (time.time(), int(job_id)))
        if cur.rowcount:
            self.add_event("info", "job", f"Job {job_id} was cancelled.", job_id=int(job_id))
            return "cancelled"
        cur = self._write("UPDATE jobs SET cancel_requested = 1, message = 'Stopping' "
                          "WHERE id = ? AND state = 'running'", (int(job_id),))
        return "stopping" if cur.rowcount else None

    def queued_count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM jobs WHERE state = 'queued'").fetchone()[0])

    def running_job(self) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM jobs WHERE state = 'running' ORDER BY id LIMIT 1").fetchone()

    def recover_running_jobs(self) -> list[int]:
        """Jobs a restart cut off: failed, with a reason, so they can be retried."""
        rows = self.conn.execute("SELECT id, stages, stage FROM jobs WHERE state = 'running'").fetchall()
        now = time.time()
        for row in rows:
            stages = loads(row["stages"], fresh_stages())
            for info in stages.values():
                if info.get("state") == "running":
                    info["state"] = "failed"
                    info["note"] = "The app restarted"
            self.conn.execute(
                "UPDATE jobs SET state = 'failed', error = ?, message = ?, stages = ?, finished_at = ?, "
                "cancel_requested = 0 WHERE id = ?",
                ("The app restarted during this job", "The app restarted during this job", json.dumps(stages),
                 now, row["id"]))
        self.conn.commit()
        return [int(r["id"]) for r in rows]

    # ---- clips --------------------------------------------------------------

    def add_clip(self, job_id: int, video_id: int, *, idx: int, path: str, start_s: float, end_s: float,
                 title: str = "", category: str = "other", label: str = "", score: float = 0.0, thumb: str = "",
                 debug: str = "", team: str = "", player: str = "", evidence: str = "", transcript: str = "",
                 follow: dict | None = None) -> int:
        return int(self._write(
            "INSERT INTO clips (job_id, video_id, idx, title, category, label, score, team, player, evidence, "
            "transcript, start_s, end_s, path, thumb, debug, follow, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (int(job_id), int(video_id), int(idx), title, category, label, float(score), team, player, evidence,
             transcript, float(start_s), float(end_s), path, thumb, debug, json.dumps(follow) if follow else "",
             time.time())).lastrowid)

    def get_clip(self, clip_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM clips WHERE id = ?", (int(clip_id),)).fetchone()

    def clips_for_job(self, job_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM clips WHERE job_id = ? ORDER BY idx, id", (int(job_id),)).fetchall()

    def recent_clips(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM clips ORDER BY job_id DESC, idx, id LIMIT ?",
                                 (int(limit),)).fetchall()

    # ---- publish jobs and where each clip went ------------------------------

    def add_publish_job(self, clip_id: int, request: str, *, video_id: int = 0, start_s: float = 0.0,
                        end_s: float = 0.0) -> int:
        now = time.time()
        return int(self._write(
            "INSERT INTO publish_jobs (clip_id, video_id, start_s, end_s, request, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (int(clip_id), int(video_id or 0), float(start_s), float(end_s), request, now, now)).lastrowid)

    def claim_next_publish_job(self) -> sqlite3.Row | None:
        conn = self.conn
        if conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT id FROM publish_jobs WHERE status = 'queued' ORDER BY id LIMIT 1").fetchone()
            if row is not None:
                conn.execute("UPDATE publish_jobs SET status = 'running', updated_at = ? WHERE id = ?",
                             (time.time(), row["id"]))
        except BaseException:
            conn.rollback()
            raise
        conn.commit()
        return self.get_publish_job(row["id"]) if row is not None else None

    def finish_publish_job(self, job_id: int, status: str, *, error: str = "") -> None:
        self._write("UPDATE publish_jobs SET status = ?, error = ?, updated_at = ? WHERE id = ?",
                    (status, (error or "")[:500], time.time(), int(job_id)))

    def get_publish_job(self, job_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM publish_jobs WHERE id = ?", (int(job_id),)).fetchone()

    def active_publish_job_for_clip(self, clip_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM publish_jobs WHERE clip_id = ? AND status IN ('queued', 'running') ORDER BY id DESC LIMIT 1",
            (int(clip_id),)).fetchone()

    def publish_busy(self) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM publish_jobs WHERE status IN ('queued', 'running') LIMIT 1").fetchone() is not None

    def recover_running_publish_jobs(self) -> int:
        """Uploads a restart cut off. Never sent again by themselves: the post
        may have gone up, so the creator is asked to check first."""
        cur = self._write(
            "UPDATE publish_jobs SET status = 'interrupted', updated_at = ?, error = ? WHERE status = 'running'",
            (time.time(), "The app restarted while this was uploading. Check the account before trying again."))
        return cur.rowcount

    def record_clip_publish(self, clip_id: int, platform: str, fields: dict) -> None:
        """One platform's outcome for one clip, written as it happens."""
        allowed = {"provider", "video_id", "start_s", "end_s", "state", "post_id", "post_url", "error",
                   "request_id"}
        row = {k: v for k, v in fields.items() if k in allowed}
        now = time.time()
        sent = bool(fields.get("created_at"))
        columns = ["clip_id", "platform", "created_at", "updated_at", *row]
        updates = [f"{k} = excluded.{k}" for k in row] + ["updated_at = excluded.updated_at"]
        if sent:
            updates.append("created_at = excluded.created_at")
        self._write(
            f"INSERT INTO clip_publishes ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))}) "
            f"ON CONFLICT(clip_id, platform) DO UPDATE SET {', '.join(updates)}",
            (int(clip_id), platform, now, now, *row.values()))

    def clip_publishes(self, clip_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM clip_publishes WHERE clip_id = ? ORDER BY platform",
                                 (int(clip_id),)).fetchall()

    # ---- events -------------------------------------------------------------

    def add_event(self, level: str, kind: str, message: str, *, job_id: int | None = None) -> int:
        if level not in ("info", "warn", "error"):
            level = "info"
        return int(self._write("INSERT INTO events (at, level, kind, message, job_id) VALUES (?, ?, ?, ?, ?)",
                               (time.time(), level, kind, str(message)[:500], job_id)).lastrowid)

    def events(self, after: int = 0, limit: int = 100) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM events WHERE id > ? ORDER BY id LIMIT ?",
                                 (int(after), int(limit))).fetchall()

    # ---- the Meta usage ledger ----------------------------------------------

    def record_call(self, call: ApiCall) -> None:
        self._write(
            "INSERT INTO api_calls (at, model, endpoint, purpose, ok, status, ms, cost_usd, units, job_id, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), call.model, call.endpoint, call.purpose, 1 if call.ok else 0, int(call.status),
             int(call.ms), float(call.cost_usd or 0), json.dumps(call.units or {}), call.job_id,
             str(call.error or "")[:300]))

    def spent_usd(self) -> float:
        return float(self.conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM api_calls").fetchone()[0])

    def job_cost(self, job_id: int) -> float:
        return float(self.conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM api_calls WHERE job_id = ?",
                                       (int(job_id),)).fetchone()[0])

    def job_calls(self, job_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM api_calls WHERE job_id = ? ORDER BY id", (int(job_id),)).fetchall()

    def calls(self, *, model: str = "") -> list[sqlite3.Row]:
        if model:
            return self.conn.execute("SELECT * FROM api_calls WHERE model = ? ORDER BY id", (model,)).fetchall()
        return self.conn.execute("SELECT * FROM api_calls ORDER BY id").fetchall()

    def recent_calls(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM api_calls ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()

    def ledger(self) -> Ledger:
        return Ledger(self)


class Ledger:
    """gameday.types.Ledger over the api_calls table, against META_BUDGET_USD."""

    def __init__(self, store: Store):
        self.store = store
        self._lock = threading.Lock()

    def check_budget(self, estimate_usd: float) -> None:
        with self._lock:
            spent = self.store.spent_usd()
            limit = self.store.budget_usd
            if spent + max(0.0, float(estimate_usd or 0)) > limit:
                raise BudgetExceeded(
                    f"Meta calls are paused: about ${spent:.2f} is spent and this call would pass the "
                    f"${limit:.2f} budget (META_BUDGET_USD).")

    def record(self, call: ApiCall) -> None:
        with self._lock:
            self.store.record_call(call)
