"""Phone approval settings: what is switched on, and the relay this app talks to.

Ported from this week's event branch. The settings blob lives in app_state.
The device secret that signs requests to the relay does not: it is kept
encrypted (gameday/secrets.py), and no route ever returns it. The owner's
phone number never reaches this app at all: pairing happens on the relay,
which only tells us its last four digits, for "Is this your number?".

Remote approval is off until switched on, and needs an approval PIN and a
way to reach the owner first: WhatsApp notifications with a confirmed
pairing, or a connected Muse agent (service.can_enable_remote_approval).
The key Muse uses is never kept here either: the relay holds its hash, and
this app hands the key to the dashboard once (routes/remote.py).
"""

import json
import secrets as _random

from gameday.types import MODEL_SPARK

SETTINGS_KEY = "remote_approval"
DEVICE_SECRET = "remote_approval_device"

PLATFORMS = ("instagram", "facebook")
PLATFORM_LABELS = {
    "instagram": "Instagram Reels",
    "facebook": "Facebook Page Reels",
}

DEFAULTS = {
    # WhatsApp notifications through the relay.
    "whatsapp_enabled": False,
    "relay_url": "",
    "notify_on_finish": True,
    "notify_on_fail": True,
    "notify_on_publish": True,
    # Approving publishes from the phone. Off by default, on purpose.
    "remote_approvals": False,
    # Ticked in advance on the phone. Only ever a subset of what is connected.
    "preferred_destinations": [],
    # How long a link in a message stays valid, and how long "Remind me later"
    # waits.
    "approval_hours": 24,
    "remind_minutes": 120,
    # At most this many clips in one offer, best first: each platform caps
    # posts per day (Facebook Reels: 30).
    "max_clips": 10,
    # Only basketball games, which is what this app is for. Talk videos are
    # the owner's to opt in to.
    "basketball_only": True,
    # Muse Spark writes the captions (one request per offer, kept) and,
    # separately, may add a one-line summary to the message.
    "muse_captions": True,
    "muse_summary": False,
    "muse_model": MODEL_SPARK,
    # What the relay last told us, for the dashboard to show without asking it.
    "pairing": {},      # {"state": "none|pending|confirmed", "tail": "1234", "at": "..."}
    "pin_set": False,
    "accounts": {},     # {"instagram": {"connected": true, "name": "@..."}, ...}
    "templates": {},    # {"ck_highlights_ready": "APPROVED", ...}
    "muse": {},         # {"enabled": true, "created_at": 1730000000, "last_used": 0}: never the key
}

#: Keys a settings PATCH from the dashboard may change. The rest are written
#: by the service from what the relay reports.
EDITABLE = frozenset({
    "whatsapp_enabled", "relay_url", "notify_on_finish", "notify_on_fail", "notify_on_publish",
    "remote_approvals", "preferred_destinations", "approval_hours", "remind_minutes", "max_clips",
    "basketball_only", "muse_captions", "muse_summary", "muse_model",
})


def load(db) -> dict:
    try:
        stored = json.loads(db.get_flag(SETTINGS_KEY, "") or "{}")
    except (TypeError, ValueError):
        stored = {}
    out = {**DEFAULTS, **(stored if isinstance(stored, dict) else {})}
    # Only the places an offer can post to, whatever the saved blob names.
    out["preferred_destinations"] = _clean("preferred_destinations", out["preferred_destinations"])
    accounts = out["accounts"] if isinstance(out["accounts"], dict) else {}
    out["accounts"] = {p: a for p, a in accounts.items() if p in PLATFORMS}
    return out


def save(db, patch: dict, *, internal: bool = False) -> dict:
    """Merge a patch in. From the dashboard (internal=False) only EDITABLE
    keys get through, each cleaned; the service passes internal=True for what
    the relay reported."""
    allowed = DEFAULTS.keys() if internal else EDITABLE
    clean = {k: _clean(k, v) for k, v in patch.items() if k in allowed}
    merged = {**load(db), **clean}
    db.set_flag(SETTINGS_KEY, json.dumps(merged))
    return merged


def _clean(key: str, value):
    if key == "relay_url":
        url = str(value or "").strip().rstrip("/")
        if url and not url.startswith("https://") and not _is_local(url):
            raise ValueError("The relay address must start with https://")
        return url
    if key == "preferred_destinations":
        return [p for p in PLATFORMS if p in (value or [])]
    if key == "approval_hours":
        return _bounded(value, 1, 72, 24)
    if key == "remind_minutes":
        return _bounded(value, 15, 24 * 60, 120)
    if key == "max_clips":
        return _bounded(value, 1, 25, 10)
    if key == "muse_model":
        model = str(value or "").strip()
        return model if model.startswith("muse-spark") else DEFAULTS["muse_model"]
    if key in DEFAULTS and isinstance(DEFAULTS[key], bool):
        return bool(value)
    return value


def muse_connected(config: dict) -> bool:
    """Whether the relay last said a Muse key works for this app. Offers are
    then made and kept for Muse even with WhatsApp off."""
    return bool((config.get("muse") or {}).get("enabled"))


def _bounded(value, low: int, high: int, default: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def _is_local(url: str) -> bool:
    """http:// is allowed for a relay on this machine only: the mock relay
    the demo and the tests run (meta-relay/test/serve.mjs)."""
    return url.startswith(("http://127.0.0.1", "http://localhost"))


# ---- this app's identity on the relay -----------------------------------------


def device(db) -> dict:
    """{"device_id", "device_secret"} or {} before the app is registered."""
    got = (db.secrets.load_json(DEVICE_SECRET) if db.secrets is not None else None) or {}
    if got.get("device_id") and got.get("device_secret"):
        return {"device_id": str(got["device_id"]), "device_secret": str(got["device_secret"])}
    return {}


def new_device(db) -> dict:
    """Make this app's id and secret. Registered with the relay once (with the
    relay owner's enrolment key), then used to sign every request."""
    made = {"device_id": "dev_" + _random.token_hex(8), "device_secret": _random.token_urlsafe(32)}
    db.secrets.save_json(DEVICE_SECRET, made)
    return made


def forget_device(db) -> bool:
    return db.secrets.delete(DEVICE_SECRET) if db.secrets is not None else False


def public_view(db) -> dict:
    """What the dashboard shows. No secret, no phone number: the last four
    digits are all this app is ever told."""
    s = load(db)
    return {
        **{k: s[k] for k in DEFAULTS},
        "registered": bool(device(db)),
        "platform_labels": PLATFORM_LABELS,
    }
