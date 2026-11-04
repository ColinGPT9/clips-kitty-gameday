"""Phone alerts through ntfy (gameday/notify.py; docs/API.md, "Phone alerts").

ntfy is faked: each request is recorded and nothing leaves the test. The
worker's steps run by hand, as in test_platform_worker.py, and the alerts'
own thread sends what they queue (Alerts.wait_idle waits for it)."""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import threading
import time
import traceback
from email.header import decode_header, make_header
from pathlib import Path

import pytest
import requests
from conftest import ACTION, VIDEO_BYTES
from fastapi.testclient import TestClient
from platform_fakes import FakeMeta, FakePipeline, FakeRelay, SaysLine, broken_pipeline, sign_command, switch_on

from gameday import notify
from gameday.config import Config
from gameday.publish.errors import PublishError
from gameday.remote_approval import publishing, service, store
from gameday.types import MetaError, PipelineError

PUBLIC_URL = "https://203-0-113-7.sslip.io"
JUDGE_PASSWORD = "judge-password-for-tests"
JUDGE_AUTH = {"Authorization": "Basic " + base64.b64encode(f"judge:{JUDGE_PASSWORD}".encode()).decode()}
CANT_CHANGE = {"error": "The judge login can't change this."}
TOPIC_SHAPE = re.compile(r"clipskitty-[A-Za-z0-9_-]{24}")
PUBLISHED = {"clips": [{"clip_id": 1, "title": "Dunk", "posts": [
    {"platform": "instagram", "state": "published", "url": "https://www.instagram.com/reel/R1/", "error": ""}]}]}


class FakeResponse:
    def __init__(self, status: int, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("not JSON")
        return self._body


class FakeNtfy:
    """ntfy, stood in for. Each request is recorded, with its title decoded
    the way ntfy decodes it. `answer` is (status, JSON) or an exception to
    raise; `hold`, when set, is waited for before answering."""

    def __init__(self):
        self.sent: list[dict] = []
        self.answer = (200, {"id": "m1", "event": "message"})
        self.hold: threading.Event | None = None
        self._lock = threading.Lock()

    def __call__(self, method, url, *, headers, data, timeout, allow_redirects):
        if self.hold is not None:
            assert self.hold.wait(10)
        with self._lock:
            self.sent.append({"method": method, "url": url, "headers": dict(headers), "body": data.decode("utf-8"),
                              "timeout": timeout, "allow_redirects": allow_redirects,
                              "title": str(make_header(decode_header(headers["Title"])))})
        if isinstance(self.answer, Exception):
            raise self.answer
        return FakeResponse(*self.answer)

    def titles(self) -> list[str]:
        return [r["title"] for r in self.sent]


@pytest.fixture
def ntfy(monkeypatch):
    fake = FakeNtfy()
    monkeypatch.setattr(notify, "transport", fake)
    return fake


@pytest.fixture
def alerting(make_app, ntfy):
    """make_app(**env), with PUBLIC_URL set. What each app queued is sent,
    and its alerts' thread ended, before ntfy stops being faked."""
    made = []

    def make(**kw):
        kw.setdefault("PUBLIC_URL", PUBLIC_URL)
        app = make_app(**kw)
        made.append(app)
        return app

    yield make
    if ntfy.hold is not None:
        ntfy.hold.set()
    for app in made:
        app.worker.alerts.stop(timeout=10)


def upload(client, name="game.mp4") -> int:
    r = client.post("/api/upload", params={"filename": name}, content=VIDEO_BYTES + os.urandom(1000),
                    headers={"Content-Type": "application/octet-stream", **ACTION})
    assert r.status_code == 200, r.text
    return r.json()["job_id"]


def channel(app) -> str:
    """Phone alerts on, as the dashboard's "Make my alert channel" does it: the new topic."""
    r = app.post("/api/alerts/ntfy")
    assert r.status_code == 200, r.text
    return r.json()["topic"]


def sent(app, ntfy) -> list[dict]:
    """Every alert sent so far, once the app's queue is empty."""
    assert app.worker.alerts.wait_idle(10)
    return list(ntfy.sent)


def judge_client(app) -> TestClient:
    return TestClient(app.app, base_url="http://127.0.0.1", headers=JUDGE_AUTH)


# ---- one alert: send() ----------------------------------------------------------------


def test_send_is_one_post_to_the_topic_with_ntfys_headers(ntfy):
    notify.send("clipskitty-abc_DEF-123", "Blue vs Gold:\n5 highlights ready", "Poster dunk\nOpen Game day.",
                click=f"{PUBLIC_URL}/#gameday", tags=("basketball",), priority=4, server="https://alerts.example/")
    [request] = ntfy.sent
    assert (request["method"], request["url"]) == ("POST", "https://alerts.example/clipskitty-abc_DEF-123")
    assert request["headers"] == {"Title": "Blue vs Gold: 5 highlights ready", "Tags": "basketball",
                                  "Click": f"{PUBLIC_URL}/#gameday", "Priority": "4",
                                  "Content-Type": "text/plain; charset=utf-8"}
    assert request["body"] == "Poster dunk\nOpen Game day."
    assert request["timeout"] == notify.TIMEOUT_S == 10 and request["allow_redirects"] is False
    # Without the extras: the title alone, to ntfy.sh.
    notify.send("clipskitty-abc", "Hello", "There")
    assert ntfy.sent[1]["url"] == "https://ntfy.sh/clipskitty-abc"
    assert set(ntfy.sent[1]["headers"]) == {"Title", "Content-Type"}


def test_a_title_that_isnt_ascii_is_sent_the_way_ntfy_reads_it(ntfy):
    title = "Équipe bleue vs 東京 – 3 highlights ready 🏀"
    notify.send("clipskitty-abc", title, "Ça marche ✓", tags=("basketball",))
    [request] = ntfy.sent
    header = request["headers"]["Title"]
    header.encode("ascii")  # a header carries Latin-1 at most: this one is plain ASCII
    assert header.startswith("=?UTF-8?B?") and all(len(word) <= 75 for word in header.split(" "))
    assert request["title"] == title  # the RFC 2047 words, decoded as ntfy decodes them
    assert request["body"] == "Ça marche ✓"  # the body goes as UTF-8, as it is


TOPIC = "clipskitty-" + "s3cr3t" * 4


@pytest.mark.parametrize("answer, reason", [
    (requests.ConnectionError(f"Max retries exceeded with url: /{TOPIC}"), "couldn't reach ntfy."),
    (requests.Timeout(f"Read timed out: /{TOPIC}"), "ntfy took too long to answer."),
    ((429, {"code": 42901, "http": 429, "error": "limit reached: too many requests"}),
     "ntfy answered 429: limit reached: too many requests."),
    ((403, {"error": f"forbidden for {TOPIC}"}), "ntfy answered 403: forbidden for the alert channel."),
    ((502, None), "ntfy answered 502."),
    ((301, None), "ntfy answered with a redirect. Check NTFY_SERVER."),
], ids=["unreachable", "timeout", "429", "403", "502", "redirect"])
def test_why_an_alert_wasnt_sent_never_names_the_topic(ntfy, answer, reason):
    ntfy.answer = answer
    with pytest.raises(notify.AlertError) as caught:
        notify.send(TOPIC, "Title", "Message")
    assert caught.value.reason == reason == str(caught.value)
    assert TOPIC not in "".join(traceback.format_exception(caught.value))


def test_plain_words_for_a_phone():
    assert notify.plain("Out of memory at C:\\Users\\someone\\clips\\x.wav") == "Out of memory at x.wav"
    assert notify.plain("Error opening /var/lib/gameday/media/jobs/3/tmp/clip_01.mp4: no such file") == \
        "Error opening clip_01.mp4: no such file"
    kept = "See https://www.instagram.com/reel/R1/ and Instagram/Facebook, 1/2 of them (Meta error 4)."
    assert notify.plain(kept) == kept
    assert notify.plain("one\n  two") == "one two" and notify.plain("x" * 400) == "x" * 299 + "…"


# ---- the alert channel: the routes ---------------------------------------------------------


def test_a_new_channel_is_shown_once_and_kept_encrypted(alerting):
    app = alerting()
    assert app.get("/api/status").json()["alerts"] == {"ntfy": False}
    got = app.post("/api/alerts/ntfy").json()
    topic = got["topic"]
    assert TOPIC_SHAPE.fullmatch(topic)
    assert got["subscribe"] == {"web": f"https://ntfy.sh/{topic}", "app": f"ntfy://ntfy.sh/{topic}"}
    assert got["alerts"] == {"ntfy": True}
    # Never again: not in what the dashboard reads, and not in the database as it is.
    for path in ("/api/status", "/api/settings", "/api/events", "/api/jobs", "/remote-approval/status",
                 "/remote-approval/audit"):
        r = app.get(path)
        assert r.status_code == 200 and topic not in r.text, path
    assert app.get("/api/status").json()["alerts"] == {"ntfy": True}
    assert topic not in "\n".join(app.store.conn.iterdump())
    assert notify.saved_topic(app.store) == topic
    # A new one replaces it, and turning alerts off forgets it.
    again = app.post("/api/alerts/ntfy").json()["topic"]
    assert again != topic and notify.saved_topic(app.store) == again
    assert app.delete("/api/alerts/ntfy").json() == {"alerts": {"ntfy": False}}
    assert app.get("/api/status").json()["alerts"] == {"ntfy": False} and notify.saved_topic(app.store) == ""
    said = [e["message"] for e in app.get("/api/events").json() if e["kind"] == "alerts"]
    assert said == ["A new phone alert channel was made. Subscribe to it in the ntfy app."] * 2 + [
        "Phone alerts were turned off."]


def test_the_subscribe_links_follow_ntfy_server(alerting):
    app = alerting(NTFY_SERVER="https://alerts.example.org/")
    got = app.post("/api/alerts/ntfy").json()
    topic = got["topic"]
    assert got["subscribe"] == {"web": f"https://alerts.example.org/{topic}", "app": f"ntfy://alerts.example.org/{topic}"}


def test_public_url_and_ntfy_server_are_checked_at_start(tmp_path):
    base = {"DATA_DIR": str(tmp_path / "data")}
    config = Config.from_env(base)
    assert (config.public_url, config.ntfy_server) == ("", "https://ntfy.sh")
    config = Config.from_env({**base, "PUBLIC_URL": f"{PUBLIC_URL}/", "NTFY_SERVER": "http://127.0.0.1:2586"})
    assert (config.public_url, config.ntfy_server) == (PUBLIC_URL, "http://127.0.0.1:2586")
    assert Config.from_env({**base, "PUBLIC_URL": "http://192.168.1.20:8080"}).public_url == "http://192.168.1.20:8080"
    # The topic is in every alert's path: it never crosses a network in the clear.
    for env in ({"PUBLIC_URL": "203-0-113-7.sslip.io"}, {"PUBLIC_URL": "ftp://203-0-113-7.sslip.io"},
                {"NTFY_SERVER": "http://alerts.example.org"}, {"NTFY_SERVER": "https://me:pw@alerts.example.org"}):
        with pytest.raises(ValueError, match=next(iter(env))):
            Config.from_env({**base, **env})


def test_the_judge_gets_403_on_every_alerts_route(alerting, ntfy):
    app = alerting(GAMEDAY_JUDGE_PASSWORD=JUDGE_PASSWORD)
    topic = channel(app)
    judge = TestClient(app.app, base_url="http://127.0.0.1", headers={**JUDGE_AUTH, **ACTION})
    for method, path in (("POST", "/api/alerts/ntfy"), ("DELETE", "/api/alerts/ntfy"), ("POST", "/api/alerts/test")):
        r = judge.request(method, path)
        assert r.status_code == 403 and r.json() == CANT_CHANGE, (method, path)
    assert judge.get("/api/alerts/ntfy").status_code == 403
    assert notify.saved_topic(app.store) == topic and ntfy.sent == []
    status = judge.get("/api/status")
    assert status.json()["alerts"] == {"ntfy": True} and topic not in status.text


def test_changing_alerts_needs_the_action_header(alerting):
    app = alerting()
    for method, path in (("POST", "/api/alerts/ntfy"), ("DELETE", "/api/alerts/ntfy"), ("POST", "/api/alerts/test")):
        assert app.client.request(method, path).status_code == 403, (method, path)
    assert notify.saved_topic(app.store) == ""


def test_the_test_alert(alerting, ntfy):
    app = alerting()
    assert app.post("/api/alerts/test").json() == {"ok": False,
                                                   "error": "There is no alert channel yet. Make one first."}
    topic = channel(app)
    assert app.post("/api/alerts/test").json() == {"ok": True}
    [request] = ntfy.sent
    assert request["title"] == "Test alert from Clips Kitty Game Day"
    assert request["url"] == f"https://ntfy.sh/{topic}" and request["headers"]["Click"] == f"{PUBLIC_URL}/#setup"
    ntfy.answer = requests.ConnectionError(f"Max retries exceeded with url: /{topic}")
    assert app.post("/api/alerts/test").json() == {"ok": False, "error": "couldn't reach ntfy."}


# ---- what alerts, and when --------------------------------------------------------------------


def test_a_finished_game_alerts_once_with_its_best_clip(alerting, ntfy):
    app = alerting()
    channel(app)
    job_id = upload(app.client, "Blue vs Gold.mp4")
    assert app.worker.run_next_job() == job_id
    [alert] = sent(app, ntfy)
    assert alert["title"] == "Blue vs Gold: 5 highlights ready"
    assert alert["body"] == "Poster dunk in the third\nOpen Game day to watch them."  # the best clip's title
    assert alert["headers"]["Tags"] == "basketball" and alert["headers"]["Click"] == f"{PUBLIC_URL}/#gameday"
    assert f"Job {job_id} is done: 5 clips ready." in [e["message"] for e in app.get("/api/events").json()]


def test_a_game_offered_on_the_phone_says_to_ask_muse(alerting, ntfy):
    app = alerting()
    switch_on(app.store, whatsapp=False, muse=True)
    channel(app)
    upload(app.client)
    app.worker.run_next_job()
    [alert] = sent(app, ntfy)
    assert alert["title"] == "game: 5 highlights ready"
    assert alert["body"] == "Poster dunk in the third\nAsk Muse what's ready, or open Phone offers."


def test_muse_sparks_line_for_the_offer_says_whats_ready(alerting, ntfy):
    app = alerting()
    app.worker.meta_factory = lambda: FakeMeta(app.store.ledger())
    switch_on(app.store, whatsapp=False, muse=True)
    channel(app)
    job_id = upload(app.client)
    app.worker.run_next_job()
    [alert] = sent(app, ntfy)
    [review] = store.reviews_for_job(app.store, job_id)
    line = review["summary"]["offer_line"]["line"]  # the offer's summary: what Muse says and the page shows
    assert line == "5 big plays, led by the poster dunk in the third"
    assert alert["title"] == "game: 5 highlights ready"
    assert alert["body"] == f"{line}\nAsk Muse what's ready, or open Phone offers."
    # Not offered on the phone, a game's alert names its best clip, as before.
    assert app.worker.alerts.game_ready(app.store.get_job(job_id), 5, offered=False, line=line)
    assert sent(app, ntfy)[-1]["body"] == "Poster dunk in the third\nOpen Game day to watch them."


@pytest.mark.parametrize("answer", [MetaError("Muse Spark answered 503.", status=503), "3 dunks and a block"],
                         ids=["spark-failed", "made-up-number"])
def test_without_muse_sparks_line_the_alert_names_the_best_clip(alerting, ntfy, answer):
    app = alerting()
    meta = FakeMeta(fail=answer) if isinstance(answer, Exception) else SaysLine(answer)  # there were 2 dunks
    app.worker.meta_factory = lambda: meta
    switch_on(app.store, whatsapp=False, muse=True)
    channel(app)
    upload(app.client)
    app.worker.run_next_job()
    [alert] = sent(app, ntfy)
    assert alert["body"] == "Poster dunk in the third\nAsk Muse what's ready, or open Phone offers."
    assert "offer_line" in [c[1] for c in meta.calls]  # asked, and not used


def test_a_game_that_didnt_finish_alerts_with_the_plain_error(alerting, ntfy):
    app = alerting(pipeline=broken_pipeline)
    channel(app)
    job_id = upload(app.client, "Blue vs Gold.mp4")
    app.worker.run_next_job()
    [alert] = sent(app, ntfy)
    assert alert["title"] == "Game didn't finish: Blue vs Gold"
    assert alert["body"] == "The video has no audio stream."
    assert alert["headers"]["Click"] == f"{PUBLIC_URL}/#gameday" and alert["headers"]["Priority"] == "4"
    assert f"Job {job_id} failed: The video has no audio stream." in [e["message"]
                                                                      for e in app.get("/api/events").json()]


def test_a_path_in_the_error_is_cut_to_its_file_name(alerting, ntfy):
    error = PipelineError("No clip could be rendered: C:\\Users\\someone\\gameday\\tmp\\clip_01.mp4 is empty")
    app = alerting(pipeline=FakePipeline(fail=error))
    channel(app)
    upload(app.client)
    app.worker.run_next_job()
    [alert] = sent(app, ntfy)
    assert alert["body"] == "No clip could be rendered: clip_01.mp4 is empty"


class Graph:
    """Meta's Graph API through the relay, stood in for. `refuse` maps a
    platform to the reason it refuses every post with."""

    def __init__(self, refuse=None):
        self.refuse = dict(refuse or {})
        self.calls: list[str] = []

    def instagram(self, ctx, path, caption, **kw):
        return self._post("instagram", "https://www.instagram.com/reel/R1/")

    def facebook(self, ctx, path, title, description, **kw):
        return self._post("facebook", "https://www.facebook.com/reel/F1")

    def _post(self, platform, url):
        from gameday.publish.meta_graph import Posted

        self.calls.append(platform)
        if platform in self.refuse:
            raise PublishError(self.refuse[platform])
        return Posted(platform, post_id=platform[:2].upper(), url=url)


SLOW_DOWN = "Instagram asked us to slow down. Try again in a few minutes (Meta error 4)."
TWO = "Poster dunk in the third and 1 more clip"


@pytest.mark.parametrize("refuse, title, body", [
    ({}, "Posted to Instagram + Facebook", TWO),
    ({"instagram": SLOW_DOWN}, "Posted to Facebook",
     f"{TWO}\nInstagram didn't post: {SLOW_DOWN} (1 more didn't post.)"),
    ({"instagram": SLOW_DOWN, "facebook": "Facebook refused the post (Meta error 100)."}, "Posting failed",
     f"{TWO}\nInstagram: {SLOW_DOWN} (3 more didn't post.)"),
], ids=["posted", "partly", "failed"])
def test_an_approval_alerts_once_when_its_uploads_have_settled(alerting, ntfy, monkeypatch, refuse, title, body):
    app = alerting()
    switch_on(app.store)
    relay, graph = FakeRelay(), Graph(refuse)
    monkeypatch.setattr(service, "client", lambda db: relay)
    monkeypatch.setattr(publishing, "make_graph", lambda config, device: graph)
    channel(app)
    job_id = upload(app.client)
    app.worker.run_next_job()
    app.worker.relay_step()  # the offer goes out
    [review] = store.reviews_for_job(app.store, job_id)
    first, second = (c["clip_id"] for c in review["clips"][:2])
    relay.pending.append(sign_command({
        "id": "cmd_" + "a" * 24, "kind": "approve", "approval_id": review["approval_id"], "review_ref": review["id"],
        "fingerprint": review["fingerprint"], "clips": [first, second], "platforms": ["instagram", "facebook"],
        "decided_at": store.stamp()}))
    app.worker.wake_relay()
    assert app.worker.relay_step() is True  # the approval, and the first clip's upload
    assert app.worker.relay_step() is True  # the second clip's
    assert [a["title"] for a in sent(app, ntfy)] == ["game: 5 highlights ready"]  # not settled yet: nothing
    for _ in range(2):  # it settles, and a pass after that says nothing more
        app.worker.wake_relay()
        app.worker.relay_step()
    alerts = sent(app, ntfy)
    assert [a["title"] for a in alerts] == ["game: 5 highlights ready", title]
    assert alerts[1]["body"] == body and alerts[1]["headers"]["Click"] == f"{PUBLIC_URL}/#offers"
    assert len(graph.calls) == 4


def test_a_judges_game_never_alerts(alerting, ntfy):
    app = alerting(GAMEDAY_JUDGE_PASSWORD=JUDGE_PASSWORD)
    switch_on(app.store, whatsapp=False, muse=True)  # the phone is ready for offers
    channel(app)
    job_id = upload(judge_client(app))
    assert app.worker.run_next_job() == job_id and app.store.get_job(job_id)["state"] == "done"
    # Asked directly too: a judge's game that failed, or an offer of its clips.
    assert not app.worker.alerts.game_failed(app.store.get_job(job_id), "It broke.")
    assert not app.worker.alerts.posted({"job_id": job_id, "results": PUBLISHED})
    assert sent(app, ntfy) == []
    # The owner's game, with the same settings, alerts.
    upload(app.client, "owner.mp4")
    app.worker.run_next_job()
    assert [a["title"] for a in sent(app, ntfy)] == ["owner: 5 highlights ready"]


def test_a_judges_failed_game_never_alerts(alerting, ntfy):
    app = alerting(pipeline=broken_pipeline, GAMEDAY_JUDGE_PASSWORD=JUDGE_PASSWORD)
    channel(app)
    job_id = upload(judge_client(app))
    app.worker.run_next_job()
    assert app.store.get_job(job_id)["state"] == "failed"
    assert sent(app, ntfy) == []


def test_nothing_alerts_without_a_channel(alerting, ntfy):
    app = alerting(pipeline=broken_pipeline)
    upload(app.client)
    app.worker.run_next_job()  # failed
    app.worker._pipeline = FakePipeline()
    upload(app.client, "second.mp4")
    app.worker.run_next_job()  # done
    assert not app.worker.alerts.alert("Hello", "There")
    assert not app.worker.alerts.posted({"job_id": 0, "results": PUBLISHED})
    assert sent(app, ntfy) == []
    # Nor once alerts are turned off again.
    channel(app)
    app.delete("/api/alerts/ntfy")
    upload(app.client, "third.mp4")
    app.worker.run_next_job()
    assert sent(app, ntfy) == []


def test_a_failing_ntfy_adds_one_warn_event_and_nothing_else(alerting, ntfy):
    app = alerting()
    topic = channel(app)
    ntfy.answer = requests.ConnectionError(f"Max retries exceeded with url: /{topic}")
    job_id = upload(app.client)
    assert app.worker.run_next_job() == job_id
    assert len(sent(app, ntfy)) == 1
    job = app.get(f"/api/jobs/{job_id}").json()
    assert job["state"] == "done" and job["clips"] == 5
    events = app.get("/api/events").json()
    warns = [e for e in events if e["level"] != "info"]
    assert [(e["kind"], e["message"], e["job_id"]) for e in warns] == [
        ("alerts", "Phone alert not sent: couldn't reach ntfy.", None)]
    assert topic not in json.dumps(events)
    # Whatever goes wrong in sending it, it stops there.
    ntfy.answer = RuntimeError("boom")
    assert app.worker.alerts.alert("Hello", "There")
    assert len(sent(app, ntfy)) == 2
    assert app.get("/api/events").json()[-1]["message"] == "Phone alert not sent: something went wrong (RuntimeError)."
    # The next game runs as before, and with ntfy back, its alert goes.
    ntfy.answer = (200, {"id": "m2"})
    upload(app.client, "second.mp4")
    app.worker.run_next_job()
    assert [a["title"] for a in sent(app, ntfy)] == ["game: 5 highlights ready", "Hello", "second: 5 highlights ready"]


def test_a_slow_ntfy_never_holds_up_a_job_or_the_relay_loop(alerting, ntfy):
    app = alerting()
    channel(app)
    ntfy.hold = threading.Event()
    started = time.monotonic()
    for name in ("game.mp4", "second.mp4"):
        upload(app.client, name)
        app.worker.run_next_job()
    app.worker.relay_step()
    assert time.monotonic() - started < 5  # ntfy hasn't answered once yet: each wait is 10 s
    assert not app.worker.alerts.wait_idle(0.2)
    ntfy.hold.set()
    assert [a["title"] for a in sent(app, ntfy)] == ["game: 5 highlights ready", "second: 5 highlights ready"]


def test_no_log_line_holds_the_topic(alerting, ntfy, caplog):
    caplog.set_level(logging.DEBUG)
    app = alerting(pipeline=broken_pipeline)
    topic = channel(app)
    app.post("/api/alerts/test")
    ntfy.answer = (500, {"error": f"topic {topic} is broken"})
    app.post("/api/alerts/test")
    upload(app.client)
    app.worker.run_next_job()
    assert len(sent(app, ntfy)) == 3
    assert any(r.getMessage().startswith("Phone alert not sent: ntfy answered 500") for r in caplog.records)
    assert topic not in caplog.text


def test_what_is_queued_goes_before_the_alerts_thread_stops(alerting, ntfy):
    app = alerting()
    channel(app)
    ntfy.hold = threading.Event()
    assert app.worker.alerts.alert("One", "first") and app.worker.alerts.alert("Two", "second")
    threading.Timer(0.3, ntfy.hold.set).start()
    app.worker.alerts.stop(timeout=10)
    assert ntfy.titles() == ["One", "Two"]
    assert app.worker.alerts.alert("Three", "third")  # started again, it alerts again
    assert [a["title"] for a in sent(app, ntfy)] == ["One", "Two", "Three"]


def test_the_lightsail_service_knows_the_dashboards_address():
    script = (Path(__file__).resolve().parents[2] / "scripts" / "setup-lightsail.sh").read_text(encoding="utf-8")
    assert "Environment=PUBLIC_URL=https://$DOMAIN\n" in script
