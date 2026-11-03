"""What the Meta services cost this app, counted as they are used.

Ported from this week's event branch. Each count comes from the place that
knows: the Meta model calls from the usage ledger (the api_calls table, which
gameday.meta_api records with each call's cost estimate), and the relay's
answer to each WhatsApp send (template or free-form) and each Graph step,
kept here. Dollar figures are estimates from published prices, labelled as
estimates wherever they are shown.

WhatsApp utility template, North America: about $0.0034 per delivered
message. A free-form reply inside the 24-hour window is free.
"""

import json

from gameday.types import MODEL_SAM, MODEL_SPARK, MODEL_VOICE

KEY = "remote_approval_usage"

WHATSAPP_UTILITY = 0.0034


def load(db) -> dict:
    try:
        got = json.loads(db.get_flag(KEY, "") or "{}")
    except (TypeError, ValueError):
        got = {}
    got = got if isinstance(got, dict) else {}
    got.setdefault("whatsapp", {"templates": 0, "free_form": 0, "usd": 0.0})
    got.setdefault("meta_graph", {"steps": 0, "uploads": 0, "bytes": 0})
    return got


def _save(db, data: dict) -> None:
    db.set_flag(KEY, json.dumps({k: data[k] for k in ("whatsapp", "meta_graph")}))


def record_whatsapp(db, channel: str) -> None:
    data = load(db)
    w = data["whatsapp"]
    if channel == "template":
        w["templates"] += 1
        w["usd"] = round(w["usd"] + WHATSAPP_UTILITY, 6)
    elif channel and channel != "muse":  # an offer kept for Muse sent no message
        w["free_form"] += 1
    _save(db, data)


def record_graph(db, *, steps: int = 0, uploads: int = 0, size: int = 0) -> None:
    data = load(db)
    g = data["meta_graph"]
    g["steps"] += steps
    g["uploads"] += uploads
    g["bytes"] += size
    _save(db, data)


def _units(row) -> dict:
    try:
        units = json.loads(row["units"] or "{}")
    except (TypeError, ValueError):
        return {}
    return units if isinstance(units, dict) else {}


def _sum(rows, *names) -> float:
    total = 0.0
    for row in rows:
        units = _units(row)
        total += float(next((units[n] for n in names if isinstance(units.get(n), (int, float))), 0) or 0)
    return total


def models(db) -> dict:
    """The three Meta models' counts, from the ledger."""
    spark, voice, sam = db.calls(model=MODEL_SPARK), db.calls(model=MODEL_VOICE), db.calls(model=MODEL_SAM)
    return {
        "muse_spark": {
            "requests": len(spark),
            "prompt_tokens": int(_sum(spark, "prompt_tokens", "input_tokens")),
            "cached_tokens": int(_sum(spark, "cached_tokens")),
            "completion_tokens": int(_sum(spark, "completion_tokens", "output_tokens")),
            "usd": round(sum(float(r["cost_usd"]) for r in spark), 6),
        },
        "muse_voice": {
            "requests": len(voice),
            "audio_seconds": round(_sum(voice, "audio_seconds"), 1),
            "usd": round(sum(float(r["cost_usd"]) for r in voice), 6),
        },
        "sam": {
            "requests": len(sam),
            "frames": int(_sum(sam, "frames")),
            "usd": round(sum(float(r["cost_usd"]) for r in sam), 6),
        },
    }


def summary(db) -> dict:
    data = {**models(db), **load(db)}
    total = sum(data[k]["usd"] for k in ("muse_spark", "muse_voice", "sam", "whatsapp"))
    return {**data, "estimated_usd": round(total, 4)}
