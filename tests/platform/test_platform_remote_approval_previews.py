"""Previews: a small video of each offered clip, made here and sent to the
relay one a pass, for the approval page to play (remote_approval/previews.py).

Ported from this week's event branch (tests/test_remote_approval_previews.py).
The relay is faked here and so is ffmpeg, except in the last test, which
encodes a real clip and skips without ffmpeg.
"""

import json
import os
import struct
import subprocess
from datetime import timedelta
from fractions import Fraction

import pytest
from platform_fakes import PLAYS, SECRET, FakeRelay, ffmpeg_ok, finished_job, sign_command, switch_on

from gameday import media
from gameday.remote_approval import commands, notifier, previews, service, states, store
from gameday.remote_approval import relay as relay_mod
from gameday.remote_approval.relay import RelayError

#: What the fake make() writes: the start of an MP4, "ftyp" at byte 4.
MP4 = b"\x00\x00\x00\x18ftypisom" + bytes(16)


@pytest.fixture
def on(db, data_dir):
    """WhatsApp and remote approval switched on, the app registered, every
    platform connected."""
    switch_on(db)
    return data_dir


@pytest.fixture
def made(monkeypatch):
    """make() without ffmpeg: the clips it was asked for, in order."""
    asked = []

    def make(entry, out):
        asked.append(entry["clip_id"])
        out.write_bytes(MP4)
        return True

    monkeypatch.setattr(previews, "make", make)
    return asked


def sent(db, data_dir, relay=None, **kw):
    """A finished job, its offer made and sent to a relay that keeps
    previews unless another is given: (review, relay)."""
    relay = relay or FakeRelay(previews=True)
    review_id = notifier.on_job_finished(db, finished_job(db, data_dir, **kw), "done")
    service.flush_outbox(db, relay)
    return store.get_review(db, review_id), relay


def rows(db) -> list[dict]:
    return [dict(r) for r in db.conn.execute("SELECT * FROM remote_outbox WHERE kind = 'preview' ORDER BY id")]


def approve_command(review):
    return sign_command({
        "id": "cmd_" + "a" * 24, "kind": "approve", "approval_id": review["approval_id"], "review_ref": review["id"],
        "fingerprint": review["fingerprint"], "clips": [review["clips"][0]["clip_id"]], "platforms": ["instagram"],
        "decided_at": store.stamp(),
    })


# ---- which offers get previews ------------------------------------------------------


def test_previews_are_queued_only_when_the_relay_says_it_keeps_them(db, on):
    # An older relay, or one with no KV binding: thumbnails only.
    review, relay = sent(db, on, relay=FakeRelay())
    assert relay.notified[0]["previews"] is True  # this app can send them
    assert review["state"] == states.NOTIFICATION_SENT
    assert rows(db) == []


def test_each_clip_is_queued_best_first_under_the_link_it_was_sent_with(db, on):
    review, _ = sent(db, on)
    queued = rows(db)
    assert [json.loads(r["body"]) for r in queued] == [
        {"approval_id": review["approval_id"], "clip_id": c["clip_id"]} for c in review["clips"]]
    assert [r["event_key"] for r in queued] == [f"preview:{review['approval_id']}:{c['clip_id']}"
                                               for c in review["clips"]]
    assert {(r["review_id"], r["status"]) for r in queued} == {(review["id"], "pending")}
    assert [c["score"] for c in review["clips"]] == [92, 88, 80, 75, 30]


def test_an_offer_that_moved_on_while_it_was_sent_queues_no_previews(db, on):
    class CancelledMeanwhile(FakeRelay):
        def notify(self, body):
            store.move(db, body["review_ref"], states.CANCELLED, actor="desktop", detail="cancelled in the app")
            return super().notify(body)

    review, _ = sent(db, on, relay=CancelledMeanwhile(previews=True))
    assert review["state"] == states.CANCELLED
    assert rows(db) == []


# ---- one a pass -------------------------------------------------------------------------


def test_one_preview_goes_up_a_pass_best_clip_first(db, on, made):
    notifier.on_job_finished(db, finished_job(db, on), "done")
    relay = FakeRelay(previews=True)
    state = service.TickState()
    clock = [100.0]
    for n in range(1, 7):
        service.tick(db, state, relay=relay, clock=lambda: clock[0])
        assert len(relay.uploads) == min(n, 5)
        clock[0] += service.FAST_SECONDS
    review = store.recent_reviews(db, 1)[0]
    assert [u[:2] for u in relay.uploads] == [(review["approval_id"], c["clip_id"]) for c in review["clips"]]
    assert made == [c["clip_id"] for c in review["clips"]]
    assert relay.uploads[0][2] == MP4
    assert {r["status"] for r in rows(db)} == {"sent"}
    # The offer, then the phone's decisions, then a preview.
    calls = [c[0] for c in relay.calls]
    assert calls.index("notify") < calls.index("commands") < calls.index("upload_preview")


def test_previews_waiting_never_hurry_the_loop(db, on, made):
    notifier.on_job_finished(db, finished_job(db, on), "done")
    relay = FakeRelay(previews=True)
    state = service.TickState()
    service.tick(db, state, relay=relay, clock=lambda: 100.0)
    assert len(relay.uploads) == 1
    assert len(store.due_outbox(db, kind=previews.KIND)) == 4
    calls = len(relay.calls)
    assert service.tick(db, state, relay=relay, clock=lambda: 101.0) == 0  # not due yet
    assert len(relay.calls) == calls
    service.tick(db, state, relay=relay, clock=lambda: 100.0 + service.FAST_SECONDS)
    assert len(relay.uploads) == 2


def test_an_approval_in_the_same_pass_comes_before_any_encode(db, on, made):
    review, relay = sent(db, on)
    relay.pending.append(approve_command(review))
    assert service.tick(db, service.TickState(), relay=relay, clock=lambda: 100.0) == 1
    assert store.get_review(db, review["id"])["state"] == states.PUBLISH_QUEUED
    assert made == [] and relay.uploads == []
    assert {(r["status"], r["error"]) for r in rows(db)} == {("dropped", "the offer is publish_queued")}


def test_a_pass_that_queued_uploads_encodes_nothing(db, on, made):
    # This thread runs the uploads, so they start before any encode.
    first, relay = sent(db, on, title="Game one")
    second, _ = sent(db, on, relay, title="Game two")
    relay.pending.append(approve_command(first))
    state = service.TickState()
    assert service.tick(db, state, relay=relay, clock=lambda: 100.0) == 1
    assert made == []
    service.tick(db, state, relay=relay, clock=lambda: 100.0 + service.FAST_SECONDS)
    assert made == [second["clips"][0]["clip_id"]]


class _Unreachable(FakeRelay):
    def _down(self, name):
        self.calls.append((name,))
        raise RelayError("network", "Couldn't reach your relay.", retryable=True)

    def status(self):
        self._down("status")

    def commands(self):
        self._down("commands")

    def notify(self, body):
        self._down("notify")

    def upload_preview(self, *a):
        self._down("upload_preview")


def test_previews_of_a_closed_offer_dont_keep_an_unreachable_relay_polled_fast(db, on):
    review, _ = sent(db, on)
    later = store.parse(review["expires_at"]) + timedelta(minutes=1)
    down, state, clock = _Unreachable(), service.TickState(), [1000.0]
    service.tick(db, state, relay=down, clock=lambda: clock[0])
    down.calls.clear()
    for _ in range(300):  # ten minutes of the loop waking every 2 s, after the link expired
        clock[0] += 2
        service.tick(db, state, relay=down, clock=lambda: clock[0], now=later)
    assert store.get_review(db, review["id"])["state"] == states.EXPIRED
    assert not service.busy(db)
    assert sum(1 for c in down.calls if c[0] == "commands") <= 600 // service.SLOW_SECONDS + 1


# ---- what is no longer shown ------------------------------------------------------------


def test_an_approval_drops_the_previews_still_to_go(db, on, made):
    review, relay = sent(db, on)
    assert previews.send_one(db, relay)
    assert commands.handle(db, approve_command(review), secret=SECRET).outcome == "accepted"
    assert not previews.send_one(db, relay)
    assert made == [review["clips"][0]["clip_id"]]
    assert [r["status"] for r in rows(db)] == ["sent", "dropped", "dropped", "dropped", "dropped"]


def test_a_cancelled_offer_sends_no_previews(db, on, made):
    review, relay = sent(db, on)
    out = commands.handle(db, sign_command({"id": "cmd_" + "c" * 24, "kind": "cancel",
                                            "approval_id": review["approval_id"], "review_ref": review["id"]}),
                          secret=SECRET)
    assert out.outcome == "accepted"
    assert not previews.send_one(db, relay)
    assert made == [] and relay.uploads == []
    assert {(r["status"], r["error"]) for r in rows(db)} == {("dropped", "the offer is cancelled")}


def test_an_offer_sent_again_sends_previews_only_under_its_new_link(db, on, made):
    review, relay = sent(db, on)
    old = review["approval_id"]
    commands.handle(db, sign_command({"id": "cmd_" + "d" * 24, "kind": "delivery", "status": "failed",
                                      "approval_id": old, "review_ref": review["id"]}), secret=SECRET)
    assert service.resend(db, review["id"])
    service.flush_outbox(db, relay)
    new = store.get_review(db, review["id"])["approval_id"]
    assert new != old
    assert previews.send_one(db, relay)
    assert relay.uploads[0][:2] == (new, review["clips"][0]["clip_id"])
    stale = [r for r in rows(db) if json.loads(r["body"])["approval_id"] == old]
    assert len(stale) == 5
    assert {(r["status"], r["error"]) for r in stale} == {("dropped", "the offer was sent again under a new link")}


def test_a_clip_changed_since_the_offer_keeps_its_thumbnail(db, on, made):
    review, relay = sent(db, on)
    first, second = review["clips"][0], review["clips"][1]
    with open(first["path"], "ab") as f:
        f.write(b"re-rendered")
    # And a row for a clip the offer doesn't hold.
    store.enqueue(db, f"preview:{review['approval_id']}:999", previews.KIND, review_id=review["id"],
                  body={"approval_id": review["approval_id"], "clip_id": 999})
    assert previews.send_one(db, relay)
    assert made == [second["clip_id"]]
    by_clip = {json.loads(r["body"])["clip_id"]: r for r in rows(db)}
    assert (by_clip[first["clip_id"]]["status"], by_clip[first["clip_id"]]["error"]) == \
        ("dropped", "the clip changed after the offer was made")
    # Dropped in the same pass, before the encode, though it was queued last.
    assert (by_clip[999]["status"], by_clip[999]["error"]) == ("dropped", "the clip is not in the offer")


# ---- when it can't be made or sent -------------------------------------------------------


def test_a_relay_that_keeps_no_previews_drops_the_rest_of_that_offers(db, on, made):
    review, relay = sent(db, on)
    relay.fail["upload_preview"] = RelayError("previews_off", "This relay keeps no previews.", status=409)
    assert not previews.send_one(db, relay)
    assert {(r["status"], r["error"]) for r in rows(db)} == {("dropped", "This relay keeps no previews.")}
    assert not previews.send_one(db, relay)
    assert made == [review["clips"][0]["clip_id"]]


def test_an_upload_the_relay_couldnt_take_is_tried_twice_more_then_given_up(db, on, made):
    sent(db, on, plays=PLAYS[:1])
    later = store.now_utc()
    for attempt in (1, 2, 3):
        relay = FakeRelay()
        relay.fail["upload_preview"] = RelayError("preview_not_kept", "Your relay couldn't keep it.", status=503,
                                                  retryable=True)
        assert not previews.send_one(db, relay, now=later)
        row = rows(db)[0]
        assert (row["status"], row["attempts"]) == ("pending" if attempt < 3 else "failed", attempt)
        if attempt == 1:
            assert not previews.send_one(db, FakeRelay(), now=later)  # it waits before the next try
        later += timedelta(hours=1)
    assert not previews.send_one(db, FakeRelay(), now=later)
    assert len(made) == 3


def test_a_refused_upload_is_not_tried_again(db, on, made):
    _, relay = sent(db, on, plays=PLAYS[:1])
    relay.fail["upload_preview"] = RelayError("closed", "This offer is closed.", status=409)
    assert not previews.send_one(db, relay)
    assert [(r["status"], r["error"]) for r in rows(db)] == [("failed", "This offer is closed.")]


def test_a_preview_that_cant_be_made_is_dropped_and_the_next_clip_goes(db, on, monkeypatch):
    review, relay = sent(db, on)
    first = review["clips"][0]["clip_id"]

    def make(entry, out):
        if entry["clip_id"] == first:
            return False
        out.write_bytes(MP4)
        return True

    monkeypatch.setattr(previews, "make", make)
    assert not previews.send_one(db, relay)
    assert rows(db)[0]["status"] == "dropped" and relay.uploads == []
    assert previews.send_one(db, relay)
    assert relay.uploads[0][1] == review["clips"][1]["clip_id"]


def test_nothing_in_the_preview_step_stops_the_loop(db, on, monkeypatch):
    _, relay = sent(db, on)

    def broken(*a, **k):
        raise OSError("disk gone")

    monkeypatch.setattr(previews, "make", broken)
    assert previews.send_one(db, relay) is False


def test_disconnecting_drops_the_previews_still_to_go(db, on, monkeypatch):
    sent(db, on)
    monkeypatch.setattr(service, "client", lambda db: None)  # no relay to tell
    service.disconnect(db)
    assert {(r["status"], r["error"]) for r in rows(db)} == {("dropped", "disconnected")}


# ---- the upload ------------------------------------------------------------------------------


class _Answer:
    status_code = 200

    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


def test_a_preview_is_signed_over_its_query_and_sent_whole(monkeypatch):
    got = {}

    def transport(method, url, **kw):
        got.update(method=method, url=url, **kw)
        return _Answer({"ok": True, "clip_id": 7, "size": 5})

    monkeypatch.setattr(relay_mod, "transport", transport)
    relay = relay_mod.Relay("https://relay.test", "dev_0123456789abcdef", SECRET)
    assert relay.upload_preview("apr_" + "a" * 24, 7, b"video") == {"ok": True, "clip_id": 7, "size": 5}
    target = "/v1/previews?approval_id=apr_" + "a" * 24 + "&clip_id=7&size=5"
    assert (got["method"], got["url"], got["data"], got["timeout"]) == \
        ("POST", "https://relay.test" + target, b"video", 120)
    headers = got["headers"]
    assert (headers["Content-Type"], headers["Content-Length"]) == ("video/mp4", "5")
    assert headers["X-CK-Signature"] == relay_mod.sign(SECRET, "POST", target, headers["X-CK-Timestamp"],
                                                       headers["X-CK-Nonce"], "UNSIGNED-PAYLOAD")
    with pytest.raises(RelayError) as refused:
        relay_mod.Relay("https://relay.test").upload_preview("apr_" + "a" * 24, 7, b"video")
    assert refused.value.code == "not_registered"


def test_a_redirect_from_the_relay_is_never_followed(monkeypatch):
    sent = []

    class Moved(_Answer):
        status_code = 307

    def transport(method, url, **kw):
        sent.append(kw)
        return Moved({})

    monkeypatch.setattr(relay_mod, "transport", transport)
    with pytest.raises(RelayError) as moved:
        relay_mod.Relay("https://relay.test", "dev_0123456789abcdef", SECRET).disconnect()
    assert moved.value.code == "redirect" and "final https://" in moved.value.message
    assert [kw["allow_redirects"] for kw in sent] == [False]


# ---- the encode ----------------------------------------------------------------------------


@pytest.mark.parametrize("window", [30, 60, 90, 120, 150, 180])
def test_the_cap_fits_every_clip_an_offer_can_hold(monkeypatch, tmp_path, window):
    seen = {}

    def run(cmd, **kw):
        seen["k"] = int(cmd[cmd.index("-maxrate") + 1].rstrip("k"))
        seen["a"] = int(cmd[cmd.index("-b:a") + 1].rstrip("k"))
        (tmp_path / "p.mp4").write_bytes(MP4)

        class Done:
            returncode = 0
        return Done()

    monkeypatch.setattr(previews.subprocess, "run", run)
    assert previews.make({"path": "clip.mp4", "duration": window}, tmp_path / "p.mp4")
    # Video at its cap plus the audio, over the clip and its ~3 s outro, with
    # a tenth spare for the container and x264's overshoot.
    assert (seen["k"] + seen["a"]) * 1000 / 8 * (window + 3) * 1.1 <= previews.MAX_BYTES


def _boxes(data: bytes) -> list[bytes]:
    """An MP4's top-level boxes, in order."""
    out, at = [], 0
    while at + 8 <= len(data):
        size, kind = struct.unpack(">I4s", data[at:at + 8])
        if size == 1:
            size = struct.unpack(">Q", data[at + 8:at + 16])[0]
        elif size == 0:
            size = len(data) - at
        if size < 8:
            break
        out.append(kind)
        at += size
    return out


@pytest.mark.media
@pytest.mark.skipif(not ffmpeg_ok(), reason="needs ffmpeg and ffprobe")
def test_a_preview_is_small_and_starts_playing_at_once(tmp_path):
    clip = tmp_path / "clip.mp4"  # 9:16 at 60 frames a second, with a stereo tone at 96 kHz
    subprocess.run([media.FFMPEG, "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=1080x1920:rate=60:duration=3",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=3:sample_rate=96000", "-c:v", "libx264",
                    "-preset", "ultrafast",
                    "-c:a", "aac", "-ac", "2", "-shortest", str(clip)], check=True, capture_output=True, timeout=120)
    out = tmp_path / "preview.mp4"
    assert previews.make({"clip_id": 1, "path": str(clip), "duration": 3.0}, out)
    data = out.read_bytes()
    assert 0 < len(data) <= previews.MAX_BYTES
    assert data[4:8] == b"ftyp"
    boxes = _boxes(data)
    assert boxes.index(b"moov") < boxes.index(b"mdat")  # +faststart: the index comes first
    probe = json.loads(subprocess.run([media.FFPROBE, "-v", "error", "-show_streams", "-of", "json", str(out)],
                                      check=True, capture_output=True, text=True, timeout=60).stdout)
    video = next(s for s in probe["streams"] if s["codec_type"] == "video")
    audio = next(s for s in probe["streams"] if s["codec_type"] == "audio")
    assert (video["codec_name"], video["profile"], video["width"], video["height"]) == ("h264", "Main", 360, 640)
    assert Fraction(video["avg_frame_rate"]) <= 30
    assert (audio["codec_name"], audio["channels"], audio["sample_rate"]) == ("aac", 1, "48000")

    junk = tmp_path / "junk.mp4"
    junk.write_bytes(os.urandom(4096))
    assert not previews.make({"clip_id": 2, "path": str(junk), "duration": 3.0}, tmp_path / "junk-preview.mp4")
    assert not (tmp_path / "junk-preview.mp4").exists()
