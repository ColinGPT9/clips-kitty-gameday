"""The /remote-approval routes (routes/remote.py): the action header on every
route that changes something, nothing secret in what they return, remote
approval only after pairing and a PIN, and the whole set-up against the real
relay code (meta-relay/test/serve.mjs, Meta faked) when Node is installed.

Ported from this week's event branch (tests/test_remote_approval_api.py):
the app's sign-in and X-Gameday-Action replace the desktop session header,
and errors are {"error": ...}.
"""

import json

import pytest
from conftest import ENROLL_KEY
from fastapi.testclient import TestClient
from platform_fakes import SECRET as DEVICE_SECRET
from platform_fakes import FakeRelay, Phone, finished_job, node_ok, sign_command, switch_on

from gameday.remote_approval import commands, notifier, service, states, store
from gameday.remote_approval import settings as ra_settings
from gameday.remote_approval.relay import RelayError

CHANGING = [
    ("PATCH", "/remote-approval/settings", {"whatsapp_enabled": True}),
    ("POST", "/remote-approval/register", {"enroll_key": "x"}),
    ("POST", "/remote-approval/refresh", None),
    ("POST", "/remote-approval/pair/start", None),
    ("POST", "/remote-approval/pair/confirm", {"confirm": True}),
    ("POST", "/remote-approval/pin", {"pin": "4826"}),
    ("POST", "/remote-approval/test", None),
    ("POST", "/remote-approval/disconnect", None),
    ("POST", "/remote-approval/accounts/connect", {"platform": "facebook"}),
    ("POST", "/remote-approval/accounts/disconnect", {"platform": "facebook"}),
    ("POST", "/remote-approval/templates", {"create": True}),
    ("POST", "/remote-approval/reviews/1/cancel", None),
    ("POST", "/remote-approval/reviews/1/retry", None),
    ("POST", "/remote-approval/reviews/1/ask-again", None),
    ("POST", "/remote-approval/reviews/1/resend", None),
    ("POST", "/remote-approval/muse/token", None),
    ("POST", "/remote-approval/muse/revoke", None),
]


@pytest.fixture
def woken(gd, monkeypatch):
    """Each time a route asks the relay loop to run now."""
    seen = []
    monkeypatch.setattr(gd.worker, "wake_relay", lambda: seen.append(True))
    return seen


@pytest.mark.parametrize("method, path, body", CHANGING)
def test_every_changing_route_needs_the_session_header(gd, method, path, body):
    r = gd.client.request(method, path, json=body)
    assert r.status_code == 403
    r = gd.client.request(method, path, json=body, headers={"X-Gameday-Action": "wrong"})
    assert r.status_code == 403
    anonymous = TestClient(gd.app, base_url="http://127.0.0.1")
    assert anonymous.request(method, path, json=body, headers={"X-Gameday-Action": "1"}).status_code == 401


def test_reads_need_no_header_and_hold_no_secret(gd):
    gd.store.secrets.save_json(ra_settings.DEVICE_SECRET, {"device_id": "dev_0123456789abcdef",
                                                           "device_secret": DEVICE_SECRET})
    ra_settings.save(gd.store, {"pairing": {"state": "confirmed", "tail": "4567"}}, internal=True)
    for path in ("/remote-approval/status", "/remote-approval/reviews", "/remote-approval/audit"):
        r = gd.get(path)
        assert r.status_code == 200, path
        assert DEVICE_SECRET not in r.text
    status = gd.get("/remote-approval/status").json()
    assert status["settings"]["registered"] is True
    assert status["settings"]["pairing"] == {"state": "confirmed", "tail": "4567"}
    assert status["settings"]["remote_approvals"] is False  # off by default


def test_remote_approval_is_refused_until_paired_with_a_pin(gd):
    r = gd.patch("/remote-approval/settings", json={"whatsapp_enabled": True, "remote_approvals": True})
    assert r.status_code == 409 and "Pair" in r.json()["error"]
    status = gd.get("/remote-approval/status").json()
    assert status["settings"]["whatsapp_enabled"] is True  # the rest of the change was kept
    assert status["settings"]["remote_approvals"] is False


def test_bad_settings_are_refused(gd):
    r = gd.patch("/remote-approval/settings", json={"relay_url": "http://relay.example.com"})
    assert r.status_code == 400 and r.json()["error"] == "The relay address must start with https://"
    r = gd.patch("/remote-approval/settings", json={"preferred_destinations": ["instagram", "tiktok"],
                                                    "approval_hours": 2})
    assert r.json()["settings"]["preferred_destinations"] == ["instagram"]
    r = gd.patch("/remote-approval/settings", json={"approval_hours": "soon"})
    assert r.status_code == 400 and set(r.json()) == {"error"}


def test_the_gaming_opt_in_is_saved_from_settings(gd):
    assert gd.get("/remote-approval/status").json()["settings"]["basketball_only"] is True
    r = gd.patch("/remote-approval/settings", json={"basketball_only": False})
    assert r.status_code == 200 and r.json()["settings"]["basketball_only"] is False
    assert gd.get("/remote-approval/status").json()["settings"]["basketball_only"] is False
    r = gd.patch("/remote-approval/settings", json={"basketball_only": True})
    assert r.json()["settings"]["basketball_only"] is True


def test_routes_that_need_the_relay_say_so_before_it_is_set_up(gd):
    r = gd.post("/remote-approval/pair/start")
    assert r.status_code == 409 and "relay" in r.json()["error"]
    r = gd.post("/remote-approval/test")
    assert r.status_code == 409
    r = gd.post("/remote-approval/register", json={"relay_url": "https://relay.test"})
    assert r.status_code == 400 and r.json()["error"] == "Enter your relay's enrolment key."


def test_the_environment_fills_in_the_relay_form(make_app):
    app = make_app(RELAY_URL="https://relay.example.workers.dev", RELAY_ENROLMENT_KEY="enrol-0123456789")
    env = app.get("/remote-approval/status").json()["env"]
    assert env == {"relay_url": "https://relay.example.workers.dev", "enrolment_key": True}
    assert "enrol-0123456789" not in app.get("/remote-approval/status").text


def test_offers_can_be_cancelled_resent_and_asked_again_from_the_app(gd, monkeypatch, woken):
    switch_on(gd.store)
    relay = FakeRelay()
    monkeypatch.setattr(service, "client", lambda db: relay)
    job = finished_job(gd.store, gd.config.data_dir)
    review_id = notifier.on_job_finished(gd.store, job, "done")
    service.flush_outbox(gd.store, relay)

    listed = gd.get("/remote-approval/reviews").json()["reviews"]
    assert listed[0]["state"] == states.NOTIFICATION_SENT and listed[0]["can_cancel"]
    assert '"path"' not in json.dumps(listed) and str(gd.config.data_dir) not in json.dumps(listed)
    assert listed[0]["clips"][0]["video_url"] == f"/media/jobs/{job['id']}/clips/clip_01.mp4"
    one = gd.get(f"/remote-approval/reviews/{review_id}").json()
    assert one["audit"] and one["messages"][0]["status"] == "sent"

    r = gd.post(f"/remote-approval/reviews/{review_id}/resend")
    assert r.status_code == 409  # it was delivered: nothing to send again

    r = gd.post(f"/remote-approval/reviews/{review_id}/cancel")
    assert r.status_code == 200 and r.json()["review"]["state"] == states.CANCELLED
    assert ("cancel", store.get_review(gd.store, review_id)["approval_id"]) in relay.calls

    r = gd.post(f"/remote-approval/reviews/{review_id}/ask-again")
    assert r.status_code == 200
    new = r.json()["review_id"]
    assert gd.get(f"/remote-approval/reviews/{new}").json()["state"] == states.NOTIFICATION_PENDING
    assert woken  # the relay loop was asked to run now
    assert gd.get("/remote-approval/reviews/999").status_code == 404


def test_asking_again_says_so_when_nothing_is_left_to_offer(gd):
    ra_settings.save(gd.store, {"whatsapp_enabled": True, "relay_url": "https://relay.test"})
    job = finished_job(gd.store, gd.config.data_dir)
    review_id = notifier.on_job_finished(gd.store, job, "done")
    r = gd.post(f"/remote-approval/reviews/{review_id}/ask-again")
    assert r.status_code == 409 and r.json()["error"] == "Only an expired or cancelled offer can be sent again."

    for clip in gd.store.clips_for_job(job["id"]):  # every clip posted everywhere since
        for platform in ra_settings.PLATFORMS:
            gd.store.record_clip_publish(int(clip["id"]), platform, {"provider": "meta", "state": "published",
                                                                     "request_id": "apr_other"})
    store.move(gd.store, review_id, states.EXPIRED, actor="test")
    r = gd.post(f"/remote-approval/reviews/{review_id}/ask-again")
    assert r.status_code == 409
    assert r.json()["error"] == ("None of this video's clips can be offered again: they were removed or made "
                                 "again since, or are posted everywhere already.")


def test_a_retry_says_so_when_what_failed_went_up_from_somewhere_else(gd):
    d = gd.store
    switch_on(d)
    review_id = notifier.on_job_finished(d, finished_job(d, gd.config.data_dir), "done")
    service.flush_outbox(d, FakeRelay())
    review = store.get_review(d, review_id)
    clip = review["clips"][0]["clip_id"]
    out = commands.handle(d, sign_command({
        "id": "cmd_" + "e" * 24, "kind": "approve", "approval_id": review["approval_id"], "review_ref": review_id,
        "fingerprint": review["fingerprint"], "clips": [clip], "platforms": ["instagram"],
        "decided_at": store.stamp(),
    }), secret=DEVICE_SECRET)
    assert out.outcome == "accepted"
    d.record_clip_publish(clip, "instagram", {"provider": "meta", "state": "failed", "error": "Refused.",
                                              "request_id": review["approval_id"]})
    d.finish_publish_job(commands.jobs_for_review(d, review_id)[0]["id"], "failed")
    service.settle_publishing(d, ra_settings.load(d))
    assert store.get_review(d, review_id)["state"] == states.FAILED
    # Posted since, by another offer of the same clip.
    d.record_clip_publish(clip, "instagram", {"provider": "meta", "state": "published", "request_id": "apr_other"})
    r = gd.post(f"/remote-approval/reviews/{review_id}/retry")
    assert r.status_code == 409
    assert r.json()["error"] == ("What failed is already posted, on its way or uploading now, so there is "
                                 "nothing to retry.")


def _registered(gd, monkeypatch):
    """This app registered with a fake relay, nothing else set up."""
    gd.store.secrets.save_json(ra_settings.DEVICE_SECRET, {"device_id": "dev_0123456789abcdef",
                                                           "device_secret": DEVICE_SECRET})
    relay = FakeRelay()
    monkeypatch.setattr(service, "client", lambda db: relay)
    ra_settings.save(gd.store, {"relay_url": "https://relay.test"})
    return relay


def test_a_muse_key_is_shown_once_and_kept_nowhere_on_this_pc(gd, monkeypatch, caplog):
    relay = _registered(gd, monkeypatch)
    with caplog.at_level("DEBUG"):
        r = gd.post("/remote-approval/muse/token")
    assert r.status_code == 200
    key = r.json()["key"]
    assert key == relay.keys[0] and r.json()["settings"]["muse"]["enabled"] is True
    assert any(c[0] == "preferences" and c[1]["whatsapp_enabled"] is False for c in relay.calls)
    # Not in any answer after this one, the database, a file, an event or the log.
    for path in ("/remote-approval/status", "/remote-approval/reviews", "/remote-approval/audit", "/api/events"):
        assert key not in gd.get(path).text, path
    for f in gd.config.data_dir.rglob("*"):
        if f.is_file():
            assert key.encode() not in f.read_bytes(), f.name
    assert key not in caplog.text

    status = gd.patch("/remote-approval/settings", json={"remote_approvals": True})
    assert status.status_code == 409 and "PIN" in status.json()["error"]  # Muse stands in for WhatsApp, not the PIN
    ra_settings.save(gd.store, {"pin_set": True}, internal=True)
    status = gd.patch("/remote-approval/settings", json={"remote_approvals": True})
    assert status.status_code == 200 and status.json()["settings"]["remote_approvals"] is True

    r = gd.post("/remote-approval/muse/revoke")
    assert r.status_code == 200
    assert r.json()["settings"]["muse"]["enabled"] is False
    assert r.json()["settings"]["remote_approvals"] is False  # no WhatsApp either: nothing reaches the phone


def test_revoking_says_whether_the_key_stopped_working_not_whether_the_preferences_followed(gd, monkeypatch):
    relay = _registered(gd, monkeypatch)
    assert gd.post("/remote-approval/muse/token").status_code == 200
    # The relay can't be reached: the key still works, and the dashboard says so.
    relay.fail["muse_revoke"] = RelayError("network", "Couldn't reach your relay.", retryable=True)
    r = gd.post("/remote-approval/muse/revoke")
    assert r.status_code == 502 and r.json()["error"] == "Couldn't reach your relay."
    assert gd.get("/remote-approval/status").json()["settings"]["muse"]["enabled"] is True
    # The key stopped working but the preferences didn't follow: that is no failure to report.
    relay.fail["preferences"] = RelayError("network", "Couldn't reach your relay.", retryable=True)
    r = gd.post("/remote-approval/muse/revoke")
    assert r.status_code == 200
    assert relay.muse["enabled"] is False and r.json()["settings"]["muse"]["enabled"] is False


def test_a_relay_from_before_muse_says_to_deploy_it_again(gd, monkeypatch):
    relay = _registered(gd, monkeypatch)
    relay.fail["muse_token"] = RelayError("not_found", "Unknown relay route.", status=404)
    r = gd.post("/remote-approval/muse/token")
    assert r.status_code == 409
    assert r.json()["error"] == ("Your relay is older than this version of Clips Kitty. Deploy it again to "
                                 "connect Muse.")


@pytest.mark.skipif(not node_ok(), reason="needs Node 22.5+ for the relay")
def test_settings_set_up_against_the_real_relay(gd, relay_url):
    phone = Phone(relay_url)
    r = gd.post("/remote-approval/register", json={"relay_url": relay_url, "enroll_key": "wrong-key"})
    assert r.status_code in (403, 502)
    assert gd.get("/remote-approval/status").json()["settings"]["registered"] is False

    r = gd.post("/remote-approval/register", json={"relay_url": relay_url, "enroll_key": ENROLL_KEY})
    assert r.status_code == 200 and r.json()["settings"]["registered"] is True

    code = gd.post("/remote-approval/pair/start").json()
    assert code["text"] == f"LINK {code['code']}"
    phone.send(code["text"])
    status = gd.post("/remote-approval/refresh").json()
    assert status["settings"]["pairing"]["state"] == "awaiting_confirm"
    tail = status["settings"]["pairing"]["tail"]
    assert len(tail) == 4 and phone.state()["phone"].endswith(tail)

    status = gd.post("/remote-approval/pair/confirm", json={"confirm": True}).json()
    assert status["settings"]["pairing"]["state"] == "confirmed"
    assert "notifications from Clips Kitty are on" in json.dumps(phone.last_message())

    assert gd.post("/remote-approval/pin", json={"pin": "1234"}).status_code == 400
    status = gd.post("/remote-approval/pin", json={"pin": "4826"}).json()
    assert status["settings"]["pin_set"] is True

    url = gd.post("/remote-approval/accounts/connect", json={"platform": "facebook"}).json()["url"]
    assert url.startswith("https://") and "client_id" in url
    assert phone.oauth(url)["ok"]
    status = gd.post("/remote-approval/refresh").json()
    assert status["settings"]["accounts"]["instagram"]["connected"] is True
    assert status["settings"]["accounts"]["facebook"]["name"] == "Hoops Daily"  # the mock relay's page

    r = gd.patch("/remote-approval/settings", json={"whatsapp_enabled": True, "remote_approvals": True,
                                                    "preferred_destinations": ["instagram"]})
    assert r.status_code == 200 and r.json()["relay_error"] == ""
    assert r.json()["settings"]["remote_approvals"] is True

    assert gd.post("/remote-approval/test").json() == {"queued": True}
    service.tick(gd.store, service.TickState())
    assert "test" in json.dumps(phone.last_message()).lower()

    status = gd.post("/remote-approval/disconnect").json()
    assert status["settings"]["registered"] is False
    assert status["settings"]["whatsapp_enabled"] is False and status["settings"]["remote_approvals"] is False
