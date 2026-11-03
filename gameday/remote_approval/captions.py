"""The words that go with each clip on Instagram and Facebook.

Ported from this week's event branch. Muse Spark writes them when the owner
has that on (the default): ONE structured request per offer, covering every
offered clip and both platforms at once (titles, Instagram captions, Facebook
descriptions, hashtags, and a one-line summary of the game), held to a strict
JSON schema through the MetaAPI protocol (chat_json). The answer is stored on
the offer and reused: a reminder, a retry or a new offer of the same clips
never asks again. Its cost is on the usage ledger with every other Meta call.

The model is told only what the pipeline found (the play, the clip's title,
the team or player Muse Spark named, the commentary that made it pick the
play, and the owner's own "Teams or players") and told not to invent scores,
names or stats. What comes back is checked: a clip id it was not given is
dropped, lengths are capped, hashtags are cleaned.

Without Muse Spark, or if it fails, the text is built from what the clip
already has (its title and label), so publishing never waits on a model.

Only a basketball offer is written as basketball and tagged #basketball; any
other video is written in plain words with nothing added.
"""

import json
import re

from gameday.remote_approval import store
from gameday.types import MODEL_SPARK

SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "clips": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "clip_id": {"type": "integer"},
                "title": {"type": "string"},
                "instagram_caption": {"type": "string"},
                "facebook_description": {"type": "string"},
                "hashtags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["clip_id", "title", "instagram_caption", "facebook_description", "hashtags"],
            "additionalProperties": False,
        }},
    },
    "required": ["summary", "clips"],
    "additionalProperties": False,
}

# What every kind of offer asks for, and how the answer comes back.
_ASK = (
    "For each clip below, write: a short title (max 80 characters); an Instagram Reels caption (1-3 "
    "sentences, no hashtags); a Facebook Reels description (1-3 sentences, no hashtags); and 3 to 6 "
    "hashtags without spaces, which are added to both. Also write `summary`: one sentence (max 140 "
    "characters) describing the set of highlights.\n"
)
_ANSWER = " Answer with JSON matching the schema, with one entry per clip_id given and no others."

INSTRUCTIONS = (
    "You write social posts for a basketball highlights creator. " + _ASK
    + "Rules: use ONLY the facts given for each clip. Never invent a score, a statistic, a player "
    "name, a team, a date or a result that is not given. If no player or team is given, describe "
    "the play itself. No emojis in titles. Keep the tone energetic but plain." + _ANSWER
)

#: The instructions for each kind of offer (summary.kind_of). The same
#: request and the same rule: nothing that was not given.
INSTRUCTIONS_FOR = {
    "basketball": INSTRUCTIONS,
    "video": (
        "You write social posts for a creator's short video clips. " + _ASK
        + "Rules: use ONLY the facts given for each clip. Never invent a name, a place, a date, a "
        "number, a quote or a result that is not given. If no name is given, describe the moment "
        "itself. No emojis in titles. Keep the tone friendly and plain." + _ANSWER
    ),
}

MAX_TOKENS = 4000


class CaptionsError(Exception):
    """Muse Spark gave nothing usable. `message` is for people."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def offer_kind(review: dict) -> str:
    """The offer's kind. An offer without one was basketball."""
    kind = str((review.get("summary") or {}).get("kind") or "basketball")
    return kind if kind in INSTRUCTIONS_FOR else "video"


def facts(review: dict, clip_rows: dict) -> list[dict]:
    """What the model is told about each clip: the pipeline's own findings."""
    found = "play" if offer_kind(review) == "basketball" else "moment"
    out = []
    for entry in review["clips"]:
        row = clip_rows.get(entry["clip_id"])
        item = {
            "clip_id": entry["clip_id"],
            found: entry.get("label") or "highlight",
            "seconds": entry.get("duration"),
            "title_so_far": entry.get("title") or "",
        }
        if row is not None:
            for key, name in (("team", "team"), ("player", "player"), ("evidence", "commentary")):
                if row[key]:
                    item[name] = str(row[key])[:300 if key == "evidence" else 80]
        out.append(item)
    return out


def generate(db, review: dict, clip_rows: dict, *, meta, hint: str = "") -> dict:
    """One Muse Spark request for the whole offer (MetaAPI.chat_json). Returns
    the stored captions; raises MetaError (or BudgetExceeded) when the call
    fails, and CaptionsError when the answer holds nothing usable."""
    cached = reusable(db, review, MODEL_SPARK)
    if cached:
        return cached
    prompt = {
        "video": review.get("title") or "",
        "counts": (review.get("summary") or {}).get("counts_text") or "",
        "clips": facts(review, clip_rows),
    }
    if hint.strip():
        prompt["teams_or_players"] = hint.strip()[:200]
    messages = [
        {"role": "system", "content": INSTRUCTIONS_FOR[offer_kind(review)]},  # as the engine's Spark call
        {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
    ]
    answer = meta.chat_json(messages, SCHEMA, name="clips_kitty_posts", purpose="captions",
                            job_id=review.get("job_id"), max_tokens=MAX_TOKENS, temperature=0.4)
    parsed = _parse(answer, {c["clip_id"] for c in review["clips"]})
    if not parsed["clips"]:
        raise CaptionsError("Muse Spark's answer had no usable captions.")
    stored = {
        **parsed,
        "model": MODEL_SPARK,
        "fingerprint": review["fingerprint"],
        "generated_at": store.stamp(),
    }
    store.update_review(db, review["id"], captions=stored)
    store.audit(db, review["id"], "desktop", "muse_spark_captions", f"{len(parsed['clips'])} clip(s), {MODEL_SPARK}")
    return stored


def reusable(db, review: dict, model: str) -> dict | None:
    """Captions already made for exactly these clips, on this offer or an
    earlier one of the same job, by the same model."""
    if review.get("captions", {}).get("fingerprint") == review["fingerprint"]:
        return review["captions"]
    for other in store.reviews_for_job(db, review["job_id"]):
        got = other.get("captions") or {}
        if got.get("fingerprint") == review["fingerprint"] and got.get("model") == model:
            store.update_review(db, review["id"], captions=got)
            return got
    return None


def _parse(content, wanted: set[int]) -> dict:
    """The answer, already parsed by chat_json or as text, cleaned."""
    if isinstance(content, dict):
        data = content
    else:
        try:
            data = json.loads(content)
        except (TypeError, ValueError):
            match = re.search(r"\{.*\}", content or "", re.S)
            try:
                data = json.loads(match.group(0)) if match else {}
            except ValueError:
                data = {}
    clips = {}
    items = data.get("clips") if isinstance(data, dict) else None
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            cid = int(item.get("clip_id"))
        except (TypeError, ValueError):
            continue
        if cid not in wanted or str(cid) in clips:
            continue
        clips[str(cid)] = {
            "title": _line(item.get("title"), 80),
            "instagram": _text(item.get("instagram_caption"), 1800),
            "facebook": _text(item.get("facebook_description"), 1800),
            "hashtags": hashtags(item.get("hashtags"), limit=6),
        }
    return {"summary": _line(data.get("summary") if isinstance(data, dict) else "", 140), "clips": clips}


def _line(value, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _text(value, limit: int) -> str:
    return re.sub(r"[ \t]+", " ", str(value or "")).strip()[:limit]


def hashtags(values, *, limit: int = 6) -> list[str]:
    out: list[str] = []
    for entry in values or []:
        for part in re.split(r"[#\s]+", str(entry).lower()):
            word = re.sub(r"[^\w]", "", part)
            if word and f"#{word}" not in out:
                out.append(f"#{word}")
    return out[:limit]


_TAG = re.compile(r"#\w+")
_TRAILING_TAGS = re.compile(r"(?:[\s,;]*#\w+)+[\s.]*$")


def with_tags(text: str, tags: list[str], *, limit: int = 8) -> str:
    """`text`, then a blank line and the hashtags, each said once. A run of
    hashtags Muse Spark put at the end of `text` anyway is folded into them,
    and one it used inside a sentence is not repeated after it."""
    text = str(text or "").strip()
    run = _TRAILING_TAGS.search(text)
    if run:
        tags = hashtags([*tags, *_TAG.findall(run.group(0))], limit=limit)
        text = text[:run.start()].strip()
    said = set(hashtags(_TAG.findall(text), limit=100))
    rest = [t for t in tags if t not in said]
    return "\n\n".join(p for p in (text, " ".join(rest)) if p)


# ---- the text each platform gets ------------------------------------------------------


def wording(kind: str) -> tuple[str, list[str]]:
    """The title a clip with none of its own gets, and the hashtags an offer
    of this kind adds. Only a basketball offer says basketball."""
    if kind == "basketball":
        return "Basketball highlight", ["basketball"]
    return "Highlight", []


def texts_for(review: dict, clip_row, entry: dict) -> dict:
    """{"title", "instagram", "facebook", "source"} for one clip: Muse
    Spark's when there is some for these exact clips, otherwise the clip's
    own title and label."""
    made = (review.get("captions") or {})
    mine = (made.get("clips") or {}).get(str(entry["clip_id"])) \
        if made.get("fingerprint") == review["fingerprint"] else None
    kind = offer_kind(review)
    fallback, added = wording(kind)
    if mine:
        tags = hashtags([*mine["hashtags"], *added], limit=8)
        title = mine["title"] or entry.get("title") or entry.get("label") or fallback
        return {
            "title": title,
            "instagram": with_tags(mine["instagram"], tags),
            "facebook": with_tags(mine["facebook"], tags),
            "source": "muse_spark",
        }
    title = (entry.get("title") or entry.get("label") or fallback).strip()
    label = str(entry.get("label") or "").strip()
    description = label if label and label.lower() != title.lower() else ""
    tags = hashtags([*added, *(["highlights"] if kind == "basketball" else [])], limit=8)
    body = "\n\n".join(p for p in (title, description, " ".join(tags)) if p)
    return {
        "title": title[:100],
        "instagram": body,
        "facebook": body,
        "source": "clip",
    }
