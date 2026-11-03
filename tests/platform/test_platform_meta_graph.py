"""Instagram Reels and Facebook Page Reels through the relay
(publish/meta_graph.py), and one approved clip going out through the relay
loop's upload step (remote_approval/publishing.py, worker.publish_one).

Ported from this week's event branch (tests/test_meta_graph.py). No network:
the relay is a script of answers here. The relay's own side of these steps is
in meta-relay/test/relay.test.mjs, and both sides together in
test_platform_remote_approval_e2e.py.
"""

import json

import pytest
from platform_fakes import SECRET, FakeRelay, finished_job, sign_command, switch_on

from gameday.publish import meta_graph
from gameday.publish.errors import AuthRequired, PublishError, RateLimited, UploadLimitExceeded, VideoRejected
from gameday.remote_approval import commands, notifier, publishing, service, states, store
from gameday.remote_approval import settings as ra_settings
from gameday.remote_approval.relay import RelayError
from gameday.worker import Worker

CTX = {"command_id": "cmd_x", "approval_id": "apr_x", "clip_id": 7}


class ScriptedRelay:
    """meta(op) answers from `answers[op]`: a dict, a list of dicts taken in
    turn (the last one repeats), or a RelayError to raise."""

    def __init__(self, **answers):
        self.answers = answers
        self.calls = []
        self.uploads = []

    def meta(self, op, params):
        self.calls.append((op, params))
        got = self.answers.get(op, {})
        if isinstance(got, list):
            got = got.pop(0) if len(got) > 1 else got[0]
        if isinstance(got, Exception):
            raise got
        return got

    def upload_chunk(self, query, path, offset, length):
        self.uploads.append((query, str(path), offset, length))
        return {"ok": True}

    def ops(self):
        return [op for op, _ in self.calls]


@pytest.fixture
def clip_file(tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"\0" * 5000)
    return path


def graph(relay):
    return meta_graph.MetaGraph(relay, sleep=lambda s: None)


# ---- the steps, in Meta's order -------------------------------------------------------


def test_instagram_waits_for_the_container_then_publishes_and_reads_the_link(clip_file):
    relay = ScriptedRelay(
        ig_create={"container_id": "C1"},
        ig_status=[{"status_code": "IN_PROGRESS"}, {"status_code": "FINISHED"}],
        ig_publish={"media_id": "M1"},
        ig_permalink={"permalink": "https://www.instagram.com/reel/abc/"},
    )
    posted = graph(relay).instagram(CTX, clip_file, "Poster dunk! #hoops")
    assert relay.ops() == ["ig_create", "ig_status", "ig_status", "ig_publish", "ig_permalink"]
    assert posted.state == "published" and posted.post_id == "M1"
    assert posted.url == "https://www.instagram.com/reel/abc/"
    query, _, offset, length = relay.uploads[0]
    assert query["ref"] == "C1" and query["platform"] == "instagram" and query["command_id"] == "cmd_x"
    assert (offset, length) == (0, 5000)


def test_facebook_reel_start_upload_finish_and_poll(clip_file):
    relay = ScriptedRelay(
        fb_start={"video_id": "V1"},
        fb_finish={"success": True},
        fb_status=[{"video_status": "processing"},
                   {"video_status": "ready", "publish_status": "published", "post_id": "P1",
                    "permalink_url": "https://www.facebook.com/reel/V1"}],
    )
    posted = graph(relay).facebook(CTX, clip_file, "Poster dunk", "In the third quarter")
    assert relay.ops() == ["fb_start", "fb_finish", "fb_status", "fb_status"]
    assert posted.state == "published" and posted.post_id == "P1"
    assert posted.url == "https://www.facebook.com/reel/V1"


def test_a_video_still_processing_at_the_deadline_is_not_called_published(clip_file):
    clock = iter(range(0, 10_000, 100))
    relay = ScriptedRelay(ig_create={"container_id": "C1"}, ig_status={"status_code": "IN_PROGRESS"})
    g = meta_graph.MetaGraph(relay, sleep=lambda s: None, clock=lambda: next(clock))
    posted = g.instagram(CTX, clip_file, "x")
    assert posted.state == "processing" and posted.url == ""
    assert "ig_publish" not in relay.ops()


def test_a_container_error_is_a_rejected_video(clip_file):
    relay = ScriptedRelay(ig_create={"container_id": "C1"},
                          ig_status={"status_code": "ERROR", "status": "Unsupported codec"})
    with pytest.raises(VideoRejected, match="Unsupported codec"):
        graph(relay).instagram(CTX, clip_file, "x")


def test_a_publish_without_an_id_is_never_success(clip_file):
    relay = ScriptedRelay(ig_create={"container_id": "C1"}, ig_status={"status_code": "FINISHED"}, ig_publish={})
    with pytest.raises(PublishError, match="did not confirm"):
        graph(relay).instagram(CTX, clip_file, "x")


def test_a_link_is_only_what_the_api_returned(clip_file):
    relay = ScriptedRelay(ig_create={"container_id": "C1"}, ig_status={"status_code": "FINISHED"},
                          ig_publish={"media_id": "M1"}, ig_permalink={})
    posted = graph(relay).instagram(CTX, clip_file, "x")
    assert posted.state == "published" and posted.url == ""
    assert "did not return its link" in posted.warnings[0]


# ---- checked before a post is called failed ----------------------------------------------


def refused(platform="facebook", code=100, sub=33, status=422):
    return RelayError("graph_error", f"{platform.title()} refused the request.", status=status,
                      meta={"meta_code": code, "meta_subcode": sub, "platform": platform})


PUBLISHED_REEL = {"video_status": "ready", "publish_status": "published", "post_id": "P1",
                  "permalink_url": "https://www.facebook.com/reel/V1"}


def ticking(step):
    """A clock that moves `step` seconds each time it is read."""
    clock = iter(range(0, 100_000, step))
    return lambda: next(clock)


def test_a_facebook_status_refused_right_after_the_finish_is_asked_again(clip_file):
    # On November 1, 2026 a Reel went up while the finish or the first status
    # read after it was refused; which one is not known. This is the second.
    relay = ScriptedRelay(fb_start={"video_id": "V1"}, fb_finish={"success": True},
                          fb_status=[refused(), refused(), PUBLISHED_REEL])
    posted = graph(relay).facebook(CTX, clip_file, "t", "d")
    assert posted.state == "published" and posted.post_id == "P1"
    assert posted.url == "https://www.facebook.com/reel/V1"
    assert relay.ops() == ["fb_start", "fb_finish", "fb_status", "fb_status", "fb_status"]


def test_a_facebook_status_refused_until_the_deadline_is_not_confirmed_not_failed(clip_file):
    relay = ScriptedRelay(fb_start={"video_id": "V1"}, fb_finish={"success": True}, fb_status=refused())
    posted = meta_graph.MetaGraph(relay, sleep=lambda s: None, clock=ticking(100)).facebook(CTX, clip_file, "t", "d")
    assert posted.state == "processing" and posted.url == ""
    assert posted.warnings == ["Facebook took the video, but its status could not be read. "
                               "Check the Page before trying again."]


def test_a_reel_still_processing_at_the_deadline_says_so(clip_file):
    relay = ScriptedRelay(fb_start={"video_id": "V1"}, fb_finish={"success": True},
                          fb_status={"video_status": "processing"})
    posted = meta_graph.MetaGraph(relay, sleep=lambda s: None, clock=ticking(100)).facebook(CTX, clip_file, "t", "d")
    assert posted.state == "processing"
    assert posted.warnings == ["Facebook accepted the video and is still processing it. "
                               "Check the Page before trying again."]


@pytest.mark.parametrize("lost", [
    RelayError("network", "Your relay took too long to answer.", retryable=True),
    refused(code=1, sub=0, status=503),
])
def test_a_lost_answer_to_the_finish_is_watched_to_the_deadline_and_never_failed(clip_file, lost):
    # Published three minutes later: past the short check, inside the deadline.
    relay = ScriptedRelay(fb_start={"video_id": "V1"}, fb_finish=lost,
                          fb_status=[{"video_status": "processing"}] * 6 + [PUBLISHED_REEL])
    posted = meta_graph.MetaGraph(relay, sleep=lambda s: None, clock=ticking(30)).facebook(CTX, clip_file, "t", "d")
    assert posted.state == "published" and posted.post_id == "P1"
    # Never seen to go up: not confirmed, which Retry failed won't post again.
    relay = ScriptedRelay(fb_start={"video_id": "V1"}, fb_finish=lost, fb_status={"video_status": "processing"})
    posted = meta_graph.MetaGraph(relay, sleep=lambda s: None, clock=ticking(100)).facebook(CTX, clip_file, "t", "d")
    assert posted.state == "processing"
    assert "answer to the publish step was lost" in posted.warnings[0]


@pytest.mark.parametrize("lost", [
    RelayError("network", "Your relay took too long to answer.", retryable=True),
    refused(platform="instagram", code=1, sub=0, status=503),
])
def test_a_lost_answer_to_instagrams_publish_is_never_failed(clip_file, lost):
    # The container shows the Reel went up after all: published, with no link to show.
    relay = ScriptedRelay(ig_create={"container_id": "C1"}, ig_publish=lost,
                          ig_status=[{"status_code": "FINISHED"}, {"status_code": "FINISHED"},
                                     {"status_code": "PUBLISHED"}])
    posted = meta_graph.MetaGraph(relay, sleep=lambda s: None, clock=ticking(10)).instagram(CTX, clip_file, "x")
    assert posted.state == "published" and posted.url == "" and "lost" in posted.warnings[0]
    assert relay.ops().count("ig_publish") == 1
    # Never seen to go up: "processing", which Retry failed never posts again.
    relay = ScriptedRelay(ig_create={"container_id": "C1"}, ig_publish=lost, ig_status={"status_code": "FINISHED"})
    posted = meta_graph.MetaGraph(relay, sleep=lambda s: None, clock=ticking(30)).instagram(CTX, clip_file, "x")
    assert posted.state == "processing" and "Check the account" in posted.warnings[0]
    assert relay.ops().count("ig_publish") == 1


def test_an_instagram_publish_refused_or_slowed_down_is_still_an_error(clip_file):
    for answer, kind in ((refused(platform="instagram", code=4, sub=0, status=503), RateLimited),
                         (refused(platform="instagram", code=100, sub=33), PublishError)):
        relay = ScriptedRelay(ig_create={"container_id": "C1"}, ig_status={"status_code": "FINISHED"},
                              ig_publish=answer)
        with pytest.raises(kind):
            graph(relay).instagram(CTX, clip_file, "x")


def test_a_finish_asked_to_slow_down_is_a_refusal_not_a_lost_answer(clip_file):
    relay = ScriptedRelay(fb_start={"video_id": "V1"}, fb_finish=refused(code=4, sub=0, status=503),
                          fb_status={"video_status": "ready", "publish_status": "not_published"})
    with pytest.raises(RateLimited):
        meta_graph.MetaGraph(relay, sleep=lambda s: None, clock=ticking(30)).facebook(CTX, clip_file, "t", "d")
    assert relay.ops().count("fb_status") <= meta_graph.FINISH_CHECK // 30 + 1


def test_a_facebook_finish_answered_with_an_error_is_checked_before_it_is_called_failed(clip_file):
    relay = ScriptedRelay(fb_start={"video_id": "V1"}, fb_finish=refused(code=1, sub=0),
                          fb_status=[{"video_status": "processing"}, PUBLISHED_REEL])
    posted = graph(relay).facebook(CTX, clip_file, "t", "d")
    assert posted.state == "published" and posted.post_id == "P1"


def test_a_facebook_finish_error_stands_when_the_reel_never_goes_up(clip_file):
    relay = ScriptedRelay(fb_start={"video_id": "V1"}, fb_finish=refused(),
                          fb_status={"video_status": "ready", "publish_status": "not_published"})
    g = meta_graph.MetaGraph(relay, sleep=lambda s: None, clock=ticking(30))
    with pytest.raises(PublishError, match=r"^Facebook refused the request \(Meta error 100, subcode 33\)\.$"):
        g.facebook(CTX, clip_file, "t", "d")
    # Watched for the short check only, not the 10-minute deadline.
    assert 1 <= relay.ops().count("fb_status") <= meta_graph.FINISH_CHECK // 30 + 1


@pytest.mark.parametrize("code, sub, kind", [(200, 0, AuthRequired), (9, 2207042, UploadLimitExceeded),
                                             (352, 0, VideoRejected)])
def test_a_finish_refusal_that_says_nothing_was_taken_is_believed_at_once(clip_file, code, sub, kind):
    relay = ScriptedRelay(fb_start={"video_id": "V1"}, fb_finish=refused(code=code, sub=sub),
                          fb_status=PUBLISHED_REEL)
    with pytest.raises(kind):
        graph(relay).facebook(CTX, clip_file, "t", "d")
    assert "fb_status" not in relay.ops()


def test_a_post_the_api_confirmed_is_never_failed_by_reading_its_link(clip_file):
    relay = ScriptedRelay(ig_create={"container_id": "C1"}, ig_status={"status_code": "FINISHED"},
                          ig_publish={"media_id": "M1"}, ig_permalink=refused("instagram", 2, 0))
    posted = graph(relay).instagram(CTX, clip_file, "x")
    assert posted.state == "published" and posted.post_id == "M1" and posted.url == ""
    assert "did not return its link" in posted.warnings[0]


def test_an_unknown_refusal_keeps_metas_numbers_but_not_its_text():
    err = meta_graph.classify("graph_error", "Facebook refused the request.",
                              {"meta_code": 100, "meta_subcode": 33, "platform": "facebook"})
    assert err.message == "Facebook refused the request (Meta error 100, subcode 33)."
    assert meta_graph.classify("graph_error", "Facebook refused the request.",
                               {"meta_code": 1}).message == "Facebook refused the request (Meta error 1)."
    assert meta_graph.classify("graph_error", "Facebook refused the request.",
                               {}).message == "Facebook refused the request."


def test_files_too_big_for_the_relay_or_empty_are_refused_before_any_call(tmp_path, monkeypatch):
    small = tmp_path / "empty.mp4"
    small.write_bytes(b"")
    relay = ScriptedRelay()
    with pytest.raises(PublishError, match="empty"):
        graph(relay).instagram(CTX, small, "x")
    monkeypatch.setattr(meta_graph, "MAX_UPLOAD_BYTES", 2000)
    big = tmp_path / "big.mp4"
    big.write_bytes(b"\0" * 3000)
    with pytest.raises(PublishError, match="Export it smaller"):
        graph(relay).facebook(CTX, big, "t", "d")
    assert relay.calls == []


# ---- refusals a creator can act on ----------------------------------------------------------


@pytest.mark.parametrize("code, meta, kind, words", [
    ("graph_error", {"meta_code": 190, "platform": "instagram"}, AuthRequired, "Reconnect"),
    ("graph_error", {"meta_code": 200, "platform": "facebook"}, AuthRequired, "allow publishing"),
    ("graph_error", {"meta_code": 4}, RateLimited, "slow down"),
    ("graph_error", {"meta_code": 9, "meta_subcode": 2207042}, UploadLimitExceeded, "daily publishing limit"),
    ("graph_error", {"meta_subcode": 2207026}, VideoRejected, "format"),
    ("graph_error", {"meta_code": 368}, PublishError, "limiting this account"),
    ("not_approved", {}, PublishError, "That step was not approved"),
])
def test_relay_refusals_become_errors_with_a_next_step(code, meta, kind, words):
    err = meta_graph.classify(code, "That step was not approved.", meta)
    assert isinstance(err, kind)
    assert words in err.message


def test_a_relay_refusal_mid_publish_is_raised_as_that_error(clip_file):
    relay = ScriptedRelay(ig_create=RelayError("graph_error", "Instagram refused.", status=400,
                                               meta={"meta_code": 190, "platform": "instagram"}))
    with pytest.raises(AuthRequired):
        graph(relay).instagram(CTX, clip_file, "x")


def test_captions_are_held_to_instagrams_limits():
    many = " ".join(f"#tag{i}" for i in range(40))
    fitted = meta_graph.fit_caption("Poster dunk " + many)
    assert fitted.count("#") == 30 and fitted.startswith("Poster dunk")
    assert len(meta_graph.fit_caption("x" * 5000)) == 2200


# ---- one approved clip through the upload step -----------------------------------------


class FakeGraph:
    """Stands in for MetaGraph inside publishing.run. `fail` maps a platform
    to the PublishError it raises."""

    def __init__(self):
        self.calls = []
        self.fail = {}

    def _go(self, platform, *args):
        self.calls.append((platform, *args))
        if platform in self.fail:
            raise self.fail[platform]
        urls = {"instagram": "https://www.instagram.com/reel/R1/",
                "facebook": "https://www.facebook.com/reel/F1"}
        return meta_graph.Posted(platform, post_id=f"{platform}-1", url=urls[platform])

    def instagram(self, ctx, path, caption, **kw):
        return self._go("instagram", ctx, str(path), caption)

    def facebook(self, ctx, path, title, description, **kw):
        return self._go("facebook", ctx, str(path), title, description)


@pytest.fixture
def approved(db, config, monkeypatch):
    """An offer approved from the phone for one clip on both platforms, its
    upload queued, and a worker (threads never started) to run it."""
    switch_on(db)
    job = finished_job(db, config.data_dir)
    review_id = notifier.on_job_finished(db, job, "done")
    service.flush_outbox(db, FakeRelay())
    review = store.get_review(db, review_id)
    clip_id = review["clips"][0]["clip_id"]
    out = commands.handle(db, sign_command({
        "id": "cmd_" + "d" * 24, "kind": "approve", "approval_id": review["approval_id"],
        "review_ref": review["id"], "fingerprint": review["fingerprint"], "clips": [clip_id],
        "platforms": ["instagram", "facebook"], "decided_at": store.stamp(),
    }), secret=SECRET)
    assert out.outcome == "accepted"
    fake = FakeGraph()
    monkeypatch.setattr(publishing, "make_graph", lambda config, device: fake)
    worker = Worker(db, config, pipeline=lambda *a: None, meta_factory=lambda: None)
    return {"review_id": review_id, "clip_id": clip_id, "graph": fake, "worker": worker}


def _run(db, approved):
    job = db.conn.execute("SELECT id FROM publish_jobs WHERE status = 'queued' ORDER BY id").fetchone()
    assert approved["worker"].publish_one()
    return db.get_publish_job(job["id"])


def _posts(db, clip_id):
    return {r["platform"]: r for r in db.clip_publishes(clip_id) if r["provider"] == "meta"}


def test_an_approved_clip_goes_to_every_platform_chosen_in_order(db, approved):
    job = _run(db, approved)
    assert job["status"] == "done"
    assert [c[0] for c in approved["graph"].calls] == ["instagram", "facebook"]
    posts = _posts(db, approved["clip_id"])
    assert {p: r["state"] for p, r in posts.items()} == {"instagram": "published", "facebook": "published"}
    assert posts["facebook"]["post_url"] == "https://www.facebook.com/reel/F1"
    review = store.get_review(db, approved["review_id"])
    assert posts["instagram"]["request_id"] == review["approval_id"]
    service.settle_publishing(db, ra_settings.load(db))
    assert store.get_review(db, approved["review_id"])["state"] == states.PUBLISHED


def test_the_rendered_file_is_uploaded_as_it_is(db, approved):
    _run(db, approved)
    review = store.get_review(db, approved["review_id"])
    assert approved["graph"].calls[0][2] == review["clips"][0]["path"]


def test_one_platform_failing_does_not_stop_the_others(db, approved):
    approved["graph"].fail["instagram"] = AuthRequired("Your Instagram connection has expired.")
    job = _run(db, approved)
    assert job["status"] == "done"
    posts = _posts(db, approved["clip_id"])
    assert posts["instagram"]["state"] == "failed" and "expired" in posts["instagram"]["error"]
    assert posts["facebook"]["state"] == "published"
    service.settle_publishing(db, ra_settings.load(db))
    review = store.get_review(db, approved["review_id"])
    assert review["state"] == states.PARTIAL_FAILURE
    assert service.result_text(review["results"]) == "1 of 2 posts published (1 failed)"


def test_a_clip_that_went_up_nowhere_fails_its_job_and_the_offer(db, approved):
    approved["graph"].fail["instagram"] = VideoRejected("Instagram could not use this video's format.")
    approved["graph"].fail["facebook"] = VideoRejected("Facebook could not use this video's format.")
    job = _run(db, approved)
    assert job["status"] == "failed"
    posts = _posts(db, approved["clip_id"])
    assert {p: r["state"] for p, r in posts.items()} == {"instagram": "failed", "facebook": "failed"}
    assert [c[0] for c in approved["graph"].calls] == ["instagram", "facebook"]
    service.settle_publishing(db, ra_settings.load(db))
    assert store.get_review(db, approved["review_id"])["state"] == states.FAILED


def test_a_retry_never_posts_again_where_it_already_went_up(db, approved):
    approved["graph"].fail["facebook"] = RateLimited("Facebook asked us to slow down.")
    _run(db, approved)
    service.settle_publishing(db, ra_settings.load(db))
    assert service.retry_failed(db, approved["review_id"]) == 1
    approved["graph"].fail.clear()
    approved["graph"].calls.clear()
    job = _run(db, approved)
    assert job["status"] == "done"
    assert [c[0] for c in approved["graph"].calls] == ["facebook"]
    service.settle_publishing(db, ra_settings.load(db))
    assert store.get_review(db, approved["review_id"])["state"] == states.PUBLISHED


def test_a_clip_changed_after_approval_is_not_uploaded(db, approved):
    review = store.get_review(db, approved["review_id"])
    with open(review["clips"][0]["path"], "ab") as f:
        f.write(b"edited")
    job = _run(db, approved)
    assert job["status"] == "failed" and "changed after it was approved" in job["error"]
    assert approved["graph"].calls == []


def test_a_job_whose_approval_was_cancelled_is_not_uploaded(db, approved):
    db.conn.execute("UPDATE remote_reviews SET state = ? WHERE id = ?", (states.CANCELLED, approved["review_id"]))
    db.conn.commit()
    job = _run(db, approved)
    assert job["status"] == "failed" and "no longer approved" in job["error"]
    assert approved["graph"].calls == []


def test_a_forged_job_without_the_approving_command_is_not_uploaded(db, approved):
    row = db.conn.execute("SELECT * FROM publish_jobs WHERE status = 'queued'").fetchone()
    request = json.loads(row["request"])
    request["command_id"] = "cmd_forged"
    db.conn.execute("UPDATE publish_jobs SET request = ? WHERE id = ?", (json.dumps(request), row["id"]))
    db.conn.commit()
    job = _run(db, approved)
    assert job["status"] == "failed" and "does not match the approval" in job["error"]
    assert approved["graph"].calls == []
