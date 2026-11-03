"""Shared pieces for the platform tests: a finished basketball job with real
clip files, a fake relay, a fake engine, a fake Meta client, and a signed
command. Not a test module itself (no test_ prefix).

The fake relay answers the way meta-relay/worker.js does, for the unit tests;
test_platform_remote_approval_e2e.py runs the real worker under Node instead.
Ported from this week's event branch (tests/remote_approval_fake.py) and
extended for the new app.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import re
import threading
import time
from pathlib import Path

from gameday import media
from gameday.remote_approval import relay as relay_mod
from gameday.remote_approval import settings as ra_settings
from gameday.types import (
    MODEL_SPARK,
    ApiCall,
    ClipResult,
    Follow,
    JobCancelled,
    MetaError,
    PipelineError,
    PipelineResult,
)

SECRET = "device-secret-for-tests-0123456789abcdef"
DEVICE = {"device_id": "dev_0123456789abcdef", "device_secret": SECRET}
TITLE = "Blue vs Gold"

PLAYS = [
    # (category, label, score)
    ("dunks", "Poster dunk", 92),
    ("threes", "Corner three", 80),
    ("blocks", "Block", 75),
    ("dunks", "Alley-oop", 88),
    ("other", "Free throw", 30),
]


def jpeg(width: int = 108, height: int = 192) -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (width, height), (40, 90, 160)).save(out, "JPEG", quality=80)
    return out.getvalue()


def add_video(db, data_dir: Path, title: str = TITLE) -> int:
    """An uploaded video with a file in DATA_DIR/uploads."""
    uploads = Path(data_dir) / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    path = uploads / f"{hashlib.sha1(title.encode()).hexdigest()[:8]}_{title}.mp4"
    path.write_bytes(os.urandom(2048))
    return db.add_video(name=f"{title}.mp4", path=str(path), source="upload", size=2048, duration_s=1800.0)


def add_clips(db, data_dir: Path, job_id: int, video_id: int, plays=PLAYS) -> list[int]:
    """One rendered clip per play in the job's clips folder, each a real file
    (so sizes and mtimes can be pinned), with a real JPEG thumbnail."""
    folder = Path(data_dir) / "media" / "jobs" / str(job_id) / "clips"
    folder.mkdir(parents=True, exist_ok=True)
    ids = []
    for i, (category, label, score) in enumerate(plays, 1):
        path = folder / f"clip_{i:02d}.mp4"
        path.write_bytes(os.urandom(4096 + i))
        thumb = folder / f"clip_{i:02d}.jpg"
        thumb.write_bytes(jpeg())
        ids.append(db.add_clip(job_id, video_id, idx=i, path=str(path.resolve()), start_s=10.0 * i,
                               end_s=10.0 * i + 12, title=f"{label} in the third", category=category, label=label,
                               score=score, thumb=str(thumb.resolve()), evidence=f"What a {label.lower()}!",
                               follow={"source": "ball", "ball_coverage": 0.74, "note": "Followed the ball"}))
    return ids


def finished_job(db, data_dir, *, plays=PLAYS, mode="basketball", title=TITLE, video_id=None, hint="", credit=None):
    """A job that finished, its video, and one rendered clip per play.
    `credit` is the credit line it was queued with; None makes a job from
    before that setting, whose options have none at all."""
    video_id = video_id or add_video(db, data_dir, title)
    options = {"mode": mode, "max_clips": 8, "hint": hint, "title_cards": True}
    if credit is not None:
        options["caption_credit"] = credit
    job_id = db.add_job(video_id, options)
    db.update_job(job_id, state="running", started_at=time.time())
    add_clips(db, data_dir, job_id, video_id, plays)
    db.update_job(job_id, state="done", finished_at=time.time(), clips=len(plays))
    return db.get_job(job_id)


def switch_on(db, *, whatsapp=True, muse=False):
    """WhatsApp (and/or Muse) and remote approval switched on, the app
    registered, every platform connected."""
    ra_settings.save(db, {"whatsapp_enabled": whatsapp, "relay_url": "https://relay.test", "remote_approvals": True})
    internal = {"pin_set": True, "accounts": FakeRelay().accounts}
    if whatsapp:
        internal["pairing"] = {"state": "confirmed", "tail": "4567"}
    if muse:
        internal["muse"] = {"enabled": True, "created_at": 1_800_000_000, "last_used": 0}
    ra_settings.save(db, internal, internal=True)
    db.secrets.save_json(ra_settings.DEVICE_SECRET, DEVICE)


def sign_command(command: dict, secret: str = SECRET) -> dict:
    body = {k: v for k, v in command.items() if k != "sig"}
    sig = hmac.new(secret.encode(), relay_mod.canonical_json(body).encode(), hashlib.sha256).hexdigest()
    return {**body, "sig": sig}


class FakeRelay:
    """Answers like the relay. `fail` maps a method name to a RelayError to
    raise once. `previews` is whether it keeps clips' previews (a KV binding):
    off, as an older relay or one without the binding answers."""

    def __init__(self, previews=False):
        self.calls = []
        self.pending = []
        self.acks = []
        self.notified = []
        self.uploads = []
        self.fail = {}
        self.n = 0
        self.previews = previews
        self.accounts = {"instagram": {"connected": True, "name": "@demo_account"},
                         "facebook": {"connected": True, "name": "Demo Page"}}
        # The relay's word on a Muse key: never the key itself.
        self.muse = {"enabled": False, "created_at": 0, "last_used": 0}
        self.keys = []

    def _maybe_fail(self, name):
        if name in self.fail:
            raise self.fail.pop(name)

    def status(self):
        self.calls.append(("status",))
        self._maybe_fail("status")
        return {"pairing": {"state": "confirmed", "tail": "4567"}, "pin_set": True,
                "accounts": self.accounts, "templates": {"ck_highlights_ready": "APPROVED"}, "muse": self.muse}

    def notify(self, body):
        self.calls.append(("notify", body["kind"]))
        self._maybe_fail("notify")
        self.notified.append(body)
        self.n += 1
        if body["kind"] == "ready":
            return {"approval_id": f"apr_{self.n:024x}", "channel": "template", "message_id": f"wamid.{self.n}",
                    "previews": self.previews}
        return {"channel": "template", "message_id": f"wamid.{self.n}"}

    def upload_preview(self, approval_id, clip_id, data):
        self.calls.append(("upload_preview", approval_id, clip_id))
        self._maybe_fail("upload_preview")
        self.uploads.append((approval_id, clip_id, data))
        return {"ok": True, "clip_id": clip_id, "size": len(data)}

    def commands(self):
        self.calls.append(("commands",))
        self._maybe_fail("commands")
        out, self.pending = self.pending, []
        return out

    def ack(self, command_id, outcome, reason=""):
        self.acks.append((command_id, outcome, reason))
        return {"ok": True}

    def preferences(self, prefs):
        self.calls.append(("preferences", prefs))
        self._maybe_fail("preferences")
        return {"prefs": prefs}

    def cancel(self, approval_id):
        self.calls.append(("cancel", approval_id))
        return {"cancelled": True}

    def muse_token(self):
        self.calls.append(("muse_token",))
        self._maybe_fail("muse_token")
        key = "ckm_" + os.urandom(24).hex()[:43]
        self.keys.append(key)
        self.muse = {"enabled": True, "created_at": 1_800_000_000, "last_used": 0}
        return {"key": key, "muse": self.muse}

    def muse_revoke(self):
        self.calls.append(("muse_revoke",))
        self._maybe_fail("muse_revoke")
        self.muse = {"enabled": False, "created_at": 0, "last_used": 0}
        return {"revoked": True, "muse": self.muse}


class FakeMeta:
    """The MetaAPI protocol without the network. Muse Spark answers the
    captions request for exactly the clips it was given; every call goes on
    the ledger when one is given, as the real client does."""

    def __init__(self, ledger=None, *, fail: Exception | None = None, cost: float = 0.002):
        self.ledger = ledger
        self.fail = fail
        self.cost = cost
        self.calls: list[tuple] = []
        self.bad_key = False

    def _record(self, model, endpoint, purpose, job_id, ok, units):
        if self.ledger is not None:
            self.ledger.record(ApiCall(model=model, endpoint=endpoint, purpose=purpose, ok=ok,
                                       status=200 if ok else 500, ms=5, cost_usd=self.cost, units=units,
                                       job_id=job_id, error="" if ok else "failed"))

    def chat_json(self, messages, schema, *, name, purpose, job_id=None, max_tokens=2048, temperature=0.2):
        self.calls.append(("chat_json", purpose, job_id, messages))
        if self.ledger is not None:
            self.ledger.check_budget(self.cost)
        self._record(MODEL_SPARK, "/chat/completions", purpose, job_id, self.fail is None,
                     {"prompt_tokens": 900, "completion_tokens": 300})
        if self.fail is not None:
            raise self.fail
        prompt = json.loads(messages[-1]["content"])
        return {"summary": "A night of big plays.", "clips": [
            {"clip_id": c["clip_id"], "title": f"Muse: {c.get('play') or c.get('moment')}",
             "instagram_caption": f"{c.get('play') or c.get('moment')} from tonight.",
             "facebook_description": f"Watch the {str(c.get('play') or c.get('moment')).lower()}.",
             "hashtags": ["#hoops", "game day"]} for c in prompt["clips"]]}

    def chat_text(self, messages, *, purpose, job_id=None, max_tokens=600, temperature=0.4):
        self.calls.append(("chat_text", purpose, job_id))
        return "ok"

    def transcribe(self, audio, *, job_id=None):
        self.calls.append(("transcribe", job_id))
        return []

    def segment_video(self, video, noun, *, job_id=None):
        self.calls.append(("segment_video", job_id))
        raise MetaError("not in the fake", model="sam-3.1")

    def test_key(self):
        self.calls.append(("test_key",))
        if self.bad_key:
            raise MetaError("Meta refused the key (401).", status=401)


class FakePipeline:
    """The engine's run(), without ffmpeg or Meta: reports each stage, writes
    one clip (random bytes) and one real JPEG per play into work_dir/clips,
    and returns them. `fail` raises it after the first stage; `hold` blocks in
    the "sam" stage until it is set, checking should_stop, so a test can
    cancel a running job."""

    def __init__(self, plays=PLAYS, *, fail: Exception | None = None, hold: threading.Event | None = None,
                 spark_call: bool = False):
        self.plays = plays
        self.fail = fail
        self.hold = hold
        self.spark_call = spark_call
        self.calls: list[dict] = []
        self.started = threading.Event()

    def __call__(self, job_id, source, work_dir, options, meta, progress, should_stop):
        self.calls.append({"job_id": job_id, "source": Path(source), "work_dir": Path(work_dir),
                           "options": options, "meta": meta})
        assert Path(source).is_file()
        for stage in ("voice", "spark", "sam", "render"):
            progress(stage, 0.5, f"Working on {stage}")
            self.started.set()
            if stage == "voice" and self.fail is not None:
                raise self.fail
            if stage == "spark" and self.spark_call and meta is not None:
                meta.chat_json([{"role": "user", "content": json.dumps({"clips": []})}], {}, name="moments",
                               purpose="moments", job_id=job_id)
            if stage == "sam" and self.hold is not None:
                while not self.hold.wait(0.02):
                    if should_stop():
                        raise JobCancelled()
            if should_stop():
                raise JobCancelled()
            progress(stage, 1.0, f"Done with {stage}")
        clips_dir = Path(work_dir) / "clips"
        clips_dir.mkdir(parents=True, exist_ok=True)
        (Path(work_dir) / "tmp").mkdir(exist_ok=True)
        (Path(work_dir) / "tmp" / "scratch.mp4").write_bytes(b"scratch")
        out = []
        for i, (category, label, score) in enumerate(self.plays, 1):
            path = clips_dir / f"clip_{i:02d}.mp4"
            path.write_bytes(os.urandom(4096 + i))
            thumb = clips_dir / f"clip_{i:02d}.jpg"
            thumb.write_bytes(jpeg())
            debug = clips_dir / f"clip_{i:02d}_debug.jpg"
            debug.write_bytes(jpeg(192, 108))
            out.append(ClipResult(index=i, path=path, thumb=thumb, start_s=10.0 * i, end_s=10.0 * i + 12,
                                  title=f"{label} in the third", category=category, label=label, score=float(score),
                                  debug=debug, evidence=f"What a {label.lower()}!",
                                  follow=Follow("ball", 0.74, 120, "Followed the ball 74% of the time")))
        return PipelineResult(clips=out, duration_s=1800.0, warnings=["Muse Voice was faked in this test."],
                              stats={"seconds": {"voice": 1.0}})


def broken_pipeline(job_id, source, work_dir, options, meta, progress, should_stop):
    progress("voice", 0.1, "Listening")
    raise PipelineError("The video has no audio stream.")


# ---- the real relay under Node --------------------------------------------------------------


ROOT = Path(__file__).resolve().parents[2]
RELAY_DIR = ROOT / "meta-relay"


def node_ok() -> bool:
    """Node 22.5 or later on the PATH: node:sqlite, which serve.mjs needs."""
    from shutil import which
    from subprocess import run

    node = which("node")
    if not node:
        return False
    try:
        version = run([node, "--version"], capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return False
    found = re.match(r"v(\d+)\.(\d+)", version)
    return bool(found) and (int(found[1]), int(found[2])) >= (22, 5)


def ffmpeg_ok() -> bool:
    from shutil import which

    return bool(which(media.FFMPEG) and which(media.FFPROBE))


def make_video(path: Path, *, size: str = "720x1280", seconds: float = 3, rate: int = 30) -> Path:
    """A real H.264 + AAC MP4 made by ffmpeg: test bars and a tone."""
    media.run_ffmpeg(["-f", "lavfi", "-i", f"testsrc2=size={size}:rate={rate}:duration={seconds}",
                      "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-c:v", "libx264",
                      "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
                     timeout=120)
    return path


class Phone:
    """The owner's phone and browser, against the mock relay (meta-relay/test/serve.mjs)."""

    def __init__(self, url):
        self.url = url

    def _post(self, path, body):
        import requests

        return requests.post(self.url + path, json=body, timeout=10).json()

    def state(self):
        import requests

        return requests.get(self.url + "/__mock/state", timeout=10).json()

    def send(self, text):
        return self._post("/__mock/phone", {"text": text})

    def tap(self, payload, context=""):
        return self._post("/__mock/tap", {"payload": payload, "context": context})

    def oauth(self, url):
        return self._post("/__mock/oauth", {"url": url})

    def status(self, wamid, status, code=None):
        return self._post("/__mock/status", {"wamid": wamid, "status": status, "code": code})

    def last_message(self):
        return self.state()["messages"][-1]

    def link_token(self, message):
        """The approval link's token, from a template's URL button or the
        cta_url fallback."""
        if message.get("type") == "template":
            button = next(c for c in message["template"]["components"] if c.get("sub_type") == "url")
            return button["parameters"][0]["text"]
        return re.search(r"/a/([A-Za-z0-9_-]{20,})", json.dumps(message))[1]

    def open(self, token):
        import requests

        r = requests.get(f"{self.url}/a/{token}", timeout=10)
        return r.status_code, r.text

    def submit(self, token, fields):
        import requests

        r = requests.post(f"{self.url}/a/{token}", data=fields, headers={"Origin": self.url}, timeout=10)
        return r.status_code, r.text


def csrf_of(html):
    return re.search(r'name="csrf" value="([0-9a-f]{64})"', html)[1]


class Muse:
    """The owner's Muse agent: its servers calling the relay's MCP server
    with the key from the dashboard, the way a custom connector does."""

    def __init__(self, url, key):
        self.url = url
        self.key = key
        self.n = 0

    def rpc(self, method, params=None):
        import requests

        self.n += 1
        r = requests.post(self.url + "/mcp", json={"jsonrpc": "2.0", "id": self.n, "method": method,
                                                   "params": params or {}},
                          headers={"Authorization": f"Bearer {self.key}",
                                   "Accept": "application/json, text/event-stream"}, timeout=10)
        assert r.status_code == 200, r.text
        return r.json()["result"]

    def tool(self, name, **args):
        result = self.rpc("tools/call", {"name": name, "arguments": args})
        assert result["isError"] is False, result["content"][0]["text"]
        return result["structuredContent"]
