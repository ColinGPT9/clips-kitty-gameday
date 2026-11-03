"""The worker (gameday/worker.py) with a fake engine and a fake MetaAPI: a
job's stages, the captions stage that makes the phone offer, cancelling,
the folder watcher, the relay loop's one upload a pass, and restarts."""

from __future__ import annotations

import json
import os
import threading
import time

import pytest
from conftest import VIDEO_BYTES
from platform_fakes import PLAYS, FakeMeta, FakePipeline, FakeRelay, broken_pipeline, sign_command, switch_on

from gameday.remote_approval import commands, service, states, store
from gameday.types import MODEL_SPARK, PipelineOptions
from gameday.watch import folder as folder_mod
from gameday.watch.folder import FolderFeed


def upload(app, name="game.mp4", body=None) -> int:
    r = app.post(f"/api/upload?filename={name}", content=body or VIDEO_BYTES + os.urandom(1000),
                 headers={"Content-Type": "application/octet-stream"})
    assert r.status_code == 200, r.text
    return r.json()["job_id"]


def test_connections_of_finished_pool_threads_do_not_pile_up(tmp_path):
    """Muse Voice and SAM run in a new pool each job, and its threads use the ledger."""
    from concurrent.futures import ThreadPoolExecutor

    from gameday.store import Store

    db = Store(tmp_path / "pool.db")
    for _ in range(5):  # five jobs
        with ThreadPoolExecutor(3) as pool:
            list(pool.map(lambda _: db.get_flag("x", ""), range(9)))
    db.get_flag("x", "")  # the next connection opened closes the ones left by dead threads
    assert len(db._all) == 1
    db.close_all()


# ---- one job ------------------------------------------------------------------------


def test_a_job_runs_in_its_own_folder_and_its_stages_are_timed(make_app):
    pipeline = FakePipeline(spark_call=True)
    app = make_app(pipeline=pipeline)
    meta = FakeMeta(app.store.ledger())
    app.worker.meta_factory = lambda: meta
    app.patch("/api/settings", json={"mode": "basketball", "max_clips": 3, "hint": "Blue", "title_cards": False})
    job_id = upload(app)
    assert app.worker.run_next_job() == job_id
    [call] = pipeline.calls
    assert call["work_dir"] == app.config.data_dir / "media" / "jobs" / str(job_id)
    assert call["options"] == PipelineOptions(mode="basketball", max_clips=3, hint="Blue", title_cards=False)
    assert call["meta"] is meta
    assert call["source"].read_bytes().startswith(VIDEO_BYTES)  # the upload, read where it is
    job = app.get(f"/api/jobs/{job_id}").json()
    assert job["state"] == "done" and job["message"] == "5 clips ready"
    assert {s: v["state"] for s, v in job["stages"].items()} == {
        "voice": "done", "spark": "done", "sam": "done", "render": "done", "captions": "skipped"}
    assert job["stages"]["spark"]["cost_usd"] == 0.002 and job["cost_usd"] == 0.002
    assert job["stages"]["captions"]["note"].startswith("Nothing was offered on your phone")
    assert all(v["seconds"] >= 0 for v in job["stages"].values())
    assert [c["category"] for c in job["clips_list"]] == [p[0] for p in PLAYS]


def test_a_thumbnail_that_failed_to_render_is_not_offered_as_a_poster(make_app):
    class NoFirstThumb(FakePipeline):
        def __call__(self, *args, **kwargs):
            result = super().__call__(*args, **kwargs)
            result.clips[0].thumb.unlink()  # both ffmpeg tries failed: the path, but no file
            return result

    app = make_app(pipeline=NoFirstThumb())
    job_id = upload(app)
    app.worker.run_next_job()
    clips = sorted(app.get(f"/api/clips?job_id={job_id}").json(), key=lambda c: c["index"])
    assert clips[0]["thumb_url"] is None and clips[1]["thumb_url"].endswith("/clip_02.jpg")


def test_a_video_the_engine_cant_process_fails_the_job_with_its_words(make_app):
    app = make_app(pipeline=broken_pipeline)
    job_id = upload(app)
    app.worker.run_next_job()
    job = app.get(f"/api/jobs/{job_id}").json()
    assert job["state"] == "failed" and job["error"] == "The video has no audio stream."
    assert job["stage"] == "voice" and job["stages"]["voice"]["state"] == "failed"
    assert job["clips"] == 0
    assert any(e["level"] == "error" and "no audio stream" in e["message"] for e in app.get("/api/events").json())


def test_an_engine_crash_fails_the_job_without_its_traceback(make_app):
    app = make_app(pipeline=FakePipeline(fail=KeyError("C:/Users/someone/secret")))
    job_id = upload(app)
    app.worker.run_next_job()
    job = app.get(f"/api/jobs/{job_id}").json()
    assert job["state"] == "failed" and job["error"] == "Something went wrong while clipping (KeyError)."
    assert "secret" not in json.dumps(job)


def test_without_the_engine_installed_a_job_fails_plainly(make_app, monkeypatch):
    from gameday import worker as worker_mod

    def missing():
        raise ImportError("no engine")

    app = make_app()
    app.worker._pipeline = None
    monkeypatch.setattr(worker_mod, "default_pipeline", missing)
    job_id = upload(app)
    app.worker.run_next_job()
    assert app.store.get_job(job_id)["state"] == "failed"


def test_a_running_job_stops_at_the_next_safe_point_when_cancelled(make_app):
    hold = threading.Event()
    pipeline = FakePipeline(hold=hold)
    app = make_app(pipeline=pipeline)
    job_id = upload(app)
    runner = threading.Thread(target=app.worker.run_next_job)
    runner.start()
    try:
        assert pipeline.started.wait(5)
        deadline = time.time() + 5
        while app.store.get_job(job_id)["stage"] != "sam" and time.time() < deadline:
            time.sleep(0.02)
        r = app.post(f"/api/jobs/{job_id}/cancel")
        assert r.status_code == 200 and r.json()["state"] == "running" and r.json()["message"] == "Stopping"
        runner.join(10)
    finally:
        hold.set()
        runner.join(10)
    job = app.get(f"/api/jobs/{job_id}").json()
    assert job["state"] == "cancelled" and job["stages"]["sam"]["state"] == "skipped"
    assert job["stages"]["render"]["state"] == "waiting"
    assert app.post(f"/api/jobs/{job_id}/retry").json()["state"] == "queued"


# ---- the captions stage: the offer on the phone -------------------------------------------


def test_the_captions_stage_makes_the_offer_with_muse_spark_captions(make_app, monkeypatch):
    app = make_app()
    meta = FakeMeta(app.store.ledger())
    app.worker.meta_factory = lambda: meta
    switch_on(app.store)
    job_id = upload(app)
    app.worker.run_next_job()
    job = app.get(f"/api/jobs/{job_id}").json()
    captions = job["stages"]["captions"]
    assert captions["state"] == "done"
    assert captions["note"] == "Muse Spark wrote the captions. Offered 5 clips on your phone."
    assert captions["cost_usd"] == 0.002 and job["stages"]["spark"]["cost_usd"] == 0
    [review] = store.reviews_for_job(app.store, job_id)
    assert review["state"] == states.NOTIFICATION_PENDING and review["title"] == "game"
    assert review["captions"]["model"] == MODEL_SPARK and len(review["captions"]["clips"]) == 5
    [(_, purpose, called_for, _messages)] = [c for c in meta.calls if c[0] == "chat_json"]
    assert (purpose, called_for) == ("captions", job_id)
    # The relay loop sends it with Muse Spark's words, and no local path.
    relay = FakeRelay()
    monkeypatch.setattr(service, "client", lambda db: relay)
    app.worker.relay_step()
    [body] = relay.notified
    assert body["clips"][0]["text_source"] == "muse_spark" and body["clips"][0]["title"].startswith("Muse:")
    assert body["clips"][0]["thumb"]  # the engine's thumbnail, made small
    assert str(app.config.data_dir) not in json.dumps(body)
    assert store.get_review(app.store, review["id"])["state"] == states.NOTIFICATION_SENT


def test_without_a_meta_key_the_offer_goes_with_the_clips_own_titles(make_app):
    app = make_app()
    switch_on(app.store)
    job_id = upload(app)
    app.worker.run_next_job()
    note = app.get(f"/api/jobs/{job_id}").json()["stages"]["captions"]["note"]
    assert note == "Offered 5 clips on your phone. No Meta key is saved, so the clips' own titles are the captions."
    [review] = store.reviews_for_job(app.store, job_id)
    assert review["captions"] == {}


def test_a_muse_spark_failure_never_stops_the_offer(make_app):
    from gameday.types import MetaError

    app = make_app()
    app.worker.meta_factory = lambda: FakeMeta(fail=MetaError("Muse Spark answered 503.", status=503))
    switch_on(app.store)
    job_id = upload(app)
    app.worker.run_next_job()
    job = app.get(f"/api/jobs/{job_id}").json()
    assert job["state"] == "done" and job["stages"]["captions"]["state"] == "done"
    assert "Muse Spark couldn't write the captions (Muse Spark answered 503.)" in job["stages"]["captions"]["note"]
    assert store.reviews_for_job(app.store, job_id)[0]["state"] == states.NOTIFICATION_PENDING


def test_the_captions_stage_says_why_nothing_was_offered(make_app):
    app = make_app()
    job_id = upload(app)
    app.worker.run_next_job()
    note = app.get(f"/api/jobs/{job_id}").json()["stages"]["captions"]["note"]
    assert note == "Nothing was offered on your phone. In the dashboard's Setup, make a Muse key."
    # On, but this app was never registered with a relay.
    from gameday.remote_approval import settings as ra_settings

    ra_settings.save(app.store, {"whatsapp_enabled": True})
    job_id = upload(app, "second.mp4")
    app.worker.run_next_job()
    note = app.get(f"/api/jobs/{job_id}").json()["stages"]["captions"]["note"]
    assert note == "Connect this app to your relay to get its clips on your phone."
    assert store.recent_reviews(app.store) == []


def test_talk_videos_are_offered_only_once_the_owner_opts_in(make_app):
    from gameday.remote_approval import settings as ra_settings

    app = make_app()
    switch_on(app.store)
    app.patch("/api/settings", json={"mode": "talk"})
    job_id = upload(app)
    app.worker.run_next_job()
    stage = app.get(f"/api/jobs/{job_id}").json()["stages"]["captions"]
    assert stage["state"] == "skipped" and stage["note"].startswith("Only basketball games are offered")
    assert "Offer other videos too" in stage["note"]
    # What the dashboard's "Offer other videos too" box sends.
    assert app.patch("/remote-approval/settings", json={"basketball_only": False}).status_code == 200
    assert ra_settings.load(app.store)["basketball_only"] is False
    job_id = upload(app, "talk.mp4")
    app.worker.run_next_job()
    [review] = store.reviews_for_job(app.store, job_id)
    assert review["summary"]["kind"] == "video"


def test_the_notes_name_switches_the_dashboard_really_has():
    from pathlib import Path

    from gameday.remote_approval import notifier

    page = (Path(notifier.__file__).parents[1] / "static" / "index.html").read_text(encoding="utf-8")
    off = {"whatsapp_enabled": False, "notify_on_finish": True, "muse": {}}
    notes = [notifier.why_not_offered(off), notifier.why_not_offered({**off, "whatsapp_enabled": True,
                                                                        "notify_on_finish": False})]
    labels = ["Setup", "Make a Muse key", "Offer other videos too", "Publishing"]
    for label in labels:
        assert label in page, label
    assert all(label.lower() in notes[0].lower() for label in labels[:2])
    # The dashboard has no switch for notify_on_finish any more: the note
    # names the setting and where it is changed instead.
    assert "notify_on_finish" in notes[1] and "PATCH /remote-approval/settings" in notes[1]
    # WhatsApp stays in the code, off, but the notes never send the owner to it.
    assert not any("whatsapp" in note.lower() for note in notes)
    assert "whatsapp" not in page.lower()


def test_a_failed_job_queues_the_plain_failure_message(make_app):
    app = make_app(pipeline=broken_pipeline)
    switch_on(app.store)
    job_id = upload(app)
    app.worker.run_next_job()
    [item] = store.due_outbox(app.store)
    assert item["kind"] == "failed" and item["body"] == {"job_id": job_id}


# ---- the relay loop and uploads ------------------------------------------------------------


class _Graph:
    def __init__(self):
        self.calls = []

    def instagram(self, ctx, path, caption, **kw):
        from gameday.publish.meta_graph import Posted

        self.calls.append(("instagram", str(path)))
        return Posted("instagram", post_id="M1", url="https://www.instagram.com/reel/R1/")

    def facebook(self, ctx, path, title, description, **kw):
        from gameday.publish.meta_graph import Posted

        self.calls.append(("facebook", str(path)))
        return Posted("facebook", post_id="F1", url="https://www.facebook.com/reel/F1")


def test_the_relay_loop_takes_an_approval_and_runs_one_upload_a_pass(make_app, monkeypatch):
    from gameday.remote_approval import publishing

    app = make_app()
    switch_on(app.store)
    relay, graph = FakeRelay(), _Graph()
    monkeypatch.setattr(service, "client", lambda db: relay)
    monkeypatch.setattr(publishing, "make_graph", lambda config, device: graph)
    job_id = upload(app)
    app.worker.run_next_job()
    app.worker.relay_step()
    review = store.reviews_for_job(app.store, job_id)[0]
    assert review["state"] == states.NOTIFICATION_SENT
    first, second = (c["clip_id"] for c in review["clips"][:2])
    relay.pending.append(sign_command({
        "id": "cmd_" + "a" * 24, "kind": "approve", "approval_id": review["approval_id"], "review_ref": review["id"],
        "fingerprint": review["fingerprint"], "clips": [first, second], "platforms": ["instagram"],
        "decided_at": store.stamp()}))
    app.worker.wake_relay()
    assert app.worker.relay_step() is True  # the approval queued two uploads; this pass ran one
    assert [j["status"] for j in commands.jobs_for_review(app.store, review["id"])] == ["done", "queued"]
    assert app.worker.relay_step() is True
    assert app.worker.relay_step() is False
    app.worker.wake_relay()
    app.worker.relay_step()
    assert store.get_review(app.store, review["id"])["state"] == states.PUBLISHED
    assert len(graph.calls) == 2
    events = [e["message"] for e in app.get("/api/events").json()]
    assert any("Published to Instagram Reels" in m for m in events)


def test_an_upload_a_restart_cut_off_is_never_sent_again(make_app, monkeypatch):
    app = make_app()
    switch_on(app.store)
    job = app.store.add_publish_job(1, json.dumps({"provider": "meta"}))
    assert app.store.claim_next_publish_job()["id"] == job
    restarted = make_app(config=app.config)
    restarted.worker.recover()
    assert restarted.store.get_publish_job(job)["status"] == "interrupted"
    assert restarted.worker.publish_one() is False
    assert any("not sent again by itself" in e["message"] for e in restarted.get("/api/events").json())


def test_an_upload_that_is_not_an_approved_one_is_refused(make_app):
    app = make_app()
    job = app.store.add_publish_job(1, json.dumps({"provider": "somewhere"}))
    assert app.worker.publish_one() is True
    row = app.store.get_publish_job(job)
    assert row["status"] == "failed" and "not one this app knows" in row["error"]


# ---- the folder watcher ------------------------------------------------------------------


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def watched(make_app, tmp_path):
    clock = Clock()
    folder = tmp_path / "drop"
    folder.mkdir()
    app = make_app(feed=FolderFeed(clock=clock))
    watch = app.post("/api/watches", json={"path": str(folder)}).json()
    return app, folder, clock, watch


def test_a_new_file_gets_one_job_once_it_has_stopped_changing(watched):
    app, folder, clock, watch = watched
    game = folder / "game.mp4"
    game.write_bytes(VIDEO_BYTES + b"first half")
    assert app.worker.watcher.pass_once() == 0
    clock.now += folder_mod.SETTLE_SECONDS - 1
    game.write_bytes(VIDEO_BYTES + b"first half, second half")  # still arriving
    assert app.worker.watcher.pass_once() == 0
    clock.now += folder_mod.SETTLE_SECONDS
    assert app.worker.watcher.pass_once() == 1
    clock.now += 600
    assert app.worker.watcher.pass_once() == 0  # one job per file
    [job] = app.get("/api/jobs").json()
    assert job["video"]["name"] == "game.mp4" and job["video"]["source"] == "watch"
    [w] = app.get("/api/watches").json()
    assert w["videos"] == 1 and w["last_checked_at"] == pytest.approx(time.time(), abs=60) and w["error"] == ""


def test_watched_files_are_only_read(watched):
    app, folder, clock, _ = watched
    game = folder / "game.mp4"
    game.write_bytes(VIDEO_BYTES + os.urandom(5000))
    before = (game.read_bytes(), game.stat().st_mtime_ns)
    app.worker.watcher.pass_once()
    clock.now += folder_mod.SETTLE_SECONDS
    app.worker.watcher.pass_once()
    job_id = app.worker.run_next_job()
    assert app.store.get_job(job_id)["state"] == "done"
    assert app.worker._pipeline.calls[0]["source"] == game.resolve()
    assert (game.read_bytes(), game.stat().st_mtime_ns) == before
    assert sorted(p.name for p in folder.iterdir()) == ["game.mp4"]  # nothing moved, added or removed
    assert list(app.config.uploads_dir.iterdir()) == []  # and not copied


def test_a_file_changed_after_its_job_is_a_new_video(watched):
    app, folder, clock, _ = watched
    game = folder / "game.mp4"
    game.write_bytes(VIDEO_BYTES + b"take one")
    app.worker.watcher.pass_once()
    clock.now += folder_mod.SETTLE_SECONDS
    assert app.worker.watcher.pass_once() == 1
    game.write_bytes(VIDEO_BYTES + b"take two, which is longer")
    app.worker.watcher.pass_once()
    clock.now += folder_mod.SETTLE_SECONDS
    assert app.worker.watcher.pass_once() == 1
    assert len(app.get("/api/jobs").json()) == 2


def test_a_file_that_is_not_a_video_is_skipped_and_said_once(watched):
    app, folder, clock, _ = watched
    (folder / "notes.mp4").write_bytes(b"not a video at all")
    app.worker.watcher.pass_once()
    clock.now += folder_mod.SETTLE_SECONDS
    for _ in range(3):
        assert app.worker.watcher.pass_once() == 0
        clock.now += 10
    skipped = [e for e in app.get("/api/events").json() if e["message"].startswith("Skipped notes.mp4")]
    assert len(skipped) == 1 and skipped[0]["message"] == "Skipped notes.mp4: notes.mp4 has no video stream"
    assert app.get("/api/jobs").json() == []


def test_a_switched_off_watch_is_not_looked_at_and_a_lost_folder_says_so(watched):
    app, folder, clock, watch = watched
    (folder / "game.mp4").write_bytes(VIDEO_BYTES + b"x")
    app.patch(f"/api/watches/{watch['id']}", json={"enabled": False})
    app.worker.watcher.pass_once()
    clock.now += folder_mod.SETTLE_SECONDS
    assert app.worker.watcher.pass_once() == 0
    app.patch(f"/api/watches/{watch['id']}", json={"enabled": True})
    (folder / "game.mp4").unlink()
    folder.rmdir()
    for _ in range(2):
        app.worker.watcher.pass_once()
    [w] = app.get("/api/watches").json()
    assert w["error"] == "The folder isn't there or can't be opened."
    assert len([e for e in app.get("/api/events").json() if e["message"].startswith("Couldn't check")]) == 1


# ---- the threads ----------------------------------------------------------------------------


def test_the_threads_start_and_stop_with_the_app(tmp_path):
    from conftest import ACTION, AUTH, fake_probe, make_config
    from fastapi.testclient import TestClient

    from gameday.app import create_app

    app = create_app(make_config(tmp_path), pipeline=FakePipeline(), meta_factory=lambda: None, probe=fake_probe)
    with TestClient(app, base_url="http://127.0.0.1", headers=AUTH) as client:
        worker = app.state.worker
        assert worker.running
        r = client.post("/api/upload?filename=game.mp4", content=VIDEO_BYTES + b"x" * 100,
                        headers={**ACTION, "Content-Type": "application/octet-stream"})
        job_id = r.json()["job_id"]
        deadline = time.time() + 15
        while client.get(f"/api/jobs/{job_id}").json()["state"] != "done" and time.time() < deadline:
            time.sleep(0.05)
        assert client.get(f"/api/jobs/{job_id}").json()["state"] == "done"
    assert not worker.running
