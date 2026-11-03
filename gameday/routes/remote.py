"""The /remote-approval routes: phone approval, Muse, WhatsApp and accounts.

Ported from this week's event branch (server/remote_approval_api.py), with
the same paths and the same JSON in and out. Two things changed (docs/API.md):
the app's own rules (Basic auth, and X-Gameday-Action on every change, in
app.py) replace the desktop session header, and a clip's files are /media
URLs. An error is {"error": "..."} like every other route.

Nothing here listens for the phone: the app only ever calls out to the
owner's relay (remote_approval/relay.py). The reads return no secret and no
phone number (the last four digits only).

The owner's Muse agent talks to the relay, never to this app. The key it uses
is made here (POST /remote-approval/muse/token) and passes through to the
dashboard once: it is not stored, logged or put in the outbox, and no other
route returns it.
"""

from __future__ import annotations

from fastapi import HTTPException
from pydantic import BaseModel

from gameday.remote_approval import service, states, store, usage
from gameday.remote_approval import settings as ra_settings
from gameday.remote_approval.relay import RelayError


class SettingsPatch(BaseModel):
    whatsapp_enabled: bool | None = None
    relay_url: str | None = None
    notify_on_finish: bool | None = None
    notify_on_fail: bool | None = None
    notify_on_publish: bool | None = None
    remote_approvals: bool | None = None
    preferred_destinations: list[str] | None = None
    approval_hours: int | None = None
    remind_minutes: int | None = None
    max_clips: int | None = None
    basketball_only: bool | None = None
    muse_captions: bool | None = None
    muse_summary: bool | None = None
    muse_model: str | None = None


class RegisterIn(BaseModel):
    relay_url: str = ""
    enroll_key: str = ""


class ConfirmIn(BaseModel):
    confirm: bool


class PinIn(BaseModel):
    pin: str


class PlatformIn(BaseModel):
    platform: str


class TemplatesIn(BaseModel):
    create: bool = False


def _fail(e: RelayError) -> HTTPException:
    """The relay's refusal, in its own words (it writes them for the owner).
    A refusal of this app's signature (401) is the relay's problem, not the
    dashboard's sign-in: it is answered as a bad gateway."""
    return HTTPException(e.status if e.status in (400, 403, 404, 409, 413, 429) else 502, e.message)


def install(app, *, store_db, config, worker, meta_factory) -> None:
    """Add the /remote-approval routes over `store_db` (gameday.store.Store)."""
    d = store_db

    def changed(what: str) -> None:
        if worker is not None:
            worker.wake_relay()  # the next relay pass runs now, not in two minutes

    def relay_for(db):
        relay = service.client(db)
        if relay is None:
            raise HTTPException(409, "Connect this app to your relay first.")
        return relay

    def status(db) -> dict:
        view = ra_settings.public_view(db)
        open_states = [states.NOTIFICATION_PENDING, *states.OPEN, states.APPROVED, states.PUBLISH_QUEUED,
                       states.PUBLISHING]
        return {
            "settings": view,
            "can_enable_remote_approval": service.can_enable_remote_approval(db),
            "pending": [service.review_view(db, r) for r in store.reviews_in(db, open_states)],
            "outbox_pending": store.pending_outbox(db),
            "usage": usage.summary(db),
            # RELAY_URL and RELAY_ENROLMENT_KEY fill in the relay form: the
            # address itself, and only whether the key is set.
            "env": {"relay_url": config.relay_url, "enrolment_key": bool(config.relay_enrolment_key)},
        }

    @app.get("/remote-approval/status")
    def remote_status():
        return status(d)

    @app.patch("/remote-approval/settings")
    def remote_settings(body: SettingsPatch):
        patch = body.model_dump(exclude_none=True)
        wants_approvals = patch.pop("remote_approvals", None)
        try:
            ra_settings.save(d, patch)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        if wants_approvals:
            why = service.can_enable_remote_approval(d)
            if why:
                raise HTTPException(409, why)
        if wants_approvals is not None:
            ra_settings.save(d, {"remote_approvals": bool(wants_approvals)})
        relay_error = ""
        relay = service.client(d)
        if relay is not None:
            try:
                service.push_preferences(d, relay)
            except RelayError as e:
                relay_error = e.message
        out = {**status(d), "relay_error": relay_error}
        changed("settings")
        return out

    @app.post("/remote-approval/register")
    def remote_register(body: RegisterIn):
        url = body.relay_url.strip() or ra_settings.load(d)["relay_url"] or config.relay_url
        key = body.enroll_key.strip() or config.relay_enrolment_key
        if not key:
            raise HTTPException(400, "Enter your relay's enrolment key.")
        if url:
            try:
                ra_settings.save(d, {"relay_url": url})
            except ValueError as e:
                raise HTTPException(400, str(e)) from None
        try:
            service.register(d, key)
        except RelayError as e:
            raise _fail(e) from None
        out = status(d)
        changed("registered")
        return out

    @app.post("/remote-approval/refresh")
    def remote_refresh():
        try:
            service.refresh_status(d, relay_for(d))
        except RelayError as e:
            raise _fail(e) from None
        return status(d)

    @app.post("/remote-approval/pair/start")
    def remote_pair_start():
        """A six-character code, and a wa.me link that opens WhatsApp with it
        typed. The owner sends it from the phone that should get messages."""
        try:
            got = relay_for(d).pair_start()
        except RelayError as e:
            raise _fail(e) from None
        return {k: got.get(k, "") for k in ("code", "text", "link", "number", "expires_at")}

    @app.post("/remote-approval/pair/confirm")
    def remote_pair_confirm(body: ConfirmIn):
        """The app confirms the number ending in these four digits is the
        owner's. A code alone is never enough: a number that sent it is only
        paired once confirmed here."""
        try:
            relay = relay_for(d)
            relay.pair_confirm(body.confirm)
            service.refresh_status(d, relay)
        except RelayError as e:
            raise _fail(e) from None
        if not body.confirm:
            ra_settings.save(d, {"remote_approvals": False})
        out = status(d)
        changed("pairing")
        return out

    @app.post("/remote-approval/pin")
    def remote_pin(body: PinIn):
        """The approval PIN the phone's page asks for. Sent to the relay,
        which keeps only a slow hash of it; never stored in this app."""
        try:
            relay = relay_for(d)
            relay.set_pin(body.pin.strip())
            service.refresh_status(d, relay)
        except RelayError as e:
            raise _fail(e) from None
        out = status(d)
        changed("pin")
        return out

    def muse_fail(e: RelayError) -> HTTPException:
        if e.status == 404:  # a relay deployed before the Muse connector
            return HTTPException(409, "Your relay is older than this version of Clips Kitty. Deploy it again to "
                                      "connect Muse.")
        return _fail(e)

    @app.post("/remote-approval/muse/token")
    def remote_muse_token():
        """A new key for the owner's Muse agent, replacing any older one. In
        this answer once, for the dashboard to show; the relay keeps only its
        hash and this app keeps nothing."""
        relay = relay_for(d)
        try:
            key = service.make_muse_key(d, relay)
        except RelayError as e:
            raise muse_fail(e) from None
        try:
            # Whether WhatsApp is wanted at all decides where offers go now
            # that Muse can carry them.
            service.push_preferences(d, relay)
        except RelayError:
            pass  # the key is made; the next settings change sends these again
        out = {**status(d), "key": key}
        changed("muse")
        return out

    @app.post("/remote-approval/muse/revoke")
    def remote_muse_revoke():
        relay = relay_for(d)
        try:
            service.revoke_muse_key(d, relay)
        except RelayError as e:
            raise muse_fail(e) from None
        try:
            service.push_preferences(d, relay)
        except RelayError:
            # The key has stopped working, which is what was asked. An error
            # here would say it hadn't; the next settings change sends these again.
            pass
        out = status(d)
        changed("muse")
        return out

    @app.post("/remote-approval/test")
    def remote_test():
        if not ra_settings.load(d)["whatsapp_enabled"]:
            raise HTTPException(409, "Turn on WhatsApp notifications first.")
        service.send_test(d)
        changed("test")
        return {"queued": True}

    @app.post("/remote-approval/disconnect")
    def remote_disconnect():
        service.disconnect(d)
        out = status(d)
        changed("disconnected")
        return out

    @app.post("/remote-approval/accounts/connect")
    def remote_accounts_connect(body: PlatformIn):
        """The Meta sign-in page for Facebook (with its Instagram), on the
        relay, which keeps the token. The dashboard opens it in the browser."""
        if body.platform != "facebook":
            raise HTTPException(400, "Connect Facebook: it brings its Instagram account.")
        try:
            got = relay_for(d).accounts_connect(body.platform)
        except RelayError as e:
            raise _fail(e) from None
        return {"url": got.get("url", "")}

    @app.post("/remote-approval/accounts/disconnect")
    def remote_accounts_disconnect(body: PlatformIn):
        if body.platform not in ra_settings.PLATFORMS:
            raise HTTPException(400, "Unknown platform.")
        try:
            relay = relay_for(d)
            relay.accounts_disconnect(body.platform)
            service.refresh_status(d, relay)
        except RelayError as e:
            raise _fail(e) from None
        config_now = ra_settings.load(d)
        ra_settings.save(d, {"preferred_destinations": [p for p in config_now["preferred_destinations"]
                                                        if (config_now["accounts"].get(p) or {}).get("connected")]})
        out = status(d)
        changed("accounts")
        return out

    @app.post("/remote-approval/templates")
    def remote_templates(body: TemplatesIn):
        """The WhatsApp message templates' review status, or submit them to
        Meta for review (create)."""
        try:
            relay = relay_for(d)
            got = relay.templates(create=body.create)
            service.refresh_status(d, relay)
        except RelayError as e:
            raise _fail(e) from None
        return got

    @app.get("/remote-approval/reviews")
    def remote_reviews(limit: int = 20):
        return {"reviews": [service.review_view(d, r) for r in store.recent_reviews(d, max(1, min(limit, 100)))]}

    @app.get("/remote-approval/reviews/{review_id}")
    def remote_review(review_id: int):
        review = store.get_review(d, review_id)
        if review is None:
            raise HTTPException(404, "No such offer.")
        return service.review_view(d, review, detail=True)

    def act(review_id: int, what: str):
        review = store.get_review(d, review_id)
        if review is None:
            raise HTTPException(404, "No such offer.")
        try:
            if what == "cancel":
                service.cancel_from_app(d, review_id)
                result = {}
            elif what == "retry":
                result = {"queued": service.retry_failed(d, review_id)}
                if not result["queued"]:
                    # A post that failed is not sent again once it is up, or
                    # on its way, from somewhere else (commands.queue_publishes).
                    failed = any(p.get("state") == "failed"
                                 for c in (review.get("results") or {}).get("clips") or []
                                 for p in c.get("posts") or [])
                    raise HTTPException(409, "What failed is already posted, on its way or uploading now, "
                                             "so there is nothing to retry." if failed else
                                             "Nothing in this offer failed, so there is nothing to retry.")
            elif what == "ask-again":
                if review["state"] not in (states.EXPIRED, states.CANCELLED):
                    raise HTTPException(409, "Only an expired or cancelled offer can be sent again.")
                new_id = service.ask_again(d, review_id, meta=_meta(meta_factory))
                if not new_id:
                    # Nothing of its job is left to offer (notifier.offer).
                    raise HTTPException(409, "None of this video's clips can be offered again: they were "
                                             "removed or made again since, or are posted everywhere already.")
                result = {"review_id": new_id}
            else:
                if not service.resend(d, review_id):
                    raise HTTPException(409, "This offer's message is not waiting to be sent again.")
                result = {}
        except states.IllegalMove:
            raise HTTPException(409, "An upload from this offer has already started, so it can't be cancelled "
                                     "now. Its result will be shown when it finishes.") from None
        out = {**result, "review": service.review_view(d, store.get_review(d, review_id))}
        changed(what)
        return out

    @app.post("/remote-approval/reviews/{review_id}/cancel")
    def remote_review_cancel(review_id: int):
        return act(review_id, "cancel")

    @app.post("/remote-approval/reviews/{review_id}/retry")
    def remote_review_retry(review_id: int):
        return act(review_id, "retry")

    @app.post("/remote-approval/reviews/{review_id}/ask-again")
    def remote_review_ask_again(review_id: int):
        return act(review_id, "ask-again")

    @app.post("/remote-approval/reviews/{review_id}/resend")
    def remote_review_resend(review_id: int):
        return act(review_id, "resend")

    @app.get("/remote-approval/audit")
    def remote_audit(limit: int = 100):
        return {"audit": store.audit_log(d, None, max(1, min(limit, 500)))}


def _meta(factory):
    """Muse Spark for an offer made again from the dashboard, or None."""
    try:
        return factory() if factory is not None else None
    except Exception:
        return None
