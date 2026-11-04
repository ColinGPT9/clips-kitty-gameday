"""The judge login (GAMEDAY_JUDGE_PASSWORD; gameday/access.py, docs/API.md
"The judge login").

The event's judges sign in as `judge`. They can open the dashboard, read what
it shows, upload a game, and cancel or retry the games uploaded with it.
Everything else is refused with 403 before any route runs, and changes
nothing. A judge's game is never offered on the owner's phone."""

from __future__ import annotations

import base64
import os
import re
import sqlite3

import pytest
from conftest import ACTION, AUTH, PASSWORD, VIDEO_BYTES
from fastapi import FastAPI
from fastapi.testclient import TestClient
from platform_fakes import SECRET, FakeMeta, FakeRelay, broken_pipeline, finished_job, switch_on

from gameday import access
from gameday.app import CHANGING, Guard, create_app
from gameday.config import Config, StartRefused
from gameday.remote_approval import notifier, service
from gameday.remote_approval import store as ra_store
from gameday.store import Store

JUDGE_PASSWORD = "judge-password-for-tests"
META_KEY = "meta-key-for-tests-" + "k" * 24


def basic(user: str, password: str) -> dict:
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


JUDGE_AUTH = basic("judge", JUDGE_PASSWORD)
CANT_CHANGE = {"error": "The judge login can't change this."}
CANT_OPEN = {"error": "The judge login can't open this."}
NOT_ITS_GAME = {"error": "The judge login can only cancel or retry games uploaded with it."}
NOT_OFFERED = "Uploaded with the judge login, so it isn't offered on the phone."


@pytest.fixture
def meta():
    return FakeMeta()


@pytest.fixture
def app(make_app, meta):
    """The app with the judge login on."""
    return make_app(meta_factory=lambda: meta, GAMEDAY_JUDGE_PASSWORD=JUDGE_PASSWORD)


@pytest.fixture
def judge(app):
    """A judge's browser: signed in as judge, sending the action header as the dashboard does."""
    return TestClient(app.app, base_url="http://127.0.0.1", headers={**JUDGE_AUTH, **ACTION})


def upload(client, name="game.mp4") -> int:
    r = client.post(f"/api/upload?filename={name}", content=VIDEO_BYTES + os.urandom(1000),
                    headers={"Content-Type": "application/octet-stream", **ACTION})
    assert r.status_code == 200, r.text
    return r.json()["job_id"]


def dump(db) -> str:
    """The whole database, to show a refused request changed nothing."""
    return "\n".join(db.conn.iterdump())


@pytest.fixture
def owned(app, monkeypatch, tmp_path):
    """What the owner has set up: a saved Meta key, a watched folder, phone
    approval through a relay with an offer open on it, and a queued game."""
    relay = FakeRelay()
    monkeypatch.setattr(service, "client", lambda db: relay)
    assert app.put("/api/meta/key", json={"key": META_KEY}).status_code == 200
    watched, spare = tmp_path / "watched", tmp_path / "spare"
    watched.mkdir()
    spare.mkdir()
    watch = app.post("/api/watches", json={"path": str(watched)}).json()
    switch_on(app.store, whatsapp=False, muse=True)
    review = notifier.on_job_finished(app.store, finished_job(app.store, app.config.data_dir), "done")
    assert review is not None
    return {"relay": relay, "watch": watch["id"], "review": review, "folder": str(spare), "job": upload(app.client)}


# ---- who signed in --------------------------------------------------------------------


def test_status_says_who_signed_in(app, judge):
    assert app.get("/api/status").json()["role"] == "owner"
    assert judge.get("/api/status").json()["role"] == "judge"


def test_without_a_judge_password_there_is_no_judge_login(make_app):
    gd = make_app(GAMEDAY_JUDGE_PASSWORD="")
    for headers in (JUDGE_AUTH, basic("judge", "")):
        r = TestClient(gd.app, base_url="http://127.0.0.1", headers=headers).get("/api/status")
        assert r.status_code == 401 and r.json() == {"error": "Sign in as owner to use Clips Kitty Game Day."}
    assert gd.get("/api/status").json()["role"] == "owner"


def test_neither_password_signs_in_as_the_other_user(app):
    for headers in (basic("owner", JUDGE_PASSWORD), basic("judge", PASSWORD), basic("admin", JUDGE_PASSWORD)):
        r = TestClient(app.app, base_url="http://127.0.0.1", headers=headers).get("/api/status")
        assert r.status_code == 401
        assert r.json() == {"error": "Sign in as owner or judge to use Clips Kitty Game Day."}
        assert JUDGE_PASSWORD not in r.text and PASSWORD not in r.text


# ---- what the judge may do ------------------------------------------------------------


def reads(job_id: int) -> list[str]:
    """Everything the dashboard reads."""
    return ["/", "/static/app.js", "/static/app.css", f"/media/jobs/{job_id}/clips/clip_01.mp4",
            f"/media/jobs/{job_id}/clips/clip_01.jpg", "/api/status", "/api/settings", "/api/watches",
            "/api/jobs?limit=20", f"/api/jobs/{job_id}", "/api/clips?limit=50", f"/api/clips?job_id={job_id}",
            "/api/usage", "/api/events?after=0&limit=100", "/remote-approval/status",
            "/remote-approval/reviews?limit=20"]


def test_the_judge_reads_everything_the_dashboard_shows(app, judge, owned):
    [job] = [j for j in judge.get("/api/jobs").json() if j["clips"]]
    for path in reads(job["id"]):
        r = judge.get(path)
        assert r.status_code == 200, (path, r.text)
    assert judge.head("/").status_code == 200
    assert [r["id"] for r in judge.get("/remote-approval/reviews").json()["reviews"]] == [owned["review"]]
    assert [w["id"] for w in judge.get("/api/watches").json()] == [owned["watch"]]


def test_nothing_the_judge_reads_holds_a_secret(app, judge, owned):
    [job] = [j for j in judge.get("/api/jobs").json() if j["clips"]]
    seen = "\n".join(judge.get(path).text for path in reads(job["id"]) if not path.startswith("/media/"))
    for secret in (META_KEY, SECRET, PASSWORD, JUDGE_PASSWORD):
        assert secret not in seen


# Each with a body that would work for the owner.
REFUSED = [
    ("PATCH", "/api/settings", {"max_clips": 3, "caption_credit": "Judged"}),
    ("PUT", "/api/meta/key", {"key": "another-key-0123456789"}),
    ("DELETE", "/api/meta/key", None),
    ("POST", "/api/meta/test", None),
    ("POST", "/api/watches", {"path": "{folder}"}),
    ("PATCH", "/api/watches/{watch}", {"enabled": False}),
    ("DELETE", "/api/watches/{watch}", None),
    ("PATCH", "/remote-approval/settings", {"remote_approvals": False, "max_clips": 2}),
    ("POST", "/remote-approval/register", {"relay_url": "https://relay.test", "enroll_key": "e" * 32}),
    ("POST", "/remote-approval/refresh", None),
    ("POST", "/remote-approval/pair/start", None),
    ("POST", "/remote-approval/pair/confirm", {"confirm": True}),
    ("POST", "/remote-approval/pin", {"pin": "2468"}),
    ("POST", "/remote-approval/muse/token", None),
    ("POST", "/remote-approval/muse/revoke", None),
    ("POST", "/remote-approval/test", None),
    ("POST", "/remote-approval/disconnect", None),
    ("POST", "/remote-approval/accounts/connect", {"platform": "facebook"}),
    ("POST", "/remote-approval/accounts/disconnect", {"platform": "instagram"}),
    ("POST", "/remote-approval/templates", {"create": True}),
    ("POST", "/remote-approval/reviews/{review}/cancel", None),
    ("POST", "/remote-approval/reviews/{review}/retry", None),
    ("POST", "/remote-approval/reviews/{review}/ask-again", None),
    ("POST", "/remote-approval/reviews/{review}/resend", None),
]


@pytest.mark.parametrize("method, path, body", REFUSED, ids=[f"{m} {p}" for m, p, _ in REFUSED])
def test_the_judge_cant_change_settings_the_key_watches_or_the_phone(app, judge, owned, meta, method, path, body):
    before = dump(app.store)
    if body is not None:
        body = {k: v.format(**owned) if isinstance(v, str) else v for k, v in body.items()}
    r = judge.request(method, path.format(**owned), json=body)
    assert r.status_code == 403 and r.json() == CANT_CHANGE, r.text
    assert dump(app.store) == before
    assert owned["relay"].calls == [] and meta.calls == []


def test_the_judge_cant_cancel_or_retry_the_owners_games(app, judge, owned):
    cancelled = upload(app.client, "second.mp4")
    app.post(f"/api/jobs/{cancelled}/cancel")
    video = app.store.add_video(name="found.mp4", path=str(app.config.data_dir / "found.mp4"), source="watch")
    found = app.store.add_job(video, {})  # a watched folder's game is the owner's too
    before = dump(app.store)
    for path in (f"/api/jobs/{owned['job']}/cancel", f"/api/jobs/{cancelled}/retry", f"/api/jobs/{found}/cancel"):
        r = judge.post(path)
        assert r.status_code == 403 and r.json() == NOT_ITS_GAME, path
    assert dump(app.store) == before
    assert judge.post("/api/jobs/999/cancel").status_code == 404


def test_the_judge_cancels_and_retries_its_own_games(app, judge):
    job_id = upload(judge)
    assert app.store.get_job(job_id)["created_by"] == "judge"
    assert judge.get(f"/api/jobs/{job_id}").json()["created_by"] == "judge"
    r = judge.post(f"/api/jobs/{job_id}/cancel")
    assert r.status_code == 200 and r.json()["state"] == "cancelled"
    r = judge.post(f"/api/jobs/{job_id}/retry")
    assert r.status_code == 200
    again = r.json()
    assert again["state"] == "queued" and again["created_by"] == "judge"  # the same game: still the judge's
    # The owner may stop and rerun a judge's game too; it stays the judge's.
    assert app.post(f"/api/jobs/{again['id']}/cancel").json()["created_by"] == "judge"
    assert app.post(f"/api/jobs/{again['id']}/retry").json()["created_by"] == "judge"
    events = [e["message"] for e in judge.get("/api/events").json()]
    assert f"Uploaded game.mp4 with the judge login: job {job_id} is queued." in events
    owners = upload(app.client, "owner.mp4")
    assert app.get(f"/api/jobs/{owners}").json()["created_by"] == "owner"


def test_every_other_route_is_the_owners_alone(app, judge):
    """Default deny: a route added later is refused to the judge until access.JUDGE_MAY lists it."""
    allowed = {
        ("GET", "/"), ("HEAD", "/"), ("GET", "/static/{rel:path}"), ("HEAD", "/static/{rel:path}"),
        ("GET", "/media/jobs/{job_id}/clips/{name}"), ("HEAD", "/media/jobs/{job_id}/clips/{name}"),
        ("GET", "/api/status"), ("GET", "/api/settings"), ("GET", "/api/watches"), ("GET", "/api/jobs"),
        ("GET", "/api/jobs/{job_id}"), ("GET", "/api/clips"), ("GET", "/api/usage"), ("GET", "/api/events"),
        ("GET", "/remote-approval/status"), ("GET", "/remote-approval/reviews"), ("POST", "/api/upload"),
        ("POST", "/api/jobs/{job_id}/cancel"), ("POST", "/api/jobs/{job_id}/retry"),
    }
    routes = {(method, route.path) for route in app.app.routes for method in getattr(route, "methods", None) or ()
              if route.path != "/health"}
    assert allowed < routes
    refused = 0
    for method, template in sorted(routes):
        path = re.sub(r"\{[^}]+\}", "1", template)
        assert access.judge_may(method, path) == ((method, template) in allowed), (method, template)
        if (method, template) not in allowed:
            r = judge.request(method, path)
            assert r.status_code == 403, (method, template)
            assert r.json() == (CANT_CHANGE if method in CHANGING else CANT_OPEN), (method, template)
            refused += 1
    assert refused >= 26  # every change but the three above, an offer's details and the audit log
    # A path that is no route at all is refused too, not looked up.
    assert judge.post("/api/new-thing").json() == CANT_CHANGE
    assert judge.get("/remote-approval/audit").json() == CANT_OPEN
    assert judge.get("/api/upload/").status_code == 403


def test_the_owner_is_unaffected(app, judge, owned):
    assert app.patch("/api/settings", json={"max_clips": 3}).json()["max_clips"] == 3
    assert judge.get("/api/settings").json()["max_clips"] == 3
    assert app.post(f"/api/jobs/{owned['job']}/cancel").status_code == 200
    assert app.get(f"/remote-approval/reviews/{owned['review']}").status_code == 200
    assert app.get("/remote-approval/audit").status_code == 200
    assert app.post(f"/remote-approval/reviews/{owned['review']}/cancel").status_code == 200
    assert app.delete(f"/api/watches/{owned['watch']}").json() == {"deleted": True}


# ---- a judge's game and the owner's phone ---------------------------------------------


def test_a_judges_game_is_never_offered_on_the_owners_phone(app, judge):
    switch_on(app.store, whatsapp=False, muse=True)  # the phone is ready for offers
    job_id = upload(judge)
    assert app.worker.run_next_job() == job_id
    job = judge.get(f"/api/jobs/{job_id}").json()
    assert job["state"] == "done" and job["clips"] == 5 and job["clips_list"]
    assert job["stages"]["captions"] == {"state": "skipped", "seconds": 0.0, "cost_usd": 0.0, "note": NOT_OFFERED}
    assert ra_store.reviews_for_job(app.store, job_id) == [] and ra_store.due_outbox(app.store) == []
    # Asked directly, the notifier says nothing about it either.
    assert notifier.on_job_finished(app.store, app.store.get_job(job_id), "done") is None
    # The owner's game, with the same settings, is offered.
    owners = upload(app.client, "owner.mp4")
    assert app.worker.run_next_job() == owners
    assert len(ra_store.reviews_for_job(app.store, owners)) == 1


def test_a_judges_failed_game_sends_the_owner_nothing(make_app):
    app = make_app(pipeline=broken_pipeline, GAMEDAY_JUDGE_PASSWORD=JUDGE_PASSWORD)
    switch_on(app.store)  # WhatsApp on: an owner's failed game would queue a message
    judge = TestClient(app.app, base_url="http://127.0.0.1", headers=JUDGE_AUTH)
    job_id = upload(judge)
    app.worker.run_next_job()
    assert app.store.get_job(job_id)["state"] == "failed"
    assert ra_store.due_outbox(app.store) == []


# ---- signing in: the lockout ------------------------------------------------------------


def _guarded(clock):
    inner = FastAPI()
    inner.get("/api/status")(lambda: {"ok": True})
    return Guard(inner, password=PASSWORD, judge_password=JUDGE_PASSWORD, clock=clock)


WRONG_JUDGE = basic("judge", "a-wrong-judge-guess")
WRONG_OWNER = basic("owner", "a-wrong-owner-guess")


def test_wrong_judge_passwords_lock_the_address_out_like_the_owners(caplog):
    now = [0.0]
    guard = _guarded(lambda: now[0])
    visitor = TestClient(guard, client=("203.0.113.20", 4000))
    for _ in range(10):
        assert visitor.get("/api/status", headers=WRONG_JUDGE).status_code == 401
    for headers in (JUDGE_AUTH, AUTH):  # locked: neither right password is checked
        r = visitor.get("/api/status", headers=headers)
        assert r.status_code == 429 and r.headers["Retry-After"] == "900"
    assert "203.0.113.20" in caplog.text
    assert JUDGE_PASSWORD not in caplog.text and "a-wrong-judge-guess" not in caplog.text
    assert TestClient(guard, client=("198.51.100.20", 4000)).get("/api/status", headers=JUDGE_AUTH).status_code == 200
    now[0] += 900
    assert visitor.get("/api/status", headers=JUDGE_AUTH).status_code == 200


def test_the_judge_password_never_wipes_out_guesses_at_the_owners():
    guard = _guarded(lambda: 0.0)
    guesser = TestClient(guard, client=("203.0.113.21", 4000))
    for _ in range(9):
        guesser.get("/api/status", headers=WRONG_OWNER)
    assert guesser.get("/api/status", headers=JUDGE_AUTH).status_code == 200  # forgives nothing
    assert guesser.get("/api/status", headers=WRONG_OWNER).status_code == 401  # the 10th
    assert guesser.get("/api/status", headers=JUDGE_AUTH).status_code == 429
    # The owner's own sign-in still forgives earlier typos, the judge's included.
    owner = TestClient(guard, client=("198.51.100.21", 4000))
    for headers in [WRONG_JUDGE] * 5 + [WRONG_OWNER] * 4:
        owner.get("/api/status", headers=headers)
    assert owner.get("/api/status", headers=AUTH).status_code == 200
    for _ in range(9):
        owner.get("/api/status", headers=WRONG_OWNER)
    assert owner.get("/api/status", headers=AUTH).status_code == 200


# ---- starting -------------------------------------------------------------------------------


@pytest.mark.parametrize("host", ["0.0.0.0", "127.0.0.1"])
@pytest.mark.parametrize("judge_password, words", [("short-judge", "shorter than 12 characters"),
                                                    (PASSWORD, "is the owner password")])
def test_start_up_refuses_a_short_judge_password_or_the_owners(tmp_path, monkeypatch, capsys, host,
                                                                judge_password, words):
    import dotenv
    import uvicorn

    from gameday import __main__ as entry

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **kw: False)  # no .env from the checkout
    env = {"DATA_DIR": str(tmp_path / "data"), "HOST": host, "GAMEDAY_PASSWORD": PASSWORD,
           "GAMEDAY_JUDGE_PASSWORD": judge_password}
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    ran = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: ran.append(kw))
    assert entry.main() == 2 and ran == []
    err = capsys.readouterr().err
    assert err.startswith("Clips Kitty Game Day can't start: GAMEDAY_JUDGE_PASSWORD ") and words in err
    assert judge_password not in err and PASSWORD not in err


def test_create_app_refuses_a_judge_password_it_wont_use(tmp_path):
    with pytest.raises(StartRefused, match="shorter than 12"):
        Config.from_env({"DATA_DIR": str(tmp_path / "data"), "GAMEDAY_JUDGE_PASSWORD": "x" * 11})
    for judge_password in ("x" * 11, PASSWORD):
        config = Config(data_dir=tmp_path / "data", password=PASSWORD, judge_password=judge_password)
        with pytest.raises(StartRefused):
            create_app(config, run_threads=False)
    app = create_app(Config(data_dir=tmp_path / "data", password=PASSWORD, judge_password="x" * 12),
                     run_threads=False)
    app.state.store.close_all()


def test_a_database_from_before_the_judge_login_gets_its_column(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, video_id INTEGER NOT NULL, "
                "state TEXT NOT NULL DEFAULT 'queued', stage TEXT, stage_progress REAL NOT NULL DEFAULT 0, "
                "message TEXT NOT NULL DEFAULT '', stages TEXT NOT NULL DEFAULT '{}', "
                "options TEXT NOT NULL DEFAULT '{}', clips INTEGER NOT NULL DEFAULT 0, "
                "warnings TEXT NOT NULL DEFAULT '[]', error TEXT NOT NULL DEFAULT '', "
                "stats TEXT NOT NULL DEFAULT '{}', cancel_requested INTEGER NOT NULL DEFAULT 0, retry_of INTEGER, "
                "created_at REAL NOT NULL, started_at REAL, finished_at REAL)")
    old.execute("INSERT INTO jobs (video_id, created_at) VALUES (1, 0)")
    old.commit()
    old.close()
    db = Store(path)
    assert db.get_job(1)["created_by"] == "owner"  # every game from before was the owner's
    assert db.get_job(db.add_job(1, {}, created_by="judge"))["created_by"] == "judge"
    with pytest.raises(ValueError):
        db.add_job(1, {}, created_by="admin")
    db.close_all()
    again = Store(path)  # opened again: the column is there already
    assert again.get_job(2)["created_by"] == "judge"
    again.close_all()
