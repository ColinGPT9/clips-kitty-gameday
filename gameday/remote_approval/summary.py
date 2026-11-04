"""What a finished game produced, counted from its clips.

Ported from this week's event branch. The message on the owner's phone says
things like "4 dunks, 3 threes and a block" or "3 highlights". Every number in
it is counted here from the clips the pipeline saved, never written by a
model: a notification that says "3 dunks" when there were two is worse than
no notification. Muse Spark may add a line of colour when the owner turns
that on (captions.py). With a Meta key, Muse Spark also writes each offer's
one-line summary (offer_line.py), and a line giving a number these counts
and the clips' titles don't hold is not used: the counts are the summary
then, and whenever there is no line.

An offer is one of two kinds (kind_of): a basketball game, counted by each
clip's category (gameday.types.CATEGORIES, which the engine sets), or any
other video, whose clips are counted as highlights.

Each clip counts once, in its own category, so the counts add up to the
number of clips.
"""

import hashlib
import json
from pathlib import Path

#: (category, singular, plural), in the order a message names them.
CATEGORIES = (
    ("clutch", "clutch shot", "clutch shots"),
    ("dunks", "dunk", "dunks"),
    ("threes", "three", "threes"),
    ("blocks", "block", "blocks"),
    ("steals", "steal", "steals"),
    ("assists", "assist", "assists"),
    ("fan_reactions", "reaction", "reactions"),
)
#: A moment the crowd and the commentator's voice marked but nothing named:
#: a big moment, not a play of a kind we don't know.
BIG_MOMENT = ("big_moment", "big moment", "big moments")
OTHER = ("other", "other play", "other plays")
HIGHLIGHT = ("highlight", "highlight", "highlights")

#: Every category, in the order a message names them.
_ORDER = (*CATEGORIES, BIG_MOMENT, OTHER, HIGHLIGHT)
_BASKETBALL = {cid for cid, _, _ in (*CATEGORIES, BIG_MOMENT, OTHER)}


def kind_of(options: dict) -> str:
    """"basketball" or "video": the words an offer of this job's clips is
    written in, from the mode the job was queued with."""
    return "basketball" if (options or {}).get("mode", "basketball") == "basketball" else "video"


def category_of(category: str, kind: str = "basketball") -> str:
    """The category one clip counts in, in an offer of this kind."""
    if kind != "basketball":
        return HIGHLIGHT[0]
    category = str(category or "")
    return category if category in _BASKETBALL else OTHER[0]


def word_for(category: str) -> str:
    """A category in the words a message names one clip of it with:
    "dunk", "three", "big moment"."""
    return next((one for cid, one, _ in _ORDER if cid == category), OTHER[1])


def label_of(clip) -> str:
    """What a clip was cut around, in a few words: the engine's label."""
    return str(clip["label"] or "").strip()[:60]


def offerable(clip) -> bool:
    """A 9:16 clip whose file is on disk, the only kind an offer holds."""
    return Path(clip["path"] or "").is_file()


def snapshot(clips, *, limit: int, kind: str = "basketball", published: dict | None = None) -> list[dict]:
    """The clips an offer is made of, best first, each pinned to the exact
    file on disk (size and mtime): what the phone approves is these files.

    `published` ({clip_id: [platform, ...]}) is where a clip is already
    posted. It is kept on the clip's entry, and the clip is not posted there
    again (commands.queue_publishes)."""
    out = []
    for clip in sorted(clips, key=lambda c: (-(c["score"] or 0), c["start_s"])):
        if not offerable(clip):
            continue
        path = Path(clip["path"])
        stat = path.stat()
        entry = {
            "clip_id": int(clip["id"]),
            "start_s": float(clip["start_s"]),
            "end_s": float(clip["end_s"]),
            "duration": round(float(clip["end_s"]) - float(clip["start_s"]), 1),
            "score": int(round(float(clip["score"] or 0))),
            "path": str(path),
            "size": int(stat.st_size),
            "mtime_ns": int(stat.st_mtime_ns),
            "title": str(clip["title"] or clip["label"] or "").strip()[:120],
            "label": label_of(clip),
            "category": category_of(clip["category"], kind),
        }
        done = list((published or {}).get(int(clip["id"])) or [])
        if done:
            entry["published"] = done
        out.append(entry)
        if len(out) >= limit:
            break
    return out


def fingerprint(snap: list[dict]) -> str:
    """One hash for exactly these files. Any re-render, trim or swap changes it."""
    key = [[c["clip_id"], c["start_s"], c["end_s"], c["size"], c["mtime_ns"]] for c in snap]
    return hashlib.sha256(json.dumps(key, separators=(",", ":")).encode()).hexdigest()[:32]


def file_matches(entry: dict) -> bool:
    """Is the file on disk still the one this snapshot entry was made from?"""
    path = Path(entry.get("path") or "")
    try:
        stat = path.stat()
    except OSError:
        return False
    return int(stat.st_size) == int(entry["size"]) and int(stat.st_mtime_ns) == int(entry["mtime_ns"])


def counts(entries) -> dict[str, int]:
    """How many of each category, from snapshot entries."""
    out: dict[str, int] = {}
    for entry in entries:
        cid = entry["category"]
        out[cid] = out.get(cid, 0) + 1
    return out


def counts_text(found: dict[str, int]) -> str:
    """"4 dunks, 3 threes and 1 block": counted, in a fixed order, plain words.

    No newlines and no leading or trailing parameter text, so it can go
    straight into a WhatsApp template's body parameter."""
    parts = []
    for cid, one, many in _ORDER:
        n = found.get(cid, 0)
        if n:
            parts.append(f"{n} {one if n == 1 else many}")
    if not parts:
        return "no highlights"
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def build(job_title: str, all_clips, offered: list[dict], kind: str = "basketball") -> dict:
    """The summary every message and the approval page are written from.
    `all_clips` are the clips the run made; "best 3 of 5" counts the ones an
    offer could hold."""
    found = counts(offered)
    total = sum(1 for clip in all_clips if offerable(clip))
    title = (job_title or "your video").strip()
    return {
        "title": title[:80],
        "kind": kind,
        "total_clips": total,
        "offered": len(offered),
        "counts": found,
        "counts_text": counts_text(found),
        "duration_s": round(sum(c["duration"] for c in offered), 1),
    }


def ready_text(summary: dict) -> str:
    """The offer's counted summary: its summary_text when Muse Spark wrote
    no line for it (offer_line.py), and what the dashboard shows.
    Deterministic: the same clips always give the same words."""
    n = summary["offered"]
    extra = ""
    if summary["total_clips"] > n:
        extra = f" (best {n} of {summary['total_clips']})"
    if n and set(summary.get("counts") or {}) == {HIGHLIGHT[0]}:
        # Nothing labelled: "3 highlights", not "3 clips: 3 highlights".
        return f"{n} {HIGHLIGHT[1] if n == 1 else HIGHLIGHT[2]}{extra}"
    clips = "clip" if n == 1 else "clips"
    return f"{n} {clips}{extra}: {summary['counts_text']}"
