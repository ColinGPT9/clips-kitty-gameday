"""The web app's rules and routes (gameday/app.py, gameday/routes/): sign-in,
the action header, the security headers, /media containment and byte
ranges, uploads, settings, the Meta key, watched folders and jobs."""

from __future__ import annotations

import base64
import json
import os

import pytest
from conftest import ACTION, AUTH, PASSWORD, VIDEO_BYTES, make_config
from fastapi.testclient import TestClient
from platform_fakes import FakeMeta, finished_job

from gameday import secrets as secret_store
from gameday.app import CSP
from gameday.config import Config, owner_password
from gameday.store import Store
from gameday.types import MODEL_SAM, MODEL_SPARK, MODEL_VOICE, ApiCall

# ---- signing in -------------------------------------------------------------------


ROUTES = [
    ("GET", "/api/status"), ("GET", "/api/settings"), ("PATCH", "/api/settings"), ("PUT", "/api/meta/key"),
    ("DELETE", "/api/meta/key"), ("POST", "/api/meta/test"), ("POST", "/api/upload"), ("GET", "/api/watches"),
    ("POST", "/api/watches"), ("PATCH", "/api/watches/1"), ("DELETE", "/api/watches/1"), ("GET", "/api/jobs"),
    ("GET", "/api/jobs/1"), ("POST", "/api/jobs/1/cancel"), ("POST", "/api/jobs/1/retry"), ("GET", "/api/clips"),
    ("GET", "/api/usage"), ("GET", "/api/events"), ("GET", "/"), ("GET", "/static/app.js"),
    ("GET", "/media/jobs/1/clips/clip_01.mp4"), ("GET", "/remote-approval/status"),
    ("POST", "/remote-approval/register"), ("GET", "/remote-approval/reviews"),
]


@pytest.mark.parametrize("method, path", ROUTES)
def test_every_route_but_health_needs_the_owner_password(gd, method, path):
    anonymous = TestClient(gd.app, base_url="http://127.0.0.1")
    for headers in ({}, {"Authorization": "Basic " + base64.b64encode(b"owner:wrong").decode()},
                    {"Authorization": "Basic " + base64.b64encode(f"admin:{PASSWORD}".encode()).decode()},
                    {"Authorization": "Bearer " + PASSWORD}, {"Authorization": "Basic not-base64!"}):
        r = anonymous.request(method, path, headers={**headers, **ACTION})
        assert r.status_code == 401, (method, path, headers)
        assert r.headers["WWW-Authenticate"].startswith("Basic ")
        assert r.json() == {"error": "Sign in as owner to use Clips Kitty Game Day."}
        assert PASSWORD not in r.text


def test_health_is_open_and_says_what_it_is(gd):
    r = TestClient(gd.app, base_url="http://127.0.0.1").get("/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "app": "Clips Kitty Game Day", "version": "0.1.0"}


@pytest.mark.parametrize("method, path", [(m, p) for m, p in ROUTES if m != "GET"])
def test_every_change_needs_the_action_header(gd, method, path):
    r = gd.client.request(method, path)
    assert r.status_code == 403
    assert "X-Gameday-Action" in r.json()["error"]
    r = gd.client.request(method, path, headers={"X-Gameday-Action": "yes"})
    assert r.status_code == 403


def test_the_password_is_generated_once_and_logged_only_by_where_it_is(tmp_path, caplog):
    config = Config.from_env({"DATA_DIR": str(tmp_path / "data")})
    with caplog.at_level("INFO", logger="gameday"):
        first = owner_password(config)
    assert len(first) >= 20 and first == owner_password(config)
    assert (tmp_path / "data" / "owner-password.txt").read_text().strip() == first
    assert first not in caplog.text and "owner-password.txt" in caplog.text
    assert owner_password(Config.from_env({"DATA_DIR": str(tmp_path / "data"), "GAMEDAY_PASSWORD": "x"})) == "x"


def _guarded(clock):
    from fastapi import FastAPI

    from gameday.app import Guard

    inner = FastAPI()
    inner.get("/api/status")(lambda: {"ok": True})
    return Guard(inner, password=PASSWORD, clock=clock)


WRONG = {"Authorization": "Basic " + base64.b64encode(b"owner:guess").decode()}


def test_ten_wrong_passwords_lock_that_address_out_for_15_minutes(caplog):
    now = [1000.0]
    guard = _guarded(lambda: now[0])
    attacker = TestClient(guard, client=("203.0.113.9", 4000))
    for _ in range(9):
        assert attacker.get("/api/status", headers=WRONG).status_code == 401
    assert attacker.get("/api/status", headers=WRONG).status_code == 401  # the 10th still gets a plain answer
    r = attacker.get("/api/status", headers=AUTH)  # locked: even the right password isn't checked
    assert r.status_code == 429 and r.headers["Retry-After"] == "900"
    assert r.headers["Content-Security-Policy"] == CSP and "15 minutes" in r.json()["error"]
    assert "203.0.113.9" in caplog.text and PASSWORD not in caplog.text and "guess" not in caplog.text
    owner = TestClient(guard, client=("198.51.100.7", 4000))
    assert owner.get("/api/status", headers=AUTH).status_code == 200  # another address isn't affected
    now[0] += 600
    assert attacker.get("/api/status", headers=AUTH).headers["Retry-After"] == "300"
    now[0] += 300
    assert attacker.get("/api/status", headers=AUTH).status_code == 200


def test_no_password_never_counts_and_a_right_one_forgives_typos():
    now = [0.0]
    guard = _guarded(lambda: now[0])
    browser = TestClient(guard, client=("203.0.113.10", 4000))
    for _ in range(30):
        assert browser.get("/api/status").status_code == 401  # a browser's first try has no header
    for _ in range(9):
        assert browser.get("/api/status", headers=WRONG).status_code == 401
    assert browser.get("/api/status", headers=AUTH).status_code == 200
    for _ in range(9):
        assert browser.get("/api/status", headers=WRONG).status_code == 401
    assert browser.get("/api/status", headers=AUTH).status_code == 200
    for _ in range(5):
        browser.get("/api/status", headers=WRONG)
    now[0] += 601  # wrong tries older than 10 minutes are forgotten too
    for _ in range(9):
        browser.get("/api/status", headers=WRONG)
    assert browser.get("/api/status").status_code == 401


def test_behind_the_proxy_each_visitor_is_counted_on_their_own():
    guard = _guarded(lambda: 0.0)
    caddy = TestClient(guard, client=("172.18.0.3", 4000))  # the proxy, in a container beside the app
    for _ in range(10):
        caddy.get("/api/status", headers={**WRONG, "X-Forwarded-For": "203.0.113.11"})
    assert caddy.get("/api/status", headers={**AUTH, "X-Forwarded-For": "203.0.113.11"}).status_code == 429
    assert caddy.get("/api/status", headers={**AUTH, "X-Forwarded-For": "198.51.100.8"}).status_code == 200
    # A visitor reaching the app straight from the internet can't pick their own address.
    direct = TestClient(guard, client=("11.22.33.44", 4000))  # any address on the internet
    for i in range(10):
        direct.get("/api/status", headers={**WRONG, "X-Forwarded-For": f"10.0.0.{i}"})
    assert direct.get("/api/status", headers=AUTH).status_code == 429


@pytest.mark.parametrize("host, code", [("0.0.0.0", 2), ("127.0.0.1", 0)])
def test_a_short_password_is_refused_unless_the_app_is_local_only(tmp_path, monkeypatch, capsys, host, code):
    import dotenv
    import uvicorn

    from gameday import __main__ as entry

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **kw: False)  # no .env from the checkout
    for name, value in {"DATA_DIR": str(tmp_path / "data"), "HOST": host, "GAMEDAY_PASSWORD": "short1"}.items():
        monkeypatch.setenv(name, value)
    ran = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: ran.append(kw))
    assert entry.main() == code
    err = capsys.readouterr().err
    assert "short1" not in err
    assert ("shorter than 12" in err) == bool(code) and len(ran) == (0 if code else 1)


# ---- headers ---------------------------------------------------------------------


def _secured(r):
    assert r.headers["Content-Security-Policy"] == CSP
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["Referrer-Policy"] == "no-referrer"


def test_every_answer_carries_the_security_headers(gd):
    job = finished_job(gd.store, gd.config.data_dir)
    anonymous = TestClient(gd.app, base_url="http://127.0.0.1")
    for r in (anonymous.get("/health"), anonymous.get("/api/status"), gd.client.post("/api/settings"),
              gd.get("/api/status"), gd.get("/nope"), gd.get("/"), gd.get("/api/jobs/999"),
              gd.get(f"/media/jobs/{job['id']}/clips/clip_01.mp4"), gd.get("/media/jobs/1/clips/missing.mp4"),
              gd.patch("/api/settings", json={"mode": "soccer"})):
        _secured(r)
    assert CSP == ("default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; style-src 'self'; "
                   "script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; "
                   "frame-ancestors 'none'; form-action 'self'")


def test_errors_are_a_sentence_in_json(gd):
    assert gd.get("/nope").json() == {"error": "Not found."}
    assert gd.get("/api/jobs/999").json() == {"error": "There is no such job."}
    r = gd.patch("/api/settings", content=b"not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 400 and set(r.json()) == {"error"}
    r = gd.get("/api/jobs?limit=0")
    assert r.status_code == 400 and r.json()["error"].startswith("The request isn't valid")


def test_an_unexpected_error_is_a_500_with_the_headers(gd, monkeypatch):
    monkeypatch.setattr(gd.store, "queued_count", lambda: 1 / 0)
    r = TestClient(gd.app, base_url="http://127.0.0.1", headers=AUTH, raise_server_exceptions=False).get(
        "/api/status")
    assert r.status_code == 500 and r.json() == {"error": "Something went wrong in the app."}
    _secured(r)


# ---- /media ----------------------------------------------------------------------


@pytest.fixture
def clips(gd):
    job = finished_job(gd.store, gd.config.data_dir)
    folder = gd.config.data_dir / "media" / "jobs" / str(job["id"])
    (folder / "tmp").mkdir()
    (folder / "tmp" / "scratch.mp4").write_bytes(b"secret scratch")
    (folder / "clips" / "notes.txt").write_text("not served")
    (folder / "clips" / "upper.MP4").write_bytes(b"upper case suffix")
    return job


def test_media_serves_a_jobs_clips_thumbnails_and_debug_images(gd, clips):
    folder = gd.config.data_dir / "media" / "jobs" / str(clips["id"]) / "clips"
    r = gd.get(f"/media/jobs/{clips['id']}/clips/clip_01.mp4")
    assert r.status_code == 200 and r.headers["content-type"] == "video/mp4"
    assert r.content == (folder / "clip_01.mp4").read_bytes()
    assert gd.get(f"/media/jobs/{clips['id']}/clips/clip_01.jpg").headers["content-type"] == "image/jpeg"
    (folder / "clip_01_debug.png").write_bytes(b"\x89PNG")
    assert gd.get(f"/media/jobs/{clips['id']}/clips/clip_01_debug.png").status_code == 200


@pytest.mark.parametrize("path", [
    "/media/jobs/{id}/tmp/scratch.mp4",
    "/media/jobs/{id}/clips/..%2Ftmp%2Fscratch.mp4",
    "/media/jobs/{id}/clips/%2e%2e%2ftmp%2fscratch.mp4",
    "/media/jobs/{id}/clips/..%5Ctmp%5Cscratch.mp4",
    "/media/jobs/{id}/clips/notes.txt",
    "/media/jobs/{id}/clips/upper.MP4",
    "/media/jobs/{id}/clips/.clip_01.mp4",
    "/media/jobs/..%2F..%2F/clips/clip_01.mp4",
    "/media/jobs/x/clips/clip_01.mp4",
    "/media/jobs/%C2%B2/clips/clip_01.mp4",  # "²" is a digit to str.isdigit, not to int()
    "/media/jobs/%D9%A1/clips/clip_01.mp4",  # "١", an Arabic-Indic one
    "/media/jobs/{id}/clips/missing.mp4",
    "/media/jobs/{id}/clips/",
    "/media/owner-password.txt",
    "/media/../owner-password.txt",
    "/media/jobs/{id}/clips/..",
])
def test_media_serves_nothing_else(gd, clips, path):
    r = gd.get(path.format(id=clips["id"]))
    assert r.status_code == 404, path
    assert b"secret scratch" not in r.content and b"not served" not in r.content


def test_a_clip_link_outside_its_folder_is_not_followed(gd, clips, tmp_path):
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"outside")
    link = gd.config.data_dir / "media" / "jobs" / str(clips["id"]) / "clips" / "linked.mp4"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("this system can't make a symbolic link here")
    assert gd.get(f"/media/jobs/{clips['id']}/clips/linked.mp4").status_code == 404


def test_media_answers_byte_ranges(gd, clips):
    url = f"/media/jobs/{clips['id']}/clips/clip_01.mp4"
    whole = gd.get(url).content
    r = gd.get(url, headers={"Range": "bytes=0-1"})
    assert r.status_code == 206 and r.content == whole[:2]
    assert r.headers["content-range"] == f"bytes 0-1/{len(whole)}"
    r = gd.get(url, headers={"Range": "bytes=100-"})
    assert r.status_code == 206 and r.content == whole[100:]
    r = gd.get(url, headers={"Range": f"bytes={len(whole) + 10}-"})
    assert r.status_code == 416
    _secured(r)


def test_the_dashboard_files_are_served_when_they_are_there(gd, monkeypatch, tmp_path):
    from gameday.routes import files

    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html><title>Dashboard</title>", encoding="utf-8")
    (static / "app.js").write_text("console.log(1)", encoding="utf-8")
    (static / "app.css").write_text("body{}", encoding="utf-8")
    monkeypatch.setattr(files, "STATIC_DIR", static)
    r = gd.get("/")
    assert r.status_code == 200 and "Dashboard" in r.text and r.headers["content-type"].startswith("text/html")
    assert gd.get("/static/app.js").headers["content-type"].startswith("text/javascript")
    assert gd.get("/static/app.css").headers["content-type"].startswith("text/css")
    for bad in ("/static/../app.py", "/static/%2e%2e/app.py", "/static/missing.js", "/static/.hidden.js",
                "/static/app.py"):
        assert gd.get(bad).status_code == 404, bad


def test_without_the_dashboard_files_the_app_still_answers(gd, monkeypatch, tmp_path):
    from gameday.routes import files

    monkeypatch.setattr(files, "STATIC_DIR", tmp_path / "nothing")
    r = gd.get("/")
    assert r.status_code == 200 and "dashboard files are not installed" in r.text
    assert "<script" not in r.text and "style=" not in r.text
    assert gd.get("/static/app.js").status_code == 404


# ---- upload ----------------------------------------------------------------------


def test_an_upload_is_streamed_in_and_queued_with_the_current_settings(gd):
    gd.patch("/api/settings", json={"max_clips": 4, "hint": "Blue and Gold"})
    body = VIDEO_BYTES + os.urandom(300_000)
    r = gd.post("/api/upload?filename=../../Game Night.mp4", content=body,
                headers={"Content-Type": "application/octet-stream"})
    assert r.status_code == 200, r.text
    got = r.json()
    assert set(got) == {"video_id", "job_id"}
    video = gd.store.get_video(got["video_id"])
    assert video["name"] == "Game Night.mp4" and video["source"] == "upload"
    saved = gd.config.uploads_dir
    assert [p.read_bytes() for p in saved.iterdir()] == [body]
    job = gd.get(f"/api/jobs/{got['job_id']}").json()
    assert job["state"] == "queued" and job["video"] == {"id": got["video_id"], "name": "Game Night.mp4",
                                                          "duration_s": 1800.0, "source": "upload"}
    assert json.loads(gd.store.get_job(got["job_id"])["options"]) == {
        "mode": "basketball", "max_clips": 4, "hint": "Blue and Gold", "title_cards": True}


def test_an_upload_over_the_limit_is_refused_and_nothing_is_kept(gd):
    limit = gd.config.max_upload_bytes
    r = gd.post("/api/upload?filename=big.mp4", content=VIDEO_BYTES + b"\0" * limit,
                headers={"Content-Type": "application/octet-stream"})
    assert r.status_code == 413 and "5 MB" in r.json()["error"]
    assert list(gd.config.uploads_dir.iterdir()) == []

    def chunks():  # no Content-Length: the stream itself is counted
        yield VIDEO_BYTES
        for _ in range(6):
            yield b"\0" * (1024 * 1024)

    r = gd.post("/api/upload?filename=big.mp4", content=chunks(), headers={"Content-Type": "application/octet-stream"})
    assert r.status_code == 413
    assert list(gd.config.uploads_dir.iterdir()) == []
    assert gd.store.jobs() == []


def test_a_file_that_is_not_a_video_is_refused_and_removed(gd):
    r = gd.post("/api/upload?filename=notes.mp4", content=b"just some text",
                headers={"Content-Type": "application/octet-stream"})
    assert r.status_code == 400 and "isn't a video" in r.json()["error"]
    assert list(gd.config.uploads_dir.iterdir()) == []
    r = gd.post("/api/upload?filename=empty.mp4", content=b"", headers={"Content-Type": "application/octet-stream"})
    assert r.status_code == 400
    assert gd.store.jobs() == []


@pytest.mark.media
def test_a_real_ffprobe_refuses_random_bytes(make_app):
    from platform_fakes import ffmpeg_ok

    if not ffmpeg_ok():
        pytest.skip("needs ffprobe")
    from gameday import media

    real = make_app(probe=media.probe)
    r = real.post("/api/upload?filename=random.mp4", content=os.urandom(5000),
                  headers={"Content-Type": "application/octet-stream"})
    assert r.status_code == 400
    assert list(real.config.uploads_dir.iterdir()) == []


# ---- settings and the Meta key -----------------------------------------------------


def test_settings_are_checked_and_kept(gd):
    assert gd.get("/api/settings").json() == {"mode": "basketball", "max_clips": 8, "hint": "", "title_cards": True}
    r = gd.patch("/api/settings", json={"mode": "talk", "max_clips": 12, "title_cards": False})
    assert r.json() == {"mode": "talk", "max_clips": 12, "hint": "", "title_cards": False}
    for bad in ({"mode": "soccer"}, {"max_clips": 0}, {"max_clips": 13}, {"max_clips": "3"}, {"max_clips": True},
                {"hint": "x" * 201}, {"title_cards": "yes"}, {"colour": "red"}):
        r = gd.patch("/api/settings", json=bad)
        assert r.status_code == 400, bad
        assert r.json()["error"]
    assert gd.get("/api/settings").json()["mode"] == "talk"


def test_the_meta_key_is_kept_encrypted_and_never_sent_back(gd):
    key = "meta-test-key-" + "k" * 30
    assert gd.put("/api/meta/key", json={"key": key}).json() == {"saved": True}
    assert gd.get("/api/status").json()["meta_key"] == {"saved": True, "from_env": False}
    for path in ("/api/status", "/api/settings", "/api/usage", "/api/events", "/remote-approval/status"):
        assert key not in gd.get(path).text
    raw = b"".join(p.read_bytes() for p in gd.config.data_dir.rglob("*") if p.is_file())
    assert key.encode() not in raw
    assert gd.store.secrets.get(secret_store.META_KEY) == key
    assert gd.put("/api/meta/key", json={"key": "has space in it"}).status_code == 400
    assert gd.put("/api/meta/key", json={}).status_code == 400
    assert gd.delete("/api/meta/key").json() == {"saved": False}
    assert gd.get("/api/status").json()["meta_key"] == {"saved": False, "from_env": False}


def test_the_environments_meta_key_wins(make_app):
    app = make_app(META_API_KEY="env-key-0123456789")
    app.put("/api/meta/key", json={"key": "saved-key-0123456789"})
    assert app.get("/api/status").json()["meta_key"] == {"saved": True, "from_env": True}
    assert secret_store.meta_key(app.config, app.store.secrets) == ("env-key-0123456789", True)


def test_testing_the_key_makes_one_cheap_call(make_app):
    meta = FakeMeta()
    app = make_app(meta_factory=lambda: meta)
    assert app.post("/api/meta/test").json() == {"ok": True}
    assert meta.calls == [("test_key",)]
    meta.bad_key = True
    assert app.post("/api/meta/test").json() == {"ok": False, "error": "Meta refused the key (401)."}
    assert make_app().post("/api/meta/test").json() == {"ok": False, "error": "No Meta key is saved."}


def test_secrets_are_fernet_tokens_under_the_key_file(tmp_path):
    config = make_config(tmp_path)
    config.ensure_dirs()
    store = Store(config.db_path, data_dir=config.data_dir)
    secrets = secret_store.attach(store, config)
    secrets.set("x", "plain value")
    assert (config.data_dir / "secret.key").is_file()
    assert "plain value" not in store.secret_token("x")
    # Another key can't read it: treated as not saved, never as garbage.
    other = secret_store.Secrets(store, secret_store.load_key(make_config(tmp_path, GAMEDAY_SECRET_KEY="another")))
    assert other.get("x") is None
    passphrase = make_config(tmp_path, GAMEDAY_SECRET_KEY="a passphrase")
    assert secret_store.load_key(passphrase) == secret_store.load_key(passphrase)
    store.close_all()


# ---- status, usage and events ---------------------------------------------------------


def test_status_usage_and_the_budget(make_app):
    app = make_app(META_BUDGET_USD="1.5")
    ledger = app.store.ledger()
    for model, purpose, cost, units in ((MODEL_VOICE, "transcribe", 0.07, {"audio_seconds": 1400}),
                                        (MODEL_SPARK, "moments", 0.02, {"prompt_tokens": 100}),
                                        (MODEL_SAM, "track", 0.3, {"frames": 1500})):
        ledger.record(ApiCall(model=model, endpoint="/x", purpose=purpose, ok=True, status=200, ms=10,
                              cost_usd=cost, units=units, job_id=9))
    status = app.get("/api/status").json()
    assert status["budget"] == {"limit_usd": 1.5, "spent_usd": 0.39}
    assert status["upload_limit_mb"] == 5 and status["watches"] == 0
    usage = app.get("/api/usage").json()
    assert usage["total_usd"] == 0.39 and usage["budget_usd"] == 1.5
    assert usage["by_model"][MODEL_SAM] == {"calls": 1, "usd": 0.3, "frames": 1500}
    assert usage["by_model"][MODEL_SPARK] == {"calls": 1, "usd": 0.02}
    assert [c["model"] for c in usage["recent"]] == [MODEL_SAM, MODEL_SPARK, MODEL_VOICE]
    ledger.check_budget(1.0)
    from gameday.types import BudgetExceeded

    with pytest.raises(BudgetExceeded):
        ledger.check_budget(1.2)


def test_events_come_oldest_first_after_an_id(gd):
    for i in range(3):
        gd.store.add_event("info", "job", f"event {i}", job_id=i)
    events = gd.get("/api/events?after=0&limit=100").json()
    assert [e["message"] for e in events][-3:] == ["event 0", "event 1", "event 2"]
    last = events[-2]["id"]
    assert [e["message"] for e in gd.get(f"/api/events?after={last}").json()] == ["event 2"]
    assert set(events[0]) == {"id", "at", "level", "kind", "message", "job_id"}


# ---- watched folders ------------------------------------------------------------------


def test_watched_folders_are_added_switched_off_and_removed(gd, tmp_path):
    folder = tmp_path / "drop"
    folder.mkdir()
    r = gd.post("/api/watches", json={"path": str(folder)})
    assert r.status_code == 200, r.text
    watch = r.json()
    assert watch == {"id": watch["id"], "path": str(folder.resolve()), "enabled": True, "last_checked_at": None,
                     "videos": 0, "error": ""}
    assert gd.post("/api/watches", json={"path": str(folder)}).status_code == 400  # already watched
    assert gd.patch(f"/api/watches/{watch['id']}", json={"enabled": False}).json()["enabled"] is False
    assert gd.patch(f"/api/watches/{watch['id']}", json={"enabled": "no"}).status_code == 400
    assert [w["id"] for w in gd.get("/api/watches").json()] == [watch["id"]]
    assert gd.get("/api/status").json()["watches"] == 1
    assert gd.delete(f"/api/watches/{watch['id']}").json() == {"deleted": True}
    assert gd.delete(f"/api/watches/{watch['id']}").status_code == 404
    assert gd.get("/api/watches").json() == []


@pytest.mark.parametrize("bad", ["missing", "a file", "inside data", "relative", "network", ""])
def test_a_folder_that_cannot_be_watched_is_refused(gd, tmp_path, bad):
    a_file = tmp_path / "game.mp4"
    a_file.write_bytes(b"x")
    (gd.config.data_dir / "drop").mkdir()
    path = {"missing": str(tmp_path / "missing"), "a file": str(a_file),
            "inside data": str(gd.config.data_dir / "drop"), "relative": "some/folder",
            "network": r"\\server\share\games", "": ""}[bad]
    r = gd.post("/api/watches", json={"path": path})
    assert r.status_code == 400 and r.json()["error"]
    assert gd.get("/api/watches").json() == []


def test_watch_dir_is_watched_from_the_first_start_only(make_app, tmp_path):
    folder = tmp_path / "games"
    folder.mkdir()
    app = make_app(WATCH_DIR=str(folder))
    [watch] = app.get("/api/watches").json()
    assert watch["path"] == str(folder.resolve())
    app.delete(f"/api/watches/{watch['id']}")
    again = make_app(config=app.config)  # the next start, same DATA_DIR
    assert again.get("/api/watches").json() == []


# ---- jobs -----------------------------------------------------------------------------


def _upload(app, name="game.mp4") -> dict:
    r = app.post(f"/api/upload?filename={name}", content=VIDEO_BYTES + os.urandom(1000),
                 headers={"Content-Type": "application/octet-stream"})
    assert r.status_code == 200, r.text
    return r.json()


def test_a_queued_job_can_be_cancelled_and_retried(gd):
    job_id = _upload(gd)["job_id"]
    r = gd.post(f"/api/jobs/{job_id}/cancel")
    assert r.status_code == 200 and r.json()["state"] == "cancelled"
    assert gd.post(f"/api/jobs/{job_id}/cancel").status_code == 409
    assert gd.worker.run_next_job() is None  # never started
    r = gd.post(f"/api/jobs/{job_id}/retry")
    assert r.status_code == 200
    new = r.json()
    assert new["id"] != job_id and new["state"] == "queued" and new["video"]["id"] == gd.store.get_job(job_id)[
        "video_id"]
    assert gd.post(f"/api/jobs/{new['id']}/retry").status_code == 409  # queued: nothing to retry
    assert [j["id"] for j in gd.get("/api/jobs").json()] == [new["id"], job_id]


def test_a_finished_job_lists_its_clips_with_media_urls(gd):
    job_id = _upload(gd)["job_id"]
    assert gd.worker.run_next_job() == job_id
    job = gd.get(f"/api/jobs/{job_id}").json()
    assert job["state"] == "done" and job["clips"] == 5 and job["stage"] is None
    assert job["finished_at"] >= job["started_at"] >= job["created_at"]
    assert [s["state"] for s in job["stages"].values()] == ["done", "done", "done", "done", "skipped"]
    assert job["warnings"] == ["Muse Voice was faked in this test."]
    clip = job["clips_list"][0]
    assert set(clip) == {"id", "job_id", "index", "title", "category", "label", "score", "team", "player",
                         "evidence", "start_s", "end_s", "follow", "video_url", "thumb_url", "debug_url"}
    assert clip["video_url"] == f"/media/jobs/{job_id}/clips/clip_01.mp4"
    assert clip["thumb_url"] == f"/media/jobs/{job_id}/clips/clip_01.jpg"
    assert clip["debug_url"] == f"/media/jobs/{job_id}/clips/clip_01_debug.jpg"
    assert clip["follow"] == {"source": "ball", "ball_coverage": 0.74, "note": "Followed the ball 74% of the time"}
    assert gd.get(clip["video_url"]).status_code == 200
    assert "clips_list" not in gd.get("/api/jobs").json()[0]
    assert [c["index"] for c in gd.get(f"/api/clips?job_id={job_id}&limit=50").json()] == [1, 2, 3, 4, 5]
    assert len(gd.get("/api/clips?limit=2").json()) == 2
    assert gd.post(f"/api/jobs/{job_id}/retry").status_code == 409  # done: only failed or cancelled


def test_a_job_left_running_by_a_restart_fails_and_can_be_retried(make_app):
    app = make_app()
    job_id = _upload(app)["job_id"]
    assert app.store.claim_next_job()["id"] == job_id
    app.store.update_job(job_id, stage="sam", stages={"voice": {"state": "done"}, "sam": {"state": "running"}})
    restarted = make_app(config=app.config)
    restarted.worker.recover()
    job = restarted.get(f"/api/jobs/{job_id}").json()
    assert job["state"] == "failed" and job["error"] == "The app restarted during this job"
    assert job["stages"]["sam"]["state"] == "failed"
    assert restarted.post(f"/api/jobs/{job_id}/retry").json()["state"] == "queued"
    assert any("was running when the app stopped" in e["message"] for e in restarted.get("/api/events").json())


def test_a_retry_needs_the_video_file(gd):
    job_id = _upload(gd)["job_id"]
    gd.post(f"/api/jobs/{job_id}/cancel")
    os.remove(gd.store.get_video(gd.store.get_job(job_id)["video_id"])["path"])
    r = gd.post(f"/api/jobs/{job_id}/retry")
    assert r.status_code == 409 and "no longer there" in r.json()["error"]


def test_status_shows_the_running_job(gd):
    job_id = _upload(gd)["job_id"]
    _upload(gd, "second.mp4")
    gd.store.claim_next_job()
    assert gd.get("/api/status").json()["worker"] == {"busy": True, "job_id": job_id, "queued": 1}
