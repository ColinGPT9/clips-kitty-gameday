"""The whole phone approval flow, this app and the relay together, with Meta faked.

Ported from this week's event branch (tests/test_remote_approval_e2e.py). The
relay is the real meta-relay/worker.js, run under Node by
meta-relay/test/serve.mjs: real routes, real signatures, real D1 schema (on
node:sqlite), real approval page. Only Meta's servers are stand-ins, so
nothing is sent to WhatsApp or posted anywhere. This side is the real app
code against a real store.

    finished job -> WhatsApp offer -> phone opens the page, enters the PIN,
    picks clips and platforms -> the app picks the approval up -> the relay
    loop uploads the rendered file -> Instagram and Facebook -> results back
    to WhatsApp, with only the links the APIs returned

Skipped when Node 22.5+ (for node:sqlite) is not installed. The preview test
also needs ffmpeg.
"""

import json
import os
import shutil

import pytest
import requests
from conftest import ENROLL_KEY, VIDEO_BYTES
from platform_fakes import RELAY_DIR, FakeMeta, Muse, Phone, csrf_of, ffmpeg_ok, finished_job, make_video, node_ok

from gameday.remote_approval import captions, commands, notifier, publishing, service, states, store
from gameday.remote_approval import settings as ra_settings

PIN = "4826"

pytestmark = pytest.mark.skipif(not node_ok(), reason="needs Node 22.5+ for the relay")


def _runner(app, monkeypatch) -> dict:
    """The app's relay pass and its uploads, run by hand. The real client
    through the real relay; only its waits between status reads are skipped."""
    from gameday.publish.meta_graph import MetaGraph
    from gameday.remote_approval.relay import Relay

    monkeypatch.setattr(publishing, "make_graph",
                        lambda config, device: MetaGraph(Relay(config["relay_url"], **device), sleep=lambda s: None))
    clock = [1000.0]
    state = service.TickState()

    def tick():
        clock[0] += service.SLOW_SECONDS + service.STATUS_SECONDS
        return service.tick(app.store, state, clock=lambda: clock[0])

    def publish_all():
        n = 0
        while app.worker.publish_one():
            n += 1
        return n

    return {"tick": tick, "publish_all": publish_all}


@pytest.fixture
def pc(make_app, relay_url, monkeypatch):
    """An app set up the way the dashboard does it: registered with the relay,
    WhatsApp paired and confirmed, a PIN, Facebook (with its Instagram)
    connected, remote approval on."""
    phone = Phone(relay_url)
    app = make_app()
    db = app.store
    ra_settings.save(db, {"whatsapp_enabled": True, "relay_url": relay_url})
    # Each test gets its own app (a new device) on the shared relay.
    service.register(db, ENROLL_KEY)
    relay = service.client(db)
    code = relay.pair_start()["code"]
    phone.send(f"LINK {code}")
    assert "confirm that the number" in phone.last_message()["text"]["body"]
    relay.pair_confirm(True)
    relay.set_pin(PIN)
    assert phone.oauth(relay.accounts_connect("facebook")["url"])["ok"]
    service.refresh_status(db, relay)
    assert service.can_enable_remote_approval(db) == ""
    ra_settings.save(db, {"remote_approvals": True, "preferred_destinations": ["instagram"]})
    service.push_preferences(db, relay)
    return {"phone": phone, "relay": relay, "app": app, "db": db, **_runner(app, monkeypatch),
            "dir": app.config.data_dir}


@pytest.fixture
def muse_pc(make_app, relay_url, monkeypatch):
    """An app set up for Muse alone: registered, a PIN, Facebook (with its
    Instagram) connected, a Muse key made, remote approval on. No WhatsApp
    number is paired, and WhatsApp messages stay off."""
    phone = Phone(relay_url)
    app = make_app()
    db = app.store
    ra_settings.save(db, {"relay_url": relay_url})
    service.register(db, ENROLL_KEY)
    relay = service.client(db)
    relay.set_pin(PIN)
    assert phone.oauth(relay.accounts_connect("facebook")["url"])["ok"]
    key = service.make_muse_key(db, relay)
    service.refresh_status(db, relay)
    assert ra_settings.load(db)["whatsapp_enabled"] is False
    assert service.can_enable_remote_approval(db) == ""
    ra_settings.save(db, {"remote_approvals": True, "preferred_destinations": ["instagram"]})
    service.push_preferences(db, relay)
    return {"phone": phone, "relay": relay, "muse": Muse(relay_url, key), "app": app, "db": db,
            **_runner(app, monkeypatch), "dir": app.config.data_dir}


def test_a_finished_game_goes_from_whatsapp_to_instagram_and_facebook(pc):
    phone, db = pc["phone"], pc["db"]
    before = len(phone.state()["posts"])
    job = finished_job(db, pc["dir"])
    review_id = notifier.on_job_finished(db, job, "done")
    pc["tick"]()

    review = store.get_review(db, review_id)
    assert review["state"] == states.NOTIFICATION_SENT
    message = phone.last_message()
    text = json.dumps(message)
    assert "Blue vs Gold" in text and "2 dunks, 1 three, 1 block and 1 other play" in text
    assert str(pc["dir"]) not in text and "media" not in text  # no local paths

    # The phone opens the page: the clips, the connected accounts, a PIN box.
    token = phone.link_token(message)
    status, html = phone.open(token)
    assert status == 200
    account = ra_settings.load(db)["accounts"]["instagram"]["name"]  # as the relay reported it
    assert account and account in html
    assert "Poster dunk in the third" in html and 'name="pin"' in html
    first, second = review["clips"][0]["clip_id"], review["clips"][1]["clip_id"]
    status, html = phone.submit(token, [
        ("csrf", csrf_of(html)), ("action", "approve"), ("clip", str(first)), ("clip", str(second)),
        ("platform", "instagram"), ("platform", "facebook"), ("confirm", "yes"), ("pin", PIN),
    ])
    assert status == 200 and "Waiting for your PC" in html

    # The app picks it up on its next pass and queues the uploads.
    assert pc["tick"]() == 2
    assert store.get_review(db, review_id)["state"] == states.PUBLISH_QUEUED
    assert pc["publish_all"]() == 2

    posts = phone.state()["posts"][before:]
    assert sorted(p["platform"] for p in posts) == ["facebook", "facebook", "instagram", "instagram"]
    uploads = phone.state()["uploads"]
    sizes = {c["size"] for c in review["clips"][:2]}
    assert {u["bytes"] for u in uploads[-4:]} == sizes  # the rendered files, byte for byte

    # Settled from what the APIs confirmed, and said on WhatsApp.
    pc["tick"]()
    review = store.get_review(db, review_id)
    assert review["state"] == states.PUBLISHED
    links = [p["url"] for c in review["results"]["clips"] for p in c["posts"]]
    assert sorted(links) == sorted(p["permalink"] for p in posts)
    assert "4 of 4 posts published" in json.dumps(phone.last_message())

    # The status page shows the confirmed links; the offer can't be approved again.
    status, html = phone.open(token)
    assert all(p["permalink"] in html for p in posts)
    assert 'name="pin"' not in html


def test_the_credit_line_is_on_the_approval_page_and_in_every_post(pc):
    """What the page shows is what goes up: each caption ends with the
    credit line, on the page and in the posts Instagram and Facebook get."""
    phone, db = pc["phone"], pc["db"]
    credit = ("Game footage: DVIDS (public domain). The appearance of U.S. Department of War (DoW) visual "
              "information does not imply or constitute DoW endorsement.")
    db.save_job_settings({"caption_credit": credit})
    before = len(phone.state()["posts"])
    review_id = notifier.on_job_finished(db, finished_job(db, pc["dir"], title="Blue vs Navy"), "done")
    pc["tick"]()
    review = store.get_review(db, review_id)
    token = phone.link_token(phone.last_message())
    _, html = phone.open(token)
    assert html.count(f"#highlights\n\n{credit}") == 2 * len(review["clips"])  # Instagram's and Facebook's
    first = review["clips"][0]["clip_id"]
    status, html = phone.submit(token, [
        ("csrf", csrf_of(html)), ("action", "approve"), ("clip", str(first)),
        ("platform", "instagram"), ("platform", "facebook"), ("confirm", "yes"), ("pin", PIN),
    ])
    assert status == 200 and "Waiting for your PC" in html
    assert pc["tick"]() == 1 and pc["publish_all"]() == 1
    posts = phone.state()["posts"][before:]
    offered = captions.texts_for(review, db.get_clip(first), review["clips"][0])
    assert {p["platform"]: p["caption"] for p in posts} == {"instagram": offered["instagram"],
                                                            "facebook": offered["facebook"]}
    assert all(p["caption"].endswith(f"#basketball #highlights\n\n{credit}") for p in posts)


def test_an_approval_made_while_the_pc_is_off_waits_for_it(pc):
    phone, db = pc["phone"], pc["db"]
    job = finished_job(db, pc["dir"], title="Blue vs Red")
    review_id = notifier.on_job_finished(db, job, "done")
    pc["tick"]()
    token = phone.link_token(phone.last_message())
    _, html = phone.open(token)
    clip = store.get_review(db, review_id)["clips"][0]["clip_id"]
    phone.submit(token, [("csrf", csrf_of(html)), ("action", "approve"), ("clip", str(clip)),
                         ("platform", "instagram"), ("confirm", "yes"), ("pin", PIN)])
    # The app is off: the page says so and nothing has been posted.
    _, html = phone.open(token)
    assert "Waiting for your PC" in html
    assert store.get_review(db, review_id)["state"] == states.NOTIFICATION_SENT
    # Back on: the approval is picked up and published.
    pc["tick"]()
    pc["publish_all"]()
    pc["tick"]()
    assert store.get_review(db, review_id)["state"] == states.PUBLISHED


def test_a_plain_yes_never_approves_and_buttons_only_defer_or_cancel(pc):
    phone, db = pc["phone"], pc["db"]
    job = finished_job(db, pc["dir"], title="Blue vs Green")
    review_id = notifier.on_job_finished(db, job, "done")
    pc["tick"]()
    review = store.get_review(db, review_id)
    reply = phone.send("yes")["last"]
    assert "link" in json.dumps(reply).lower()  # guidance, not an approval
    pc["tick"]()
    assert commands.jobs_for_review(db, review_id) == []
    phone.tap(f"REMIND:{review['approval_id']}")
    pc["tick"]()
    assert store.get_review(db, review_id)["state"] == states.DEFERRED
    phone.tap(f"CANCEL:{review['approval_id']}")
    pc["tick"]()
    assert store.get_review(db, review_id)["state"] == states.CANCELLED
    # The link is dead now, and a new offer is a new link.
    token = phone.link_token(next(m for m in reversed(phone.state()["messages"])
                                  if "/a/" in json.dumps(m) or m.get("type") == "template"))
    _, html = phone.open(token)
    assert 'name="pin"' not in html
    new_id = service.ask_again(db, review_id)
    pc["tick"]()
    assert store.get_review(db, new_id)["approval_id"] != review["approval_id"]


def test_a_wrong_pin_publishes_nothing(pc):
    phone, db = pc["phone"], pc["db"]
    job = finished_job(db, pc["dir"], title="Blue vs White")
    review_id = notifier.on_job_finished(db, job, "done")
    pc["tick"]()
    token = phone.link_token(phone.last_message())
    _, html = phone.open(token)
    clip = store.get_review(db, review_id)["clips"][0]["clip_id"]
    status, _ = phone.submit(token, [("csrf", csrf_of(html)), ("action", "approve"), ("clip", str(clip)),
                                     ("platform", "instagram"), ("confirm", "yes"), ("pin", "0000")])
    assert status in (400, 403)
    pc["tick"]()
    assert commands.jobs_for_review(db, review_id) == []


def test_a_failed_job_sends_the_plain_failure_message(pc):
    phone, db = pc["phone"], pc["db"]
    job = finished_job(db, pc["dir"], title="Blue vs Black")
    db.update_job(job["id"], state="failed", error="Out of memory at C:\\Users\\someone\\clips")
    notifier.on_job_finished(db, db.get_job(job["id"]), "failed")
    pc["tick"]()
    message = phone.last_message()
    wording = "Clips Kitty could not finish processing your video. Open the app for details."
    if message["type"] == "template":
        # The approved template carries the wording; nothing from the job goes with it.
        assert message["template"]["name"] == "ck_processing_failed"
        assert message["template"]["components"] == []
        assert wording in (RELAY_DIR / "lib" / "whatsapp.js").read_text(encoding="utf-8")
    else:
        assert message["text"]["body"] == wording
    assert "memory" not in json.dumps(message) and "Users" not in json.dumps(message)


def test_an_undelivered_offer_can_be_sent_again_with_a_new_link(pc):
    phone, db = pc["phone"], pc["db"]
    job = finished_job(db, pc["dir"], title="Blue vs Orange")
    review_id = notifier.on_job_finished(db, job, "done")
    pc["tick"]()
    first = store.get_review(db, review_id)
    sent = phone.last_message()
    old_token = phone.link_token(sent)
    phone.status(sent["wamid"], "failed", 131026)
    pc["tick"]()
    review = store.get_review(db, review_id)
    assert review["state"] == states.NOTIFICATION_PENDING and review["error"]
    assert service.resend(db, review_id)
    pc["tick"]()
    review = store.get_review(db, review_id)
    assert review["state"] == states.NOTIFICATION_SENT
    assert review["approval_id"] != first["approval_id"]
    new_token = phone.link_token(phone.last_message())
    assert new_token != old_token
    _, html = phone.open(old_token)
    assert 'name="pin"' not in html
    _, html = phone.open(new_token)
    assert 'name="pin"' in html


# ---- watching the clips before deciding ---------------------------------------------------


@pytest.mark.media
@pytest.mark.skipif(not ffmpeg_ok(), reason="needs ffmpeg to make the clips' previews")
def test_the_phone_plays_each_clip_before_approving(pc):
    phone, db = pc["phone"], pc["db"]
    job = finished_job(db, pc["dir"], title="Blue vs Purple")
    # Real 12-second videos in place of the clips' random bytes, so the app can make their previews.
    source = make_video(pc["dir"] / "source.mp4", size="360x640", seconds=12)
    for clip in db.clips_for_job(job["id"]):
        shutil.copyfile(source, clip["path"])
    review_id = notifier.on_job_finished(db, job, "done")

    # The offer goes out, then one clip's preview a pass, best clip first.
    pc["tick"]()
    review = store.get_review(db, review_id)
    token = phone.link_token(phone.last_message())
    _, html = phone.open(token)
    assert html.count("<video ") == 1 and "still coming from your PC" in html
    for _ in review["clips"][1:]:
        pc["tick"]()
    sent = [r["status"] for r in store.outbox_for_review(db, review_id) if r["kind"] == "preview"]
    assert sent == ["sent"] * len(review["clips"])
    status, html = phone.open(token)
    assert status == 200 and html.count("<video ") == len(review["clips"])
    assert "still coming" not in html and "<script" not in html
    for c in review["clips"]:
        thumb, video = f"/a/{token}/thumb/{c['clip_id']}", f"/a/{token}/preview/{c['clip_id']}"
        assert f'<video controls preload="none" poster="{thumb}" src="{video}"' in html

    first = review["clips"][0]["clip_id"]
    url = f"{phone.url}/a/{token}/preview/{first}"
    whole = requests.get(url, timeout=10)
    assert (whole.status_code, whole.headers["Content-Type"]) == (200, "video/mp4")
    size = len(whole.content)
    assert 0 < size <= 4 * 1024 * 1024
    # An iPhone plays a video only once its first ask, for two bytes, gets a 206.
    probe = requests.get(url, headers={"Range": "bytes=0-1"}, timeout=10)
    assert (probe.status_code, probe.headers["Content-Range"]) == (206, f"bytes 0-1/{size}")
    assert len(probe.content) == 2
    # Bytes 4 to 7: the type of an MP4's first box.
    assert requests.get(url, headers={"Range": "bytes=4-7"}, timeout=10).content == b"ftyp"

    # Approved: the previews stop playing at once, and the app goes on to publish.
    status, html = phone.submit(token, [("csrf", csrf_of(html)), ("action", "approve"), ("clip", str(first)),
                                        ("platform", "instagram"), ("confirm", "yes"), ("pin", PIN)])
    assert status == 200 and "Waiting for your PC" in html
    assert requests.get(url, headers={"Range": "bytes=0-1"}, timeout=10).status_code == 404
    assert pc["tick"]() == 1
    assert store.get_review(db, review_id)["state"] == states.PUBLISH_QUEUED


# ---- the owner's Muse agent -------------------------------------------------------------


def test_muse_lists_the_offer_hands_over_the_link_and_reads_what_got_posted(pc, relay_url):
    phone, db = pc["phone"], pc["db"]
    muse = Muse(relay_url, service.make_muse_key(db, pc["relay"]))
    hello = muse.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "muse", "version": "1"}})
    assert hello["protocolVersion"] == "2025-06-18" and "own PIN" in hello["instructions"]
    before = len(phone.state()["posts"])
    review_id = notifier.on_job_finished(db, finished_job(db, pc["dir"], title="Home vs Away"), "done")
    pc["tick"]()
    review = store.get_review(db, review_id)

    [offer] = muse.tool("list_ready_highlights")["offers"]
    assert offer["approval_id"] == review["approval_id"] and offer["title"] == "Home vs Away"
    assert [c["clip_id"] for c in offer["clips"]] == [c["clip_id"] for c in review["clips"]]
    link = muse.tool("get_approval_link", approval_id=review["approval_id"])["url"]
    assert link == f"{relay_url}/a/{phone.link_token(phone.last_message())}"  # the link WhatsApp carries

    # The owner opens it and approves there, with the PIN. Muse never sees it.
    token = link.rsplit("/a/", 1)[1]
    _, html = phone.open(token)
    status, html = phone.submit(token, [
        ("csrf", csrf_of(html)), ("action", "approve"), ("clip", str(review["clips"][0]["clip_id"])),
        ("platform", "instagram"), ("platform", "facebook"), ("confirm", "yes"), ("pin", PIN),
    ])
    assert status == 200 and "Waiting for your PC" in html
    assert pc["tick"]() == 1
    assert pc["publish_all"]() == 1
    pc["tick"]()
    assert store.get_review(db, review_id)["state"] == states.PUBLISHED

    posts = phone.state()["posts"][before:]
    [done] = muse.tool("list_recent_posts")["offers"]
    assert done["approval_id"] == review["approval_id"] and done["status"] == "2 of 2 posts published."
    assert sorted(p["url"] for c in done["clips"] for p in c["posts"]) == sorted(p["permalink"] for p in posts)


def test_muse_alone_with_no_whatsapp_number_gets_the_offer_and_its_results(muse_pc):
    phone, muse, db = muse_pc["phone"], muse_pc["muse"], muse_pc["db"]
    messages = len(phone.state()["messages"])
    before = len(phone.state()["posts"])
    review_id = notifier.on_job_finished(db, finished_job(db, muse_pc["dir"], title="Home vs Visitors"), "done")
    assert review_id
    muse_pc["tick"]()
    review = store.get_review(db, review_id)
    assert review["state"] == states.NOTIFICATION_SENT and review["approval_id"].startswith("apr_")

    [offer] = muse.tool("list_ready_highlights")["offers"]
    assert offer["approval_id"] == review["approval_id"] and offer["kind"] == "basketball"
    token = muse.tool("get_approval_link")["url"].rsplit("/a/", 1)[1]
    status, html = phone.open(token)
    assert status == 200 and "Never give it to an assistant." in html
    muse_pc["tick"]()
    assert store.get_review(db, review_id)["state"] == states.AWAITING_APPROVAL  # the page was opened
    status, html = phone.submit(token, [
        ("csrf", csrf_of(html)), ("action", "approve"), ("clip", str(review["clips"][0]["clip_id"])),
        ("platform", "instagram"), ("confirm", "yes"), ("pin", PIN),
    ])
    assert status == 200 and "Waiting for your PC" in html
    assert muse_pc["tick"]() == 1
    assert muse_pc["publish_all"]() == 1
    muse_pc["tick"]()
    assert store.get_review(db, review_id)["state"] == states.PUBLISHED

    [post] = phone.state()["posts"][before:]
    [done] = muse.tool("list_recent_posts")["offers"]
    assert [p["url"] for c in done["clips"] for p in c["posts"]] == [post["permalink"]]
    assert len(phone.state()["messages"]) == messages  # not one WhatsApp message, start to finish


# ---- the whole app: upload to Instagram, through its own routes and worker ------------------


def test_an_uploaded_game_goes_from_the_dashboard_to_the_phone_and_out(pc):
    """The dashboard's upload, the engine (faked) and Muse Spark's captions
    (faked) in the worker's captions stage, the relay loop sending the
    offer, the phone approving with the PIN, and the upload step posting."""
    app, phone = pc["app"], pc["phone"]
    meta = FakeMeta(app.store.ledger())
    app.worker.meta_factory = lambda: meta
    before = len(phone.state()["posts"])
    r = app.post("/api/upload?filename=Final.mp4", content=VIDEO_BYTES + os.urandom(2000),
                 headers={"Content-Type": "application/octet-stream"})
    job_id = r.json()["job_id"]
    assert app.worker.run_next_job() == job_id
    job = app.get(f"/api/jobs/{job_id}").json()
    assert job["stages"]["captions"]["note"] == "Muse Spark wrote the captions. Offered 5 clips on your phone."
    app.worker.wake_relay()
    app.worker.relay_step()
    [review] = app.get("/remote-approval/reviews").json()["reviews"]
    assert review["state"] == states.NOTIFICATION_SENT and review["captions_by"] == "muse-spark-1.3"
    assert review["clips"][0]["thumb_url"].startswith(f"/media/jobs/{job_id}/clips/")
    token = phone.link_token(phone.last_message())
    _, html = phone.open(token)
    assert "Muse: Poster dunk" in html  # Muse Spark's title on the approval page
    first = review["clips"][0]["clip_id"]
    status, html = phone.submit(token, [("csrf", csrf_of(html)), ("action", "approve"), ("clip", str(first)),
                                        ("platform", "instagram"), ("confirm", "yes"), ("pin", PIN)])
    assert status == 200
    app.worker.wake_relay()
    assert app.worker.relay_step() is True  # took the approval, ran the upload
    app.worker.wake_relay()
    app.worker.relay_step()
    review = app.get(f"/remote-approval/reviews/{review['id']}").json()
    assert review["state"] == states.PUBLISHED and review["result_text"] == "1 of 1 post published"
    [post] = phone.state()["posts"][before:]
    assert post["platform"] == "instagram" and "Poster dunk from tonight." in json.dumps(post)
    assert review["results"]["clips"][0]["posts"][0]["url"] == post["permalink"]
