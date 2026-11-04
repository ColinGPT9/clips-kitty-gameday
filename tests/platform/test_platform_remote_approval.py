"""Remote approval in the app: offers, the state machine, decisions from the
phone, and the loop that talks to the relay (gameday/remote_approval/).

Ported from this week's event branch (tests/test_remote_approval.py) onto the
new store. The relay is faked here (platform_fakes.FakeRelay);
test_platform_remote_approval_e2e.py runs the real one. Tests of what this app
no longer has (gaming labels, other publishers, the desktop's run detection
by file times, Longform cuts) were left out.
"""

import json
from datetime import timedelta

import pytest
from platform_fakes import SECRET, FakeMeta, FakeRelay, SaysLine, finished_job, sign_command, switch_on

from gameday import access
from gameday.remote_approval import captions, commands, notifier, offer_line, service, states, store, summary, usage
from gameday.remote_approval import settings as ra_settings
from gameday.remote_approval.relay import RelayError
from gameday.types import MetaError


@pytest.fixture
def on(db, data_dir):
    """WhatsApp and remote approval switched on, the app registered, every
    platform connected."""
    switch_on(db)
    return data_dir


def offered(db, data_dir, relay=None, **kw):
    """A finished job, its offer made and sent: (review, relay)."""
    job = finished_job(db, data_dir, **kw)
    review_id = notifier.on_job_finished(db, job, "done")
    relay = relay or FakeRelay()
    service.flush_outbox(db, relay)
    return store.get_review(db, review_id), relay


def approve_command(review, *, cid="cmd_" + "a" * 24, clips=None, platforms=("instagram",), decided=None, **extra):
    return sign_command({
        "id": cid, "kind": "approve", "approval_id": review["approval_id"], "review_ref": review["id"],
        "fingerprint": review["fingerprint"], "clips": clips or [review["clips"][0]["clip_id"]],
        "platforms": list(platforms), "decided_at": store.stamp(decided), **extra,
    })


def best_first(db, job):
    return [c["id"] for c in sorted(db.clips_for_job(job["id"]), key=lambda c: -c["score"])]


# ---- the state machine ------------------------------------------------------------


def test_the_happy_path_is_allowed_and_shortcuts_are_not():
    path = [states.READY_FOR_REVIEW, states.NOTIFICATION_PENDING, states.NOTIFICATION_SENT,
            states.AWAITING_APPROVAL, states.APPROVED, states.PUBLISH_QUEUED, states.PUBLISHING, states.PUBLISHED]
    for a, b in zip(path, path[1:], strict=False):
        assert states.can_move(a, b), (a, b)
    assert not states.can_move(states.NOTIFICATION_PENDING, states.APPROVED)  # never approved unseen
    assert not states.can_move(states.EXPIRED, states.APPROVED)               # expiry needs a new offer
    assert not states.can_move(states.CANCELLED, states.APPROVED)
    assert not states.can_move(states.PUBLISHED, states.PUBLISH_QUEUED)       # never posted twice
    with pytest.raises(states.IllegalMove):
        states.check(states.READY_FOR_REVIEW, states.PUBLISHED)


def test_processing_and_rendering_come_from_the_job_row():
    assert states.for_job("running") == states.PROCESSING
    assert states.for_job("running", "render") == states.RENDERING
    assert states.for_job("done") is None


# ---- counting what was found ------------------------------------------------------


def test_counts_come_from_the_clips_and_read_plainly():
    clips = [{"category": c} for c in ("dunks", "dunks", "threes", "blocks", "other")]
    found = summary.counts(clips)
    assert found == {"dunks": 2, "threes": 1, "blocks": 1, "other": 1}
    assert summary.counts_text(found) == "2 dunks, 1 three, 1 block and 1 other play"
    assert summary.counts_text({}) == "no highlights"
    assert "\n" not in summary.counts_text(found)


def test_a_clip_counts_once_in_its_first_category():
    # The engine names each clip's category (gameday.types.CATEGORIES); one
    # this edition doesn't count is an other play, and in a talk video every
    # clip is a highlight.
    assert summary.category_of("dunks") == "dunks"
    assert summary.category_of("clutch") == "clutch"
    assert summary.category_of("free_throw") == "other"
    assert summary.category_of("") == "other"
    assert summary.category_of("dunks", "video") == "highlight"
    assert summary.counts_text({"dunks": 1, "big_moment": 1, "other": 2}) == \
        "1 dunk, 1 big moment and 2 other plays"


def test_a_big_moment_is_called_one_and_only_unknown_plays_are_other_plays():
    assert summary.category_of("big_moment") == "big_moment"
    assert summary.counts_text({"dunks": 1, "big_moment": 2, "other": 1}) == "1 dunk, 2 big moments and 1 other play"
    assert summary.counts_text({"big_moment": 1}) == "1 big moment"


def test_highlights_with_nothing_more_to_say_read_as_highlights():
    def ready(offered, total, found):
        return summary.ready_text({"offered": offered, "total_clips": total, "counts": found,
                                   "counts_text": summary.counts_text(found)})

    assert ready(3, 3, {"highlight": 3}) == "3 highlights"
    assert ready(3, 5, {"highlight": 3}) == "3 highlights (best 3 of 5)"
    assert ready(1, 1, {"highlight": 1}) == "1 highlight"
    assert ready(3, 3, {"dunks": 2, "highlight": 1}) == "3 clips: 2 dunks and 1 highlight"
    assert summary.build("", [], [], kind="video")["title"] == "your video"


def test_which_kind_of_video_a_job_is():
    assert summary.kind_of({"mode": "basketball"}) == "basketball"
    assert summary.kind_of({}) == "basketball"
    assert summary.kind_of({"mode": "talk"}) == "video"


def test_the_snapshot_pins_each_file_and_the_fingerprint_follows_it(db, data_dir):
    job = finished_job(db, data_dir)
    clips = db.clips_for_job(job["id"])
    snap = summary.snapshot(clips, limit=3)
    assert [c["score"] for c in snap] == [92, 88, 80]      # best first, capped
    assert all(summary.file_matches(c) for c in snap)
    before = summary.fingerprint(snap)
    with open(snap[0]["path"], "ab") as f:
        f.write(b"re-rendered")
    assert not summary.file_matches(snap[0])
    assert summary.fingerprint(summary.snapshot(clips, limit=3)) != before


def test_the_message_text_is_deterministic(db, data_dir):
    job = finished_job(db, data_dir)
    clips = db.clips_for_job(job["id"])
    snap = summary.snapshot(clips, limit=3)
    info = summary.build("Blue vs Gold", clips, snap)
    assert summary.ready_text(info) == "3 clips (best 3 of 5): 2 dunks and 1 three"
    assert summary.ready_text(info) == summary.ready_text(summary.build("Blue vs Gold", clips, snap))


# ---- the offer -----------------------------------------------------------------------


def test_nothing_happens_while_switched_off(db, data_dir):
    job = finished_job(db, data_dir)
    assert notifier.on_job_finished(db, job, "done") is None
    assert store.pending_outbox(db) == 0


def test_a_finished_basketball_job_is_offered_once(db, on):
    job = finished_job(db, on)
    review_id = notifier.on_job_finished(db, job, "done")
    review = store.get_review(db, review_id)
    assert review["state"] == states.NOTIFICATION_PENDING
    assert len(review["clips"]) == 5
    assert review["summary"]["counts"]["dunks"] == 2
    assert store.pending_outbox(db) == 1
    # The same job announced again: no second offer, no second message.
    assert notifier.on_job_finished(db, job, "done") is None
    assert store.pending_outbox(db) == 1


def test_only_the_clips_this_run_rendered_are_offered(db, on):
    # The same video was clipped before, by another job: its clips are still
    # in the table, with their files on disk.
    first = finished_job(db, on)
    again = finished_job(db, on, video_id=first["video_id"], plays=[("threes", "Corner three", 70)])
    db.add_clip(again["id"], again["video_id"], idx=2, path=str(on / "gone.mp4"), start_s=50, end_s=62,
                category="dunks", label="Gone", score=99)  # its file was deleted since
    review = store.get_review(db, notifier.on_job_finished(db, again, "done"))
    assert [c["clip_id"] for c in review["clips"]] == [c["id"] for c in db.clips_for_job(again["id"])][:1]
    assert review["summary"]["total_clips"] == 1  # a missing file is never offered
    assert summary.ready_text(review["summary"]) == "1 clip: 1 three"


def test_other_sports_are_left_alone(db, on):
    job = finished_job(db, on, mode="talk", title="Press conference")
    assert notifier.on_job_finished(db, job, "done") is None


def test_a_failure_is_announced_without_its_error(db, on):
    job = finished_job(db, on)
    db.update_job(job["id"], state="failed", error="Traceback: C:\\Users\\someone\\secret path")
    notifier.on_job_finished(db, db.get_job(job["id"]), "failed")
    item = store.due_outbox(db)[0]
    assert item["kind"] == "failed"
    assert "secret" not in json.dumps(item["body"])
    assert service.build_body(db, item) == {"kind": "failed", "event_key": item["event_key"]}


def test_sending_the_offer_records_the_relay_id(db, on):
    review, relay = offered(db, on)
    assert review["state"] == states.NOTIFICATION_SENT
    assert review["approval_id"].startswith("apr_")
    sent = relay.notified[0]
    assert sent["summary_text"] == "5 clips: 2 dunks, 1 three, 1 block and 1 other play"
    assert sent["fingerprint"] == review["fingerprint"]
    assert sent["remote_approvals"] is True
    assert all("path" not in c for c in sent["clips"])  # no local path leaves the app
    assert str(on) not in json.dumps(sent)
    assert store.pending_outbox(db) == 0


# ---- which clips an offer holds -----------------------------------------------------------


def test_making_clips_again_offers_only_the_new_runs_clips(db, on):
    """A run, then the job retried: the retry offers only its own clips."""
    plain = finished_job(db, on, plays=[("other", "Warm-up", 40)] * 3)
    store.move(db, notifier.on_job_finished(db, plain, "done"), states.CANCELLED, actor="test")
    again = finished_job(db, on, video_id=plain["video_id"], plays=[("dunks", "Dunk", 90)])
    review = store.get_review(db, notifier.on_job_finished(db, again, "done"))
    assert [c["clip_id"] for c in review["clips"]] == [c["id"] for c in db.clips_for_job(again["id"])]
    assert summary.ready_text(review["summary"]) == "1 clip: 1 dunk"


def test_a_re_run_that_finds_the_same_moments_is_offered_again(db, on):
    """A retry that finds the same windows makes new clips of them, and its
    offer is made of those, though the first run's offer is settled."""
    first = finished_job(db, on)
    store.move(db, notifier.on_job_finished(db, first, "done"), states.CANCELLED, actor="test")
    again = finished_job(db, on, video_id=first["video_id"])
    review = store.get_review(db, notifier.on_job_finished(db, again, "done"))
    assert sorted(c["clip_id"] for c in review["clips"]) == sorted(c["id"] for c in db.clips_for_job(again["id"]))


def test_asking_again_never_takes_in_a_later_runs_clips(db, on):
    first = finished_job(db, on, plays=[("dunks", "Dunk", 90), ("dunks", "Dunk", 80)])
    mine = [c["id"] for c in db.clips_for_job(first["id"])]
    review = store.get_review(db, notifier.on_job_finished(db, first, "done"))
    store.move(db, review["id"], states.EXPIRED, actor="test")
    finished_job(db, on, video_id=first["video_id"], plays=[("threes", "Three", 99)])
    again = store.get_review(db, service.ask_again(db, review["id"]))
    assert sorted(c["clip_id"] for c in again["clips"]) == sorted(mine)
    # Cleared from the jobs list since: the offer's own clips, nothing new.
    store.move(db, again["id"], states.CANCELLED, actor="test")
    db.conn.execute("DELETE FROM jobs WHERE id = ?", (first["id"],))
    db.conn.commit()
    third = store.get_review(db, service.ask_again(db, again["id"]))
    assert sorted(c["clip_id"] for c in third["clips"]) == sorted(mine)


def test_gaming_and_other_videos_wait_for_the_creator_to_opt_in(db, on):
    talk = finished_job(db, on, mode="talk", title="Locker room", plays=[("other", "Coach speaks", 70)] * 2)
    assert ra_settings.load(db)["basketball_only"] is True  # as it ships
    assert notifier.on_job_finished(db, talk, "done") is None
    assert store.pending_outbox(db) == 0
    ra_settings.save(db, {"basketball_only": False})  # "Offer other videos too"
    review = store.get_review(db, notifier.on_job_finished(db, talk, "done"))
    assert review["summary"]["kind"] == "video"
    assert summary.ready_text(review["summary"]) == "2 highlights"
    assert store.pending_outbox(db) == 1


def test_a_pipeline_that_is_not_a_games_own_offers_a_video_in_plain_words(db, on):
    """A talk video says nothing about basketball: never tagged #basketball."""
    ra_settings.save(db, {"basketball_only": False})
    job = finished_job(db, on, mode="talk", plays=[("other", "Loud", 60)])
    review = store.get_review(db, notifier.on_job_finished(db, job, "done"))
    assert review["clips"][0]["label"] == "Loud"  # the engine's own word for it, on the card
    entry = review["clips"][0]
    made = captions.texts_for(review, db.get_clip(entry["clip_id"]), entry)
    assert "basketball" not in json.dumps(made).lower()


def test_without_a_key_the_engines_loudness_note_is_never_posted(db, on):
    review = {"captions": {}, "fingerprint": "f", "summary": {"kind": "basketball"}}
    entry = {"clip_id": 1, "title": "Big moment 1", "label": "Loud moment at 0:28"}
    made = captions.texts_for(review, None, entry)
    assert made["instagram"] == made["facebook"] == "Big moment 1\n\n#basketball #highlights"
    told = captions.texts_for(review, None, {**entry, "label": "Poster dunk"})
    assert told["instagram"] == "Big moment 1\n\nPoster dunk\n\n#basketball #highlights"


def test_a_relay_that_is_down_is_retried_later_and_a_refusal_is_not(db, on):
    job = finished_job(db, on)
    review_id = notifier.on_job_finished(db, job, "done")
    relay = FakeRelay()
    relay.fail["notify"] = RelayError("network", "Couldn't reach your relay.", retryable=True)
    with pytest.raises(RelayError):
        service.flush_outbox(db, relay)
    row = db.conn.execute("SELECT * FROM remote_outbox").fetchone()
    assert row["status"] == "pending" and row["attempts"] == 1 and row["next_try_at"] > store.stamp()
    relay.fail["notify"] = RelayError("not_paired", "No WhatsApp number is paired.")
    service.flush_outbox(db, relay, now=store.now_utc() + timedelta(hours=1))
    row = db.conn.execute("SELECT * FROM remote_outbox").fetchone()
    assert row["status"] == "failed"
    assert store.get_review(db, review_id)["error"] == "No WhatsApp number is paired."


def test_a_relay_with_no_muse_key_is_told_in_the_apps_words(db, on):
    # The relay's own text for this names its other channel and the desktop
    # edition's settings; the dashboard shows what to do here instead.
    job = finished_job(db, on)
    review_id = notifier.on_job_finished(db, job, "done")
    relay = FakeRelay()
    relay.fail["notify"] = RelayError("no_channel", "WhatsApp messages are off on your PC and Muse is not "
                                      "connected, so nothing can tell you about this offer.", status=409)
    service.flush_outbox(db, relay)
    wanted = "The relay has no Muse key for this app. Make a Muse key in Setup, then Send again."
    review = store.get_review(db, review_id)
    assert review["state"] == states.NOTIFICATION_PENDING and review["error"] == wanted
    row = db.conn.execute("SELECT * FROM remote_outbox").fetchone()
    assert row["status"] == "failed" and row["error"] == wanted
    [failed] = [a for a in store.audit_log(db, review_id) if a["action"] == "notify_failed"]
    assert failed["detail"] == wanted


# ---- Muse Spark's captions --------------------------------------------------------------


def test_muse_spark_writes_every_clips_words_in_one_request_and_it_is_kept(db, on):
    meta = FakeMeta(db.ledger())
    job = finished_job(db, on, hint="Blue and Gold")
    review_id = notifier.on_job_finished(db, job, "done", meta=meta)
    review = store.get_review(db, review_id)
    [(_, purpose, job_id, messages)] = [c for c in meta.calls if c[1] == "captions"]
    assert (purpose, job_id) == ("captions", job["id"])
    told = json.loads(messages[-1]["content"])
    assert told["teams_or_players"] == "Blue and Gold"
    assert [c["clip_id"] for c in told["clips"]] == [c["clip_id"] for c in review["clips"]]
    assert told["clips"][0]["commentary"] == "What a poster dunk!"
    assert "Never invent" in messages[0]["content"]
    assert [m["role"] for m in messages] == ["system", "user"]  # the roles the engine's Spark call uses
    entry = review["clips"][0]
    text = captions.texts_for(review, db.get_clip(entry["clip_id"]), entry)
    assert text["source"] == "muse_spark" and text["title"] == "Muse: Poster dunk"
    assert text["instagram"].endswith("#hoops #game #day #basketball")  # hashtags never hold a space
    # Kept: a second offer of the same clips asks nothing.
    assert captions.generate(db, review, {}, meta=meta) == review["captions"]
    assert [c[1] for c in meta.calls] == ["captions", "offer_line"]  # and the offer's line, once
    assert db.job_cost(job["id"]) == 0.004


def test_hashtags_muse_spark_puts_in_the_caption_are_posted_once(db, on):
    meta = FakeMeta(db.ledger())
    plain = meta.chat_json

    def with_hashtags_inside(*args, **kwargs):
        answer = plain(*args, **kwargs)
        for clip in answer.get("clips") or []:  # the captions, not the offer's line
            clip["instagram_caption"] += " #Hoops #Clutch"  # what the real Muse Spark did
            clip["facebook_description"] = "What a #clutch finish. #HOOPS"
        return answer

    meta.chat_json = with_hashtags_inside
    review = store.get_review(db, notifier.on_job_finished(db, finished_job(db, on), "done", meta=meta))
    entry = review["clips"][0]
    text = captions.texts_for(review, db.get_clip(entry["clip_id"]), entry)
    assert text["instagram"] == "Poster dunk from tonight.\n\n#hoops #game #day #basketball #clutch"
    assert text["facebook"] == "What a #clutch finish.\n\n#hoops #game #day #basketball"
    assert "no hashtags" in captions.INSTRUCTIONS


# ---- Muse Spark's one-line summary of the offer ---------------------------------------------


COUNTED = "5 clips: 2 dunks, 1 three, 1 block and 1 other play"
#: What FakeMeta writes for the plays finished_job makes (platform_fakes.spark_line).
LINE = "5 big plays, led by the poster dunk in the third"


@pytest.fixture
def muse_on(db, data_dir):
    """Muse alone, as the dashboard sets the phone up: a Muse key and remote
    approval on, the app registered, every platform connected."""
    switch_on(db, whatsapp=False, muse=True)
    return data_dir


def sent_summary(db) -> str:
    """The summary_text the relay is sent with the offer just made: what Muse
    says and the approval page shows."""
    relay = FakeRelay()
    service.flush_outbox(db, relay)
    [body] = [b for b in relay.notified if b["kind"] == "ready"]
    return body["summary_text"]


def skipped_because(db, review_id: int) -> str:
    [skipped] = [a for a in store.audit_log(db, review_id) if a["action"] == "offer_line_skipped"]
    return skipped["detail"]


def test_muse_spark_writes_a_muse_offers_line_and_the_relay_gets_it_as_its_summary(db, muse_on):
    meta = FakeMeta(db.ledger())
    job = finished_job(db, muse_on, hint="Blue and Gold", credit="Footage: a notice")
    review = store.get_review(db, notifier.on_job_finished(db, job, "done", meta=meta))
    assert [c[1] for c in meta.calls] == ["captions", "offer_line"]  # one small request for the line
    [(_, _, job_id, messages)] = [c for c in meta.calls if c[1] == "offer_line"]
    assert job_id == job["id"]
    assert "one-line summary" in messages[0]["content"]
    assert "No hashtags, no links, no emojis" in messages[0]["content"]
    told = json.loads(messages[1]["content"])
    assert told["clip_count"] == 5 and told["counts"] == "2 dunks, 1 three, 1 block and 1 other play"
    assert told["clips"][0] == {"title": "Poster dunk in the third", "kind": "dunk"}  # best first
    # Only the clips' titles and plays: no commentary, no hint, no credit line.
    said = json.dumps(messages)
    assert "What a poster dunk" not in said and "Blue and Gold" not in said and "notice" not in said
    assert "whatsapp" not in said.lower()
    assert review["summary"]["offer_line"] == {"line": LINE, "fingerprint": review["fingerprint"],
                                               "model": "muse-spark-1.3"}
    assert offer_line.summary_text(review) == sent_summary(db) == LINE
    # On the ledger with the captions, in the job's Captions stage.
    assert db.job_cost(job["id"]) == 0.004
    from gameday.routes.api import stage_of_call

    assert stage_of_call("muse-spark-1.3", "offer_line") == stage_of_call("muse-spark-1.3", "captions") == "captions"


def test_the_line_is_written_with_muse_sparks_captions_off_too(db, muse_on):
    ra_settings.save(db, {"muse_captions": False})
    meta = FakeMeta(db.ledger())
    review_id = notifier.on_job_finished(db, finished_job(db, muse_on), "done", meta=meta)
    assert [c[1] for c in meta.calls] == ["offer_line"]
    assert store.get_review(db, review_id)["captions"] == {}  # the clips' own captions
    assert sent_summary(db) == LINE


def test_a_videos_line_is_asked_for_in_plain_words(db, muse_on):
    ra_settings.save(db, {"basketball_only": False})
    meta = FakeMeta(db.ledger())
    job = finished_job(db, muse_on, mode="talk", plays=[("other", "Coach speaks", 70)] * 2)
    notifier.on_job_finished(db, job, "done", meta=meta)
    [(_, _, _, messages)] = [c for c in meta.calls if c[1] == "offer_line"]
    assert "moments" in messages[0]["content"] and "basketball" not in json.dumps(messages).lower()
    assert json.loads(messages[1]["content"])["clips"][0]["kind"] == "highlight"


def test_the_line_is_kept_and_an_offer_of_the_same_clips_reuses_it(db, muse_on):
    meta = FakeMeta(db.ledger())
    review_id = notifier.on_job_finished(db, finished_job(db, muse_on), "done", meta=meta)
    assert sent_summary(db) == LINE
    store.move(db, review_id, states.EXPIRED, actor="test")
    again = store.get_review(db, service.ask_again(db, review_id, meta=meta))
    assert offer_line.summary_text(again) == LINE
    assert [c[1] for c in meta.calls].count("offer_line") == 1  # asked once


def test_the_line_is_cleaned_to_one_line_and_capped():
    raw = ('  "A monster dunk,\n\ta deep three    and a block!" '
           '\U0001f525\U0001f3c0\N{ZERO WIDTH JOINER}\N{VARIATION SELECTOR-16}  ')
    assert offer_line.clean(raw) == "A monster dunk, a deep three and a block"
    assert offer_line.clean("Big night \U0001f44d\U0001f3fd for the bench…") == "Big night for the bench"
    assert offer_line.clean("“Two dunks and a block.”") == "Two dunks and a block"
    for line in (offer_line.clean(raw), offer_line.clean("one\r\ntwo\x0bthree")):
        assert "\n" not in line and "\t" not in line and "    " not in line
    review = {"clips": [], "summary": {"offered": 2, "total_clips": 2, "counts": {"dunks": 2}}}
    assert offer_line.problem("x" * 200, review) == ""
    assert "longer than 200" in offer_line.problem("x" * 201, review)
    # Counted as the relay counts (it keeps 300): a character outside the BMP is two.
    assert "longer than 200" in offer_line.problem("\U00020000" * 101, review)


@pytest.mark.parametrize("line, why", [
    ("", "empty"),
    ("\U0001f525\U0001f525\U0001f525", "empty"),  # nothing but emoji
    ("A poster dunk and " + "a lot more " * 20, "longer than 200"),
    ("Watch them at https://example.com", "link"),
    ("Big plays, see clipskitty.com", "link"),
    ("A #poster dunk and a block", "hashtag"),
    ("3 dunks and a block", "a number the clips don't have"),  # there were 2
], ids=["empty", "only-emoji", "too-long", "link", "bare-link", "hashtag", "made-up-number"])
def test_a_line_that_breaks_the_rules_is_not_used_and_the_counts_go_instead(db, muse_on, line, why):
    meta = SaysLine(line, ledger=db.ledger())
    review_id = notifier.on_job_finished(db, finished_job(db, muse_on), "done", meta=meta)
    assert sent_summary(db) == COUNTED
    assert why in skipped_because(db, review_id)
    assert offer_line.line_of(store.get_review(db, review_id)) == ""  # nothing kept


def test_a_number_the_counts_or_a_title_hold_is_allowed(db, muse_on):
    plays = [("dunks", "Dunk at 7 seconds left", 90), ("dunks", "Dunk", 80), ("threes", "Three", 70)]
    meta = SaysLine("2 dunks, one with 7 seconds left, and a three", ledger=db.ledger())
    notifier.on_job_finished(db, finished_job(db, muse_on, plays=plays), "done", meta=meta)
    assert sent_summary(db) == "2 dunks, one with 7 seconds left, and a three"


def test_without_a_key_or_when_muse_spark_fails_the_offer_says_the_counts(db, muse_on):
    notifier.on_job_finished(db, finished_job(db, muse_on), "done")  # no Meta key: nothing is asked
    assert sent_summary(db) == COUNTED
    failing = FakeMeta(db.ledger(), fail=MetaError("Muse Spark answered 503.", status=503))
    review_id = notifier.on_job_finished(db, finished_job(db, muse_on, title="Blue vs Red"), "done", meta=failing)
    assert sent_summary(db) == COUNTED
    assert skipped_because(db, review_id) == "Muse Spark answered 503."
    assert store.get_review(db, review_id)["state"] == states.NOTIFICATION_SENT  # the offer went all the same


def test_no_line_is_asked_for_a_judges_game(db, muse_on):
    meta = FakeMeta(db.ledger())
    job = finished_job(db, muse_on, title="Judged")
    db.conn.execute("UPDATE jobs SET created_by = 'judge' WHERE id = ?", (job["id"],))
    db.conn.commit()
    # It is never offered; asked directly, no line is asked for it either.
    assert notifier.on_job_finished(db, db.get_job(job["id"]), "done", meta=meta) is None
    assert meta.calls == []
    review_id = notifier.offer(db, job_id=job["id"], video_id=job["video_id"], title="Judged",
                               clips=notifier.clips_of_job(db, job["id"]), meta=meta)
    assert offer_line.write(db, review_id, meta) == access.NOT_OFFERED
    assert "offer_line" not in [c[1] for c in meta.calls]
    assert offer_line.summary_text(store.get_review(db, review_id)) == COUNTED


# ---- the credit line ----------------------------------------------------------------------


CREDIT = ("Game footage: DVIDS (public domain). The appearance of U.S. Department of War (DoW) visual "
          "information does not imply or constitute DoW endorsement.")


def test_the_clips_own_captions_end_with_the_credit_line_and_the_offer_carries_them(db, on):
    db.save_job_settings({"caption_credit": CREDIT})
    review, relay = offered(db, on)  # no Meta key: the clips' own text
    entry = review["clips"][0]
    made = captions.texts_for(review, db.get_clip(entry["clip_id"]), entry)
    body = "Poster dunk in the third\n\nPoster dunk\n\n#basketball #highlights"
    assert made["source"] == "clip"
    assert made["instagram"] == made["facebook"] == f"{body}\n\n{CREDIT}"
    assert CREDIT not in made["title"]
    # The offer's message holds every clip's final text, credit line and all:
    # the approval page shows exactly what is posted.
    sent = relay.notified[0]["clips"]
    assert len(sent) == len(review["clips"]) == 5
    for clip, entry in zip(sent, review["clips"], strict=True):
        final = captions.texts_for(review, db.get_clip(entry["clip_id"]), entry)
        assert clip["texts"] == {"instagram": final["instagram"], "facebook": final["facebook"]}
        assert all(text.endswith(f"#highlights\n\n{CREDIT}") for text in clip["texts"].values())


def test_muse_sparks_captions_end_with_the_credit_line_and_spark_is_never_shown_it(db, on):
    meta = FakeMeta(db.ledger())
    job = finished_job(db, on, hint="Blue and Gold", credit=CREDIT)
    review = store.get_review(db, notifier.on_job_finished(db, job, "done", meta=meta))
    assert [c[1] for c in meta.calls] == ["captions", "offer_line"]
    told = " ".join(m["content"] for c in meta.calls for m in c[3])
    assert "DVIDS" not in told and "endorsement" not in told  # never asked to write or reword it, nor shown it
    entry = review["clips"][0]
    text = captions.texts_for(review, db.get_clip(entry["clip_id"]), entry)
    assert text["source"] == "muse_spark" and text["title"] == "Muse: Poster dunk"
    assert text["instagram"] == f"Poster dunk from tonight.\n\n#hoops #game #day #basketball\n\n{CREDIT}"
    assert text["facebook"] == f"Watch the poster dunk.\n\n#hoops #game #day #basketball\n\n{CREDIT}"


def test_when_muse_spark_fails_the_clips_own_captions_still_end_with_the_credit_line(db, on):
    meta = FakeMeta(db.ledger(), fail=MetaError("Muse Spark is not answering.", model="muse-spark-1.3"))
    review = store.get_review(db, notifier.on_job_finished(db, finished_job(db, on, credit=CREDIT), "done",
                                                           meta=meta))
    assert meta.calls and not review["captions"]  # asked, and it failed
    relay = FakeRelay()
    service.flush_outbox(db, relay)
    for clip in relay.notified[0]["clips"]:
        assert clip["text_source"] == "clip"
        assert clip["texts"]["instagram"] == clip["texts"]["facebook"]
        assert clip["texts"]["instagram"].endswith(f"#basketball #highlights\n\n{CREDIT}")


def test_a_caption_too_long_for_its_platform_loses_words_never_the_credit_line():
    credit = "\n".join(("a" * 99, "b" * 99, "c" * 100))  # the most a credit line can be
    words = " ".join(["highlight"] * 200)[:1800]  # the longest caption Muse Spark's answer keeps
    review = {"fingerprint": "f", "summary": {"kind": "basketball", "credit": credit},
              "captions": {"fingerprint": "f", "clips": {"1": {
                  "title": "Dunk", "instagram": words, "facebook": words,
                  "hashtags": ["#" + "h" * 60, "#" + "k" * 60]}}}}
    text = captions.texts_for(review, None, {"clip_id": 1, "title": "Dunk"})
    for platform, limit in (("instagram", 2200), ("facebook", 2000)):
        caption = text[platform]
        assert captions.units(caption) <= limit, platform
        assert caption.endswith(f"\n\n{credit}"), platform
        body = caption[:-len(credit) - 2]
        assert body.endswith("…") and not body.endswith(" …"), body[-20:]
        assert words.startswith(body[:200])
    assert text["facebook"].endswith(f"highlight…\n\n{credit}")  # cut at the end of a word
    # Counted as the relay and Meta count: an emoji is two units, and is never split.
    review["captions"]["clips"]["1"]["instagram"] = "\U0001f3c0" * 1500
    caption = captions.texts_for(review, None, {"clip_id": 1, "title": "Dunk"})["instagram"]
    assert captions.units(caption) <= 2200 and caption.endswith(f"\U0001f3c0…\n\n{credit}")
    # Short enough: nothing is cut, and no credit line adds nothing.
    assert captions.fit("A dunk.\n\n#hoops", credit, 2200) == f"A dunk.\n\n#hoops\n\n{credit}"
    assert captions.fit("A dunk.\n\n#hoops", "", 2200) == "A dunk.\n\n#hoops"
    assert captions.fit("", credit, 2200) == credit


def test_a_game_keeps_its_own_credit_line_and_an_offer_the_one_it_was_made_with(db, on):
    db.save_job_settings({"caption_credit": "Another game's notice."})
    job = finished_job(db, on, credit=CREDIT)  # queued with this footage's line
    review_id = notifier.on_job_finished(db, job, "done")
    assert captions.credit_of(store.get_review(db, review_id)) == CREDIT
    relay = FakeRelay()
    service.flush_outbox(db, relay)
    # Changed once the offer is out: its page and its posts keep the line it went out with.
    db.save_job_settings({"caption_credit": ""})
    review = store.get_review(db, review_id)
    entry = review["clips"][0]
    posted = captions.texts_for(review, db.get_clip(entry["clip_id"]), entry)
    assert relay.notified[0]["clips"][0]["texts"] == {"instagram": posted["instagram"], "facebook": posted["facebook"]}
    assert posted["instagram"].endswith(f"\n\n{CREDIT}")
    # Asked again, it is still the game's own line; and once its job is gone, the offer's.
    store.move(db, review_id, states.EXPIRED, actor="test")
    again = store.get_review(db, service.ask_again(db, review_id))
    assert captions.credit_of(again) == CREDIT
    store.move(db, again["id"], states.CANCELLED, actor="test")
    db.conn.execute("DELETE FROM jobs WHERE id = ?", (job["id"],))
    db.conn.commit()
    assert captions.credit_of(store.get_review(db, service.ask_again(db, again["id"]))) == CREDIT


def test_a_game_queued_without_a_credit_line_takes_the_one_set_while_it_ran(db, on):
    job = finished_job(db, on, credit="")
    db.save_job_settings({"caption_credit": CREDIT})
    assert captions.credit_of(store.get_review(db, notifier.on_job_finished(db, job, "done"))) == CREDIT
    older = finished_job(db, on, title="Blue vs Red")  # queued before the setting existed
    assert "caption_credit" not in json.loads(older["options"])
    assert captions.credit_of(store.get_review(db, notifier.on_job_finished(db, older, "done"))) == CREDIT
    # No line anywhere: nothing is added.
    db.save_job_settings({"caption_credit": ""})
    review = store.get_review(db, notifier.on_job_finished(db, finished_job(db, on, title="Blue vs Green",
                                                                            credit=""), "done"))
    assert "credit" not in review["summary"]
    assert captions.texts_for(review, None, review["clips"][0])["instagram"].endswith("#basketball #highlights")


def test_an_answer_naming_clips_it_was_not_given_is_cleaned():
    parsed = captions._parse({"summary": " A\n night ", "clips": [
        {"clip_id": 7, "title": "x" * 200, "instagram_caption": "a", "facebook_description": "b",
         "hashtags": ["#One Two", "one", "Th-ree!"]},
        {"clip_id": 99, "title": "not offered"}, "junk", {"clip_id": "seven"}]}, {7})
    assert set(parsed["clips"]) == {"7"} and parsed["summary"] == "A night"
    assert parsed["clips"]["7"]["hashtags"] == ["#one", "#two", "#three"]
    assert len(parsed["clips"]["7"]["title"]) == 80


# ---- decisions from the phone ----------------------------------------------------------


def test_a_cancel_refused_while_an_upload_runs_changes_nothing(db, on):
    review, _ = offered(db, on)
    ids = [c["clip_id"] for c in review["clips"][:2]]
    commands.handle(db, approve_command(review, clips=ids, platforms=["instagram"]), secret=SECRET)
    running = db.claim_next_publish_job()
    with pytest.raises(states.IllegalMove):
        service.cancel_from_app(db, review["id"])
    jobs = commands.jobs_for_review(db, review["id"])
    assert sorted(j["status"] for j in jobs) == ["queued", "running"]  # the other clip still goes
    db.finish_publish_job(running["id"], "done")
    service.cancel_from_app(db, review["id"])  # nothing uploading now: the rest is stopped
    stopped = [j for j in commands.jobs_for_review(db, review["id"]) if j["id"] != running["id"]]
    assert [(j["status"], j["error"]) for j in stopped] == [("cancelled", "Cancelled from the app.")]
    assert store.get_review(db, review["id"])["state"] == states.CANCELLED


def test_an_approval_queues_one_publish_per_clip(db, on):
    review, _ = offered(db, on)
    ids = [c["clip_id"] for c in review["clips"][:2]]
    out = commands.handle(db, approve_command(review, clips=ids, platforms=["instagram", "facebook"]), secret=SECRET)
    assert out.outcome == "accepted" and out.queued == 2
    # The relay's page says "Your PC accepted it: <reason>." and adds the full stop itself.
    assert out.reason == "2 clips queued for Instagram Reels and Facebook Page Reels"
    review = store.get_review(db, review["id"])
    assert review["state"] == states.PUBLISH_QUEUED
    assert review["decision"]["platforms"] == ["instagram", "facebook"]
    jobs = commands.jobs_for_review(db, review["id"])
    assert [j["clip_id"] for j in jobs] == ids
    request = json.loads(jobs[0]["request"])
    assert request == {"provider": "meta", "review_id": review["id"], "approval_id": review["approval_id"],
                       "command_id": "cmd_" + "a" * 24, "platforms": ["instagram", "facebook"]}


def test_a_command_delivered_twice_is_applied_once(db, on):
    review, _ = offered(db, on)
    command = approve_command(review)
    assert commands.handle(db, command, secret=SECRET).outcome == "accepted"
    again = commands.handle(db, command, secret=SECRET)
    assert again.outcome == "accepted" and again.queued == 0
    assert len(commands.jobs_for_review(db, review["id"])) == 1


def test_a_second_approval_of_the_same_offer_is_ignored(db, on):
    review, _ = offered(db, on)
    commands.handle(db, approve_command(review), secret=SECRET)
    out = commands.handle(db, approve_command(review, cid="cmd_" + "b" * 24), secret=SECRET)
    assert out.outcome == "ignored"
    assert len(commands.jobs_for_review(db, review["id"])) == 1


@pytest.mark.parametrize("tamper, reason", [
    (lambda c: {**c, "sig": "0" * 64}, "signature"),
    (lambda c: sign_command({**c, "fingerprint": "f" * 32}), "not the clips"),
    (lambda c: sign_command({**c, "review_ref": 999}), "not one this PC made"),
    (lambda c: sign_command({**c, "approval_id": "apr_" + "9" * 24}), "not one this PC made"),
    (lambda c: sign_command({**c, "clips": [123456]}), "not in this offer"),
    (lambda c: sign_command({**c, "clips": []}), "No clips"),
    (lambda c: sign_command({**c, "platforms": ["tiktok"]}), "destinations"),
    (lambda c: sign_command({**c, "platforms": []}), "destinations"),
])
def test_a_command_that_does_not_match_its_offer_is_refused(db, on, tamper, reason):
    review, _ = offered(db, on)
    out = commands.handle(db, tamper(approve_command(review)), secret=SECRET)
    assert out.outcome == "rejected"
    assert reason in out.reason
    assert commands.jobs_for_review(db, review["id"]) == []
    assert store.get_review(db, review["id"])["state"] == states.NOTIFICATION_SENT


def test_a_clip_changed_after_the_offer_is_not_published(db, on):
    review, _ = offered(db, on)
    with open(review["clips"][0]["path"], "ab") as f:
        f.write(b"edited")
    out = commands.handle(db, approve_command(review), secret=SECRET)
    assert out.outcome == "rejected" and "changed" in out.reason


def test_a_platform_not_connected_is_refused(db, on):
    review, _ = offered(db, on)
    ra_settings.save(db, {"accounts": {"instagram": {"connected": False}}}, internal=True)
    out = commands.handle(db, approve_command(review), secret=SECRET)
    assert out.outcome == "rejected" and "not connected" in out.reason


def test_switching_remote_approval_off_refuses_approvals(db, on):
    review, _ = offered(db, on)
    ra_settings.save(db, {"remote_approvals": False})
    out = commands.handle(db, approve_command(review), secret=SECRET)
    assert out.outcome == "rejected" and "switched off" in out.reason


def test_an_approval_after_expiry_needs_a_new_offer(db, on):
    review, _ = offered(db, on)
    late = store.parse(review["expires_at"]) + timedelta(minutes=1)
    out = commands.handle(db, approve_command(review, decided=late), secret=SECRET, now=late)
    assert out.outcome == "rejected"
    assert store.get_review(db, review["id"])["state"] == states.EXPIRED


def test_an_approval_made_while_the_pc_was_off_is_honoured_when_it_comes_back(db, on):
    review, _ = offered(db, on)
    decided = store.now_utc()
    out = commands.handle(db, approve_command(review, decided=decided), secret=SECRET,
                          now=decided + timedelta(hours=20))  # the app is back the next morning
    assert out.outcome == "accepted"


def test_an_approval_too_old_to_act_on_is_not_posted_late(db, on):
    review, _ = offered(db, on)
    db.conn.execute("UPDATE remote_reviews SET expires_at = ? WHERE id = ?",
                    (store.stamp(store.now_utc() + timedelta(days=5)), review["id"]))
    db.conn.commit()
    review = store.get_review(db, review["id"])
    decided = store.now_utc()
    out = commands.handle(db, approve_command(review, decided=decided), secret=SECRET,
                          now=decided + timedelta(days=3))  # the app was off for three days
    assert out.outcome == "rejected" and "too old" in out.reason
    assert commands.jobs_for_review(db, review["id"]) == []


def test_cancel_defer_and_reminders(db, on):
    review, _ = offered(db, on)
    base = {"approval_id": review["approval_id"], "review_ref": review["id"]}
    out = commands.handle(db, sign_command({"id": "cmd_" + "1" * 24, "kind": "delivery", "status": "delivered",
                                            **base}), secret=SECRET)
    assert out.outcome == "accepted"
    assert store.get_review(db, review["id"])["state"] == states.AWAITING_APPROVAL
    commands.handle(db, sign_command({"id": "cmd_" + "2" * 24, "kind": "defer",
                                      "remind_at": "2030-01-01T10:00:00Z", **base}), secret=SECRET)
    review2 = store.get_review(db, review["id"])
    assert review2["state"] == states.DEFERRED and review2["remind_at"] == "2030-01-01T10:00:00Z"
    commands.handle(db, sign_command({"id": "cmd_" + "3" * 24, "kind": "reminded", **base}), secret=SECRET)
    assert store.get_review(db, review["id"])["state"] == states.AWAITING_APPROVAL
    commands.handle(db, sign_command({"id": "cmd_" + "4" * 24, "kind": "cancel", **base}), secret=SECRET)
    assert store.get_review(db, review["id"])["state"] == states.CANCELLED
    out = commands.handle(db, approve_command(review, cid="cmd_" + "5" * 24), secret=SECRET)
    assert out.outcome == "ignored" and "cancelled" in out.reason


def test_cancelling_from_the_phone_stops_uploads_that_have_not_started(db, on):
    review, _ = offered(db, on)
    commands.handle(db, approve_command(review, clips=[c["clip_id"] for c in review["clips"][:2]]), secret=SECRET)
    out = commands.handle(db, sign_command({"id": "cmd_" + "6" * 24, "kind": "cancel",
                                            "approval_id": review["approval_id"], "review_ref": review["id"]}),
                          secret=SECRET)
    assert out.outcome == "accepted"
    assert {j["status"] for j in commands.jobs_for_review(db, review["id"])} == {"cancelled"}
    assert store.get_review(db, review["id"])["state"] == states.CANCELLED


def test_a_failed_delivery_can_be_sent_again(db, on):
    review, _ = offered(db, on)
    commands.handle(db, sign_command({"id": "cmd_" + "7" * 24, "kind": "delivery", "status": "failed",
                                      "error": "WhatsApp could not deliver to this number.",
                                      "approval_id": review["approval_id"], "review_ref": review["id"]}),
                    secret=SECRET)
    review = store.get_review(db, review["id"])
    assert review["state"] == states.NOTIFICATION_PENDING
    assert "could not deliver" in review["error"]
    assert service.resend(db, review["id"])
    relay = FakeRelay()
    service.flush_outbox(db, relay)
    assert store.get_review(db, review["id"])["state"] == states.NOTIFICATION_SENT
    assert relay.notified[0]["event_key"].startswith(f"ready:review:{review['id']}:resend:")


# ---- settling, results and the loop ----------------------------------------------------------


def _finish(db, review, outcomes):
    """Pretend the uploads ran: {platform: (state, url)} for every job, on the
    platforms that job carries."""
    for job in commands.jobs_for_review(db, review["id"]):
        clip = db.get_clip(job["clip_id"])
        ok = False
        for platform, (state, url) in outcomes.items():
            if platform not in json.loads(job["request"])["platforms"]:
                continue
            db.record_clip_publish(clip["id"], platform, {"provider": "meta", "state": state, "post_url": url,
                                                          "request_id": review["approval_id"],
                                                          "video_id": clip["video_id"], "start_s": clip["start_s"],
                                                          "end_s": clip["end_s"],
                                                          "error": "" if state == "published" else "Refused."})
            ok = ok or state == "published"
        db.finish_publish_job(job["id"], "done" if ok else "failed")


def _posted_before(db, clip_id, platform, state="published", **extra):
    """A post an earlier approval made of this clip (or is still making)."""
    clip = db.get_clip(clip_id)
    db.record_clip_publish(clip_id, platform, {"provider": "meta", "state": state, "request_id": "apr_earlier",
                                               "video_id": clip["video_id"], "start_s": clip["start_s"],
                                               "end_s": clip["end_s"], **extra})


def test_everything_published_settles_published_and_says_so(db, on):
    review, relay = offered(db, on)
    commands.handle(db, approve_command(review, platforms=["instagram", "facebook"]), secret=SECRET)
    _finish(db, review, {"instagram": ("published", "https://www.instagram.com/reel/A/"),
                         "facebook": ("published", "https://www.facebook.com/reel/1")})
    assert service.settle_publishing(db, ra_settings.load(db)) == 1
    review = store.get_review(db, review["id"])
    assert review["state"] == states.PUBLISHED
    assert review["results"]["totals"]["published"] == 2
    service.flush_outbox(db, relay)
    result = relay.notified[-1]
    assert result["kind"] == "result" and result["text"] == "2 of 2 posts published"


def test_uploads_settle_even_once_whatsapp_and_muse_are_both_off(db, on):
    review, relay = offered(db, on)
    commands.handle(db, approve_command(review, platforms=["instagram", "facebook"]), secret=SECRET)
    ra_settings.save(db, {"whatsapp_enabled": False, "muse": {}}, internal=True)  # turned off mid-upload
    _finish(db, review, {"instagram": ("published", "https://www.instagram.com/reel/A/"),
                         "facebook": ("published", "https://www.facebook.com/reel/1")})
    changed = []
    assert service.tick(db, service.TickState(), relay=relay, emit=changed.append) == 0
    assert store.get_review(db, review["id"])["state"] == states.PUBLISHED
    assert changed == [{"type": "remote_approval", "changed": 1}]


def test_a_partial_failure_is_reported_as_one_and_can_be_retried(db, on):
    review, relay = offered(db, on)
    commands.handle(db, approve_command(review, platforms=["instagram", "facebook"]), secret=SECRET)
    _finish(db, review, {"instagram": ("published", "https://www.instagram.com/reel/A/"),
                         "facebook": ("failed", "")})
    service.settle_publishing(db, ra_settings.load(db))
    review = store.get_review(db, review["id"])
    assert review["state"] == states.PARTIAL_FAILURE
    assert service.result_text(review["results"]) == "1 of 2 posts published (1 failed)"
    posts = review["results"]["clips"][0]["posts"]
    assert posts[0]["url"] and posts[1]["url"] == ""
    assert service.retry_failed(db, review["id"]) == 1
    retry = json.loads(commands.jobs_for_review(db, review["id"])[-1]["request"])
    assert retry["platforms"] == ["facebook"]
    assert store.get_review(db, review["id"])["state"] == states.PUBLISH_QUEUED


def test_an_upload_cut_off_by_a_restart_is_never_called_published(db, on):
    review, _ = offered(db, on)
    commands.handle(db, approve_command(review), secret=SECRET)
    job = commands.jobs_for_review(db, review["id"])[0]
    db.record_clip_publish(job["clip_id"], "instagram", {"provider": "meta", "state": "processing",
                                                         "request_id": review["approval_id"]})
    db.conn.execute("UPDATE publish_jobs SET status = 'running' WHERE id = ?", (job["id"],))
    db.conn.commit()
    db.recover_running_publish_jobs()
    service.settle_publishing(db, ra_settings.load(db))
    review = store.get_review(db, review["id"])
    assert review["state"] == states.FAILED
    post = review["results"]["clips"][0]["posts"][0]
    assert post["state"] == "unconfirmed" and "Check the account" in post["error"]


def test_an_offer_leaves_out_where_a_clip_is_already_posted(db, on):
    job = finished_job(db, on)
    best = best_first(db, job)
    for platform in ra_settings.PLATFORMS:
        _posted_before(db, best[0], platform)
    _posted_before(db, best[1], "instagram")
    _posted_before(db, best[1], "facebook", state="failed")  # never went up
    review_id = notifier.on_job_finished(db, job, "done")
    relay = FakeRelay()
    service.flush_outbox(db, relay)
    review = store.get_review(db, review_id)
    entries = {c["clip_id"]: c for c in review["clips"]}
    assert best[0] not in entries  # posted everywhere already: nothing to offer it for
    assert entries[best[1]]["published"] == ["instagram"]
    assert "published" not in entries[best[2]]
    assert summary.ready_text(review["summary"]) == "4 clips: 1 dunk, 1 three, 1 block and 1 other play"
    sent = {c["clip_id"]: c for c in relay.notified[0]["clips"]}
    assert sent[best[1]]["published"] == ["instagram"] and sent[best[2]]["published"] == []


def test_an_approval_never_posts_a_clip_where_it_is_already_posted(db, on):
    review, _ = offered(db, on)
    first, second = review["clips"][0]["clip_id"], review["clips"][1]["clip_id"]
    # Posted to Instagram by an earlier offer while this one waited on the phone.
    _posted_before(db, first, "instagram", post_url="https://www.instagram.com/reel/U/")
    out = commands.handle(db, approve_command(review, clips=[first, second], platforms=["instagram", "facebook"]),
                          secret=SECRET)
    assert out.outcome == "accepted" and out.queued == 2
    jobs = {j["clip_id"]: json.loads(j["request"])["platforms"] for j in commands.jobs_for_review(db, review["id"])}
    assert jobs == {first: ["facebook"], second: ["instagram", "facebook"]}
    _finish(db, review, {"instagram": ("published", "https://www.instagram.com/reel/A/"),
                         "facebook": ("published", "https://www.facebook.com/reel/1")})
    service.settle_publishing(db, ra_settings.load(db))
    review = store.get_review(db, review["id"])
    assert review["state"] == states.PUBLISHED
    posts = {(c["clip_id"], p["platform"]): p for c in review["results"]["clips"] for p in c["posts"]}
    assert posts[(first, "instagram")]["state"] == "skipped"
    assert "Already posted" in posts[(first, "instagram")]["error"]
    assert posts[(second, "instagram")]["state"] == "published"
    assert service.result_text(review["results"]) == "3 of 4 posts published (1 skipped)"


def test_an_approval_with_nothing_left_to_post_is_refused(db, on):
    review, _ = offered(db, on)
    clip = review["clips"][0]["clip_id"]
    _posted_before(db, clip, "instagram")
    out = commands.handle(db, approve_command(review, clips=[clip], platforms=["instagram"]), secret=SECRET)
    assert out.outcome == "rejected" and "already posted" in out.reason
    assert commands.jobs_for_review(db, review["id"]) == []
    assert store.get_review(db, review["id"])["state"] == states.FAILED


def test_an_approval_never_posts_where_a_post_is_on_its_way(db, on):
    """An earlier offer's upload still processing goes up all the same, so the
    phone's approval leaves that platform out."""
    review, _ = offered(db, on)
    first, second = (c["clip_id"] for c in review["clips"][:2])
    _posted_before(db, first, "instagram", state="queued")
    _posted_before(db, second, "facebook", state="processing")
    out = commands.handle(db, approve_command(review, clips=[first, second], platforms=["instagram", "facebook"]),
                          secret=SECRET)
    assert out.outcome == "accepted" and out.queued == 2
    jobs = {j["clip_id"]: json.loads(j["request"])["platforms"] for j in commands.jobs_for_review(db, review["id"])}
    assert jobs == {first: ["facebook"], second: ["instagram"]}
    _finish(db, review, {"instagram": ("published", "https://www.instagram.com/reel/A/"),
                         "facebook": ("published", "https://www.facebook.com/reel/1")})
    service.settle_publishing(db, ra_settings.load(db))
    review = store.get_review(db, review["id"])
    assert review["state"] == states.PUBLISHED
    posts = {(c["clip_id"], p["platform"]): p for c in review["results"]["clips"] for p in c["posts"]}
    assert posts[(first, "instagram")] == {"platform": "instagram", "state": "skipped", "url": "",
                                           "error": "Already on its way there, so it was not posted again."}
    assert posts[(second, "facebook")]["state"] == "skipped"
    assert service.result_text(review["results"]) == "2 of 4 posts published (2 skipped)"


def test_a_clip_on_its_way_everywhere_is_not_offered_and_only_what_is_up_is_called_posted(db, on):
    job = finished_job(db, on)
    best = best_first(db, job)
    for platform in ra_settings.PLATFORMS:
        _posted_before(db, best[0], platform, state="queued")
    _posted_before(db, best[1], "instagram", state="queued")
    review = store.get_review(db, notifier.on_job_finished(db, job, "done"))
    entries = {c["clip_id"]: c for c in review["clips"]}
    assert best[0] not in entries
    assert "published" not in entries[best[1]]  # the page says "Already posted" only of what is up


def test_a_clip_made_again_under_a_new_id_keeps_where_it_was_posted(db, on):
    """The job retried after a clip was posted: the retry makes the same
    window again under a new id. Its posts still count, matched on the
    clip's video and window."""
    first = finished_job(db, on)
    old = db.get_clip(best_first(db, first)[0])
    _posted_before(db, int(old["id"]), "instagram")
    again = finished_job(db, on, video_id=first["video_id"])
    new = next(c["id"] for c in db.clips_for_job(again["id"]) if c["start_s"] == old["start_s"])
    assert new != old["id"]
    review_id = notifier.on_job_finished(db, again, "done")
    service.flush_outbox(db, FakeRelay())
    review = store.get_review(db, review_id)
    assert next(c for c in review["clips"] if c["clip_id"] == new)["published"] == ["instagram"]
    commands.handle(db, approve_command(review, clips=[new], platforms=["instagram", "facebook"]), secret=SECRET)
    assert [json.loads(j["request"])["platforms"] for j in commands.jobs_for_review(db, review["id"])] == [["facebook"]]


def test_a_retry_sends_again_only_what_failed(db, on):
    """Never an upload a restart cut off: it may be up already."""
    review, _ = offered(db, on)
    cut, refused = (c["clip_id"] for c in review["clips"][:2])
    commands.handle(db, approve_command(review, clips=[cut, refused], platforms=["instagram"]), secret=SECRET)
    for job in commands.jobs_for_review(db, review["id"]):
        state, status = ("processing", "interrupted") if job["clip_id"] == cut else ("failed", "failed")
        db.record_clip_publish(job["clip_id"], "instagram", {"provider": "meta", "state": state, "error": "Refused.",
                                                             "request_id": review["approval_id"]})
        db.conn.execute("UPDATE publish_jobs SET status = ? WHERE id = ?", (status, job["id"]))
    db.conn.commit()
    service.settle_publishing(db, ra_settings.load(db))
    review = store.get_review(db, review["id"])
    assert service.result_text(review["results"]) == "0 of 2 posts published (1 failed, 1 not confirmed yet)"
    assert service.retry_failed(db, review["id"]) == 1
    again = [j for j in commands.jobs_for_review(db, review["id"]) if j["status"] == "queued"]
    assert [(j["clip_id"], json.loads(j["request"])["platforms"]) for j in again] == [(refused, ["instagram"])]


def test_a_local_retry_sends_only_the_clips_it_names(db, on):
    review, _ = offered(db, on)
    first, second = (c["clip_id"] for c in review["clips"][:2])
    decision = {"command_id": "cmd_" + "b" * 24, "clips": [first, second], "platforms": ["instagram", "facebook"]}
    assert commands.queue_publishes(db, review, decision, only={first: ["facebook"]}) == 1
    jobs = [(j["clip_id"], json.loads(j["request"])["platforms"]) for j in commands.jobs_for_review(db, review["id"])]
    assert jobs == [(first, ["facebook"])]


def test_offers_expire_on_their_own(db, on):
    review, _ = offered(db, on)
    later = store.parse(review["expires_at"]) + timedelta(seconds=1)
    assert service.expire_due(db, later) == 1
    assert store.get_review(db, review["id"])["state"] == states.EXPIRED
    new_id = service.ask_again(db, review["id"])
    assert new_id and store.get_review(db, new_id)["state"] == states.NOTIFICATION_PENDING


def test_the_loop_paces_itself_and_handles_commands(db, on):
    job = finished_job(db, on)
    notifier.on_job_finished(db, job, "done")
    relay = FakeRelay()
    state = service.TickState()
    clock = [100.0]
    service.tick(db, state, relay=relay, clock=lambda: clock[0])
    review = store.recent_reviews(db, 1)[0]
    assert review["state"] == states.NOTIFICATION_SENT
    relay.pending.append(approve_command(review))
    assert service.tick(db, state, relay=relay, clock=lambda: clock[0]) == 0  # not due yet
    clock[0] += service.FAST_SECONDS
    assert service.tick(db, state, relay=relay, clock=lambda: clock[0]) == 1
    assert relay.acks[-1][1] == "accepted"
    assert store.get_review(db, review["id"])["state"] == states.PUBLISH_QUEUED


def test_settings_are_cleaned_and_never_show_the_secret(db, on):
    for bad in ("http://relay.example.com", "http://localhost.evil.example", "http://localhost@evil.example",
                "http://127.0.0.1.nip.io", "https://user:pw@relay.example.com", "https://", "ftp://relay.example.com",
                "https://relay.example.com:port"):
        with pytest.raises(ValueError):
            ra_settings.save(db, {"relay_url": bad})
    for good in ("https://relay.example.com", "http://localhost:8787", "http://[::1]:8787"):
        assert ra_settings.save(db, {"relay_url": good})["relay_url"] == good
    s = ra_settings.save(db, {"relay_url": "http://127.0.0.1:8787/", "preferred_destinations": ["facebook", "tiktok"],
                              "approval_hours": 999, "pairing": {"state": "confirmed"}})
    assert s["relay_url"] == "http://127.0.0.1:8787"
    assert s["preferred_destinations"] == ["facebook"]
    assert s["approval_hours"] == 72
    view = ra_settings.public_view(db)
    assert view["registered"] is True
    assert SECRET not in json.dumps(view)


def test_saved_settings_naming_a_place_an_offer_cant_post_to_still_load(db):
    db.set_flag(ra_settings.SETTINGS_KEY, json.dumps({
        "preferred_destinations": ["tiktok", "facebook"],
        "accounts": {"facebook": {"connected": True, "name": "Demo Page"}, "tiktok": {"connected": True}},
    }))
    s = ra_settings.load(db)
    assert s["preferred_destinations"] == ["facebook"]
    assert s["accounts"] == {"facebook": {"connected": True, "name": "Demo Page"}}


def test_remote_approval_needs_pairing_and_a_pin_first(db):
    ra_settings.save(db, {"whatsapp_enabled": True})
    # WhatsApp on but no number confirmed: the note asks for Muse, the
    # dashboard's only way to reach the phone.
    why = service.can_enable_remote_approval(db)
    assert "Muse" in why and "WhatsApp" not in why
    ra_settings.save(db, {"pairing": {"state": "confirmed"}}, internal=True)
    assert "PIN" in service.can_enable_remote_approval(db)
    ra_settings.save(db, {"pin_set": True}, internal=True)
    assert service.can_enable_remote_approval(db) == ""


def test_the_device_secret_is_kept_encrypted(db, on):
    token = db.secret_token(ra_settings.DEVICE_SECRET)
    assert token and SECRET not in token
    assert ra_settings.device(db) == {"device_id": "dev_0123456789abcdef", "device_secret": SECRET}
    assert ra_settings.forget_device(db) and ra_settings.device(db) == {}


# ---- the owner's Muse agent -------------------------------------------------------------


@pytest.fixture
def muse_only(db, data_dir):
    """No WhatsApp at all: the app registered, a Muse key working, a PIN set,
    every platform connected, remote approval on."""
    switch_on(db, whatsapp=False, muse=True)
    return data_dir


def test_a_connected_muse_stands_in_for_a_paired_number_but_the_pin_is_still_asked(db):
    assert "Muse" in service.can_enable_remote_approval(db)
    ra_settings.save(db, {"muse": {"enabled": True}}, internal=True)
    assert "PIN" in service.can_enable_remote_approval(db)
    ra_settings.save(db, {"pin_set": True}, internal=True)
    assert service.can_enable_remote_approval(db) == ""


def test_offers_reach_muse_with_whatsapp_off_but_a_failure_message_does_not(db, muse_only):
    review_id = notifier.on_job_finished(db, finished_job(db, muse_only), "done")
    assert store.get_review(db, review_id)["state"] == states.NOTIFICATION_PENDING
    failed = finished_job(db, muse_only, title="Blue vs Green")
    db.update_job(failed["id"], state="failed", error="out of memory")
    assert notifier.on_job_finished(db, db.get_job(failed["id"]), "failed") is None
    assert [item["kind"] for item in store.due_outbox(db)] == ["ready"]  # no failure message: there is no WhatsApp
    ra_settings.save(db, {"notify_on_finish": False})
    assert notifier.on_job_finished(db, finished_job(db, muse_only, title="Third Game"), "done") is None
    ra_settings.save(db, {"notify_on_finish": True})
    ra_settings.save(db, {"muse": {}}, internal=True)  # the key revoked: nothing can reach the owner now
    assert notifier.on_job_finished(db, finished_job(db, muse_only, title="Fourth Game"), "done") is None


def test_the_loop_runs_for_muse_alone_and_stops_when_neither_is_on(db, muse_only):
    relay = FakeRelay()
    relay.muse = {"enabled": True, "created_at": 1_800_000_000, "last_used": 1_800_000_100}
    review_id = notifier.on_job_finished(db, finished_job(db, muse_only), "done")
    service.tick(db, service.TickState(), relay=relay, clock=lambda: 1000.0)
    assert store.get_review(db, review_id)["state"] == states.NOTIFICATION_SENT
    assert relay.notified[0]["video_kind"] == "basketball"
    assert ra_settings.load(db)["muse"] == relay.muse  # what the relay said, read on the pass
    ra_settings.save(db, {"muse": {}}, internal=True)
    quiet = FakeRelay()
    assert service.tick(db, service.TickState(), relay=quiet, clock=lambda: 1000.0) == 0
    assert quiet.calls == []


def test_the_relay_is_told_whether_whatsapp_is_wanted(db, muse_only):
    relay = FakeRelay()
    service.push_preferences(db, relay)
    prefs = next(call[1] for call in relay.calls if call[0] == "preferences")
    assert prefs["whatsapp_enabled"] is False and prefs["notify_on_finish"] is True


def test_results_go_to_the_relay_even_with_publishing_messages_off(db, on):
    """The relay keeps them for the status page and for Muse's "what got
    posted?"; it is the one that leaves the WhatsApp message out."""
    ra_settings.save(db, {"notify_on_publish": False})
    review, relay = offered(db, on)
    commands.handle(db, approve_command(review), secret=SECRET)
    _finish(db, review, {"instagram": ("published", "https://www.instagram.com/reel/A/")})
    service.settle_publishing(db, ra_settings.load(db))
    service.flush_outbox(db, relay)
    result = relay.notified[-1]
    assert result["kind"] == "result" and result["approval_id"] == review["approval_id"]
    assert result["results"]["clips"][0]["posts"][0]["url"] == "https://www.instagram.com/reel/A/"


def test_a_defer_or_cancel_asked_of_muse_is_logged_as_muses(db, on):
    review, _ = offered(db, on)
    base = {"approval_id": review["approval_id"], "review_ref": review["id"]}
    # The via is inside what the relay signed: added afterwards, the command is not the relay's.
    forged = {**sign_command({"id": "cmd_" + "c" * 24, "kind": "defer", **base}), "via": "muse"}
    assert commands.handle(db, forged, secret=SECRET).outcome == "rejected"
    out = commands.handle(db, sign_command({"id": "cmd_" + "8" * 24, "kind": "defer", "via": "muse",
                                            "remind_at": "2030-01-01T10:00:00Z", **base}), secret=SECRET)
    assert out.outcome == "accepted"
    commands.handle(db, sign_command({"id": "cmd_" + "9" * 24, "kind": "cancel", "via": "muse", **base}), secret=SECRET)
    assert store.get_review(db, review["id"])["state"] == states.CANCELLED
    actors = {row["action"]: row["actor"] for row in store.audit_log(db, review["id"])}
    assert actors["defer_accepted"] == actors["deferred"] == "muse"
    assert actors["cancel_accepted"] == actors["cancelled"] == "muse"


def test_an_approval_that_says_it_came_from_muse_is_refused(db, on):
    review, _ = offered(db, on)
    out = commands.handle(db, approve_command(review, via="muse"), secret=SECRET)
    assert out.outcome == "rejected" and "approval page with your PIN" in out.reason
    assert commands.jobs_for_review(db, review["id"]) == []
    assert store.get_review(db, review["id"])["state"] == states.NOTIFICATION_SENT


def test_the_muse_key_is_handed_over_once_and_never_kept(db, muse_only):
    relay = FakeRelay()
    key = service.make_muse_key(db, relay)
    assert key.startswith("ckm_") and key == relay.keys[0]
    saved = db.get_flag(ra_settings.SETTINGS_KEY, "")
    assert key not in saved and json.loads(saved)["muse"]["enabled"] is True
    assert key not in json.dumps(ra_settings.public_view(db))
    service.revoke_muse_key(db, relay)
    config = ra_settings.load(db)
    assert config["muse"]["enabled"] is False
    assert config["remote_approvals"] is False  # nothing is left to reach the owner


def test_an_offer_kept_for_muse_is_not_counted_as_a_whatsapp_message(db):
    usage.record_whatsapp(db, "muse")
    usage.record_whatsapp(db, "interactive")
    assert usage.load(db)["whatsapp"] == {"templates": 0, "free_form": 1, "usd": 0.0}


def test_usage_counts_the_meta_models_from_the_ledger(db, on):
    meta = FakeMeta(db.ledger())
    notifier.on_job_finished(db, finished_job(db, on), "done", meta=meta)
    got = usage.summary(db)
    # The captions and the offer's line.
    assert got["muse_spark"] == {"requests": 2, "prompt_tokens": 1800, "cached_tokens": 0, "completion_tokens": 600,
                                 "usd": 0.004}
    assert got["muse_voice"]["requests"] == 0 and got["estimated_usd"] == 0.004
