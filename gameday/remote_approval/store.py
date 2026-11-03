"""Reading and writing the remote approval tables (gameday/store.py SCHEMA).

Ported from this week's event branch. Plain storage: the rules live in
states.py and commands.py. Every state change goes through move(), which
checks the state machine inside the same write, so two threads (the API and
the relay loop) can never both move one offer.
"""

import json
from datetime import UTC, datetime, timedelta

from gameday.remote_approval import states


def now_utc() -> datetime:
    return datetime.now(UTC)


def stamp(moment: datetime | None = None) -> str:
    return (moment or now_utc()).astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse(value: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _json(value, default):
    try:
        out = json.loads(value or "")
    except (TypeError, ValueError):
        return default
    return out if isinstance(out, type(default)) else default


# ---- offers ------------------------------------------------------------------


def create_review(db, *, job_id: int, video_id: int, title: str, clips: list[dict],
                  fingerprint: str, summary: dict, expires_at: str) -> int:
    now = stamp()
    cur = db.conn.execute(
        "INSERT INTO remote_reviews (job_id, video_id, title, state, clips, fingerprint, summary, "
        "expires_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (job_id, int(video_id or 0), title, states.READY_FOR_REVIEW, json.dumps(clips), fingerprint,
         json.dumps(summary), expires_at, now, now),
    )
    db.conn.commit()
    return int(cur.lastrowid)


def get_review(db, review_id: int) -> dict | None:
    row = db.conn.execute("SELECT * FROM remote_reviews WHERE id = ?", (int(review_id),)).fetchone()
    return _review(row) if row else None


def review_for_approval(db, approval_id: str) -> dict | None:
    if not approval_id:
        return None
    row = db.conn.execute("SELECT * FROM remote_reviews WHERE approval_id = ?", (approval_id,)).fetchone()
    return _review(row) if row else None


def reviews_for_job(db, job_id: int) -> list[dict]:
    rows = db.conn.execute(
        "SELECT * FROM remote_reviews WHERE job_id = ? ORDER BY id", (int(job_id),)
    ).fetchall()
    return [_review(r) for r in rows]


def reviews_in(db, wanted) -> list[dict]:
    wanted = list(wanted)
    marks = ",".join("?" * len(wanted))
    rows = db.conn.execute(
        f"SELECT * FROM remote_reviews WHERE state IN ({marks}) ORDER BY id", wanted
    ).fetchall()
    return [_review(r) for r in rows]


def recent_reviews(db, limit: int = 20) -> list[dict]:
    rows = db.conn.execute(
        "SELECT * FROM remote_reviews ORDER BY id DESC LIMIT ?", (int(limit),)
    ).fetchall()
    return [_review(r) for r in rows]


def _review(row) -> dict:
    out = dict(row)
    out["clips"] = _json(out.get("clips"), [])
    out["summary"] = _json(out.get("summary"), {})
    out["captions"] = _json(out.get("captions"), {})
    out["decision"] = _json(out.get("decision"), {})
    out["results"] = _json(out.get("results"), {})
    return out


#: Columns move() and update_review() may write besides the state.
_FIELDS = frozenset({"approval_id", "decision", "results", "error", "expires_at", "remind_at",
                     "captions", "summary"})


def move(db, review_id: int, wanted: str, *, actor: str, detail: str = "", **fields) -> dict:
    """Move an offer to `wanted`, checked against the state machine in the
    same write. Raises states.IllegalMove when the offer is somewhere that
    move is not allowed from; nothing is written then."""
    bad = set(fields) - _FIELDS
    if bad:
        raise ValueError(f"unknown review fields: {sorted(bad)}")
    cells = {k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in fields.items()}
    if db.conn.in_transaction:
        db.conn.commit()
    db.conn.execute("BEGIN IMMEDIATE")
    try:
        row = db.conn.execute("SELECT state FROM remote_reviews WHERE id = ?", (int(review_id),)).fetchone()
        if row is None:
            raise KeyError(f"no offer {review_id}")
        states.check(row["state"], wanted)
        sets = ", ".join(["state = ?", "updated_at = ?", *(f"{k} = ?" for k in cells)])
        db.conn.execute(f"UPDATE remote_reviews SET {sets} WHERE id = ?",
                        (wanted, stamp(), *cells.values(), int(review_id)))
        _audit(db, review_id, actor, wanted.lower(), detail)
    except BaseException:
        db.conn.rollback()
        raise
    db.conn.commit()
    return get_review(db, review_id)


def update_review(db, review_id: int, **fields) -> None:
    """Fields that are not a state change: the relay's id, cached captions."""
    bad = set(fields) - _FIELDS
    if bad:
        raise ValueError(f"unknown review fields: {sorted(bad)}")
    if not fields:
        return
    cells = {k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in fields.items()}
    sets = ", ".join(["updated_at = ?", *(f"{k} = ?" for k in cells)])
    db.conn.execute(f"UPDATE remote_reviews SET {sets} WHERE id = ?", (stamp(), *cells.values(), int(review_id)))
    db.conn.commit()


#: A post that is up, or on its way there (an upload sending or processing):
#: sending the clip there again would post it twice.
SENT = ("queued", "sending", "processing", "published")


def clip_posts(db, clip_id: int) -> list:
    """Every clip_publishes row of this clip. Matched on the clip's lasting
    identity (its video and window) as well as its id: a retried job makes
    the same window again under a new clip id, and the posts the first one
    had still count."""
    clip = db.get_clip(int(clip_id))
    if clip is None:
        return db.clip_publishes(int(clip_id))
    return db.conn.execute(
        "SELECT * FROM clip_publishes WHERE clip_id = ? "
        "OR (video_id > 0 AND video_id = ? AND start_s = ? AND end_s = ?)",
        (int(clip_id), int(clip["video_id"] or 0), clip["start_s"], clip["end_s"]),
    ).fetchall()


def published_platforms(db, clip_id: int) -> set[str]:
    """Where this clip is already published (clip_posts): what an offer
    tells the phone about it."""
    return {r["platform"] for r in clip_posts(db, clip_id) if r["state"] == "published"}


def sent_platforms(db, clip_id: int, *, but_not: str = "") -> dict[str, str]:
    """Where this clip is published or on its way (SENT), as {platform:
    state}, "published" when it is both. A clip an earlier offer already
    posted is never sent there a second time. `but_not` leaves out one
    approval's own posts, so its results can tell them from what was there
    before."""
    out: dict[str, str] = {}
    for r in clip_posts(db, clip_id):
        if r["state"] not in SENT or (but_not and r["provider"] == "meta" and r["request_id"] == but_not):
            continue
        if out.get(r["platform"]) != "published":
            out[r["platform"]] = r["state"]
    return out


# ---- the outbox --------------------------------------------------------------


def enqueue(db, event_key: str, kind: str, *, review_id: int = 0, body: dict | None = None) -> bool:
    """Queue one message for the relay. False when this event was queued
    before: the key is the duplicate guard."""
    now = stamp()
    cur = db.conn.execute(
        "INSERT OR IGNORE INTO remote_outbox (event_key, kind, review_id, body, next_try_at, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (event_key, kind, int(review_id), json.dumps(body or {}), now, now, now),
    )
    db.conn.commit()
    return cur.rowcount == 1


def due_outbox(db, now: datetime | None = None, limit: int = 10, *, kind: str = "",
               skip: str = "") -> list[dict]:
    """What is due, oldest first. `kind` keeps only that kind of message and
    `skip` leaves one kind out: previews go up one a pass, on their own."""
    rows = db.conn.execute(
        "SELECT * FROM remote_outbox WHERE status = 'pending' AND next_try_at <= ? "
        "AND (? = '' OR kind = ?) AND kind != ? ORDER BY id LIMIT ?",
        (stamp(now), kind, kind, skip, int(limit)),
    ).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        item["body"] = _json(item.get("body"), {})
        out.append(item)
    return out


def pending_outbox(db, *, skip: str = "") -> int:
    return db.conn.execute(
        "SELECT COUNT(*) FROM remote_outbox WHERE status = 'pending' AND kind != ?", (skip,)
    ).fetchone()[0]


#: Waits between tries of one message: a minute, five, a quarter hour, an hour.
BACKOFF_SECONDS = (60, 300, 900, 3600)
MAX_ATTEMPTS = 8


def outbox_done(db, item_id: int, status: str, error: str = "") -> None:
    db.conn.execute(
        "UPDATE remote_outbox SET status = ?, error = ?, attempts = attempts + 1, updated_at = ? WHERE id = ?",
        (status, error[:300], stamp(), int(item_id)),
    )
    db.conn.commit()


def outbox_retry(db, item: dict, error: str, now: datetime | None = None, *,
                 max_attempts: int = MAX_ATTEMPTS) -> bool:
    """Try again later, or give up after `max_attempts`. True if it will retry."""
    attempts = int(item["attempts"]) + 1
    if attempts >= max_attempts:
        outbox_done(db, item["id"], "failed", error)
        return False
    wait = BACKOFF_SECONDS[min(attempts - 1, len(BACKOFF_SECONDS) - 1)]
    db.conn.execute(
        "UPDATE remote_outbox SET attempts = ?, error = ?, next_try_at = ?, updated_at = ? WHERE id = ?",
        (attempts, error[:300], stamp((now or now_utc()) + timedelta(seconds=wait)), stamp(), int(item["id"])),
    )
    db.conn.commit()
    return True


def drop_pending(db, review_id: int, kind: str, reason: str) -> int:
    """Drop every message of this kind still waiting for one offer: the rest
    of its previews, once the relay says it keeps none. How many it dropped."""
    cur = db.conn.execute(
        "UPDATE remote_outbox SET status = 'dropped', error = ?, updated_at = ? "
        "WHERE review_id = ? AND kind = ? AND status = 'pending'",
        (reason[:300], stamp(), int(review_id), kind),
    )
    db.conn.commit()
    return cur.rowcount


def outbox_for_review(db, review_id: int) -> list[dict]:
    rows = db.conn.execute(
        "SELECT id, event_key, kind, status, attempts, error, updated_at FROM remote_outbox "
        "WHERE review_id = ? ORDER BY id", (int(review_id),)
    ).fetchall()
    return [dict(r) for r in rows]


# ---- commands seen -----------------------------------------------------------


def seen_command(db, command_id: str) -> dict | None:
    row = db.conn.execute("SELECT * FROM remote_commands WHERE command_id = ?", (command_id,)).fetchone()
    return dict(row) if row else None


def record_command(db, command_id: str, *, review_id: int, kind: str, outcome: str, reason: str = "") -> bool:
    cur = db.conn.execute(
        "INSERT OR IGNORE INTO remote_commands (command_id, review_id, kind, outcome, reason, received_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (command_id, int(review_id), kind, outcome, reason[:300], stamp()),
    )
    db.conn.commit()
    return cur.rowcount == 1


# ---- the audit log -----------------------------------------------------------


def _audit(db, review_id: int, actor: str, action: str, detail: str = "") -> None:
    db.conn.execute(
        "INSERT INTO remote_audit (review_id, actor, action, detail, at) VALUES (?, ?, ?, ?, ?)",
        (int(review_id), actor, action, detail[:300], stamp()),
    )


def audit(db, review_id: int, actor: str, action: str, detail: str = "") -> None:
    _audit(db, review_id, actor, action, detail)
    db.conn.commit()


def audit_log(db, review_id: int | None = None, limit: int = 100) -> list[dict]:
    if review_id is None:
        rows = db.conn.execute("SELECT * FROM remote_audit ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
    else:
        rows = db.conn.execute(
            "SELECT * FROM remote_audit WHERE review_id = ? ORDER BY id DESC LIMIT ?",
            (int(review_id), int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]
