"""Instagram Reels and Facebook Page Reels, through the creator's relay.

Ported from this week's event branch (publish/meta_graph.py). Every call here
is one Graph API step, made by the relay (meta-relay/worker.js, /v1/meta/*)
with the token it holds for the account the creator connected. This app never
sees that token, the app secret, or even the account ids: it names the
approved command, the clip and the step, and the relay refuses any step
outside what the phone approved.

The steps are Meta's own, in Meta's order:

    Instagram  POST /{ig-user}/media (REELS, resumable) -> bytes to rupload
               -> poll the container until FINISHED -> /media_publish
               -> read the permalink back
    Facebook   POST /{page}/video_reels start -> bytes to rupload
               -> finish (PUBLISHED) -> poll status -> read the permalink back

A post counts as published only when the API has said so and handed back
its id; the link shown to the creator is the permalink the API returned,
never one built by hand from an id. Anything less is reported as what it
is (processing, unknown, failed), and the creator is told to look.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from gameday.publish.errors import (
    AuthRequired,
    PublishCancelled,
    PublishError,
    RateLimited,
    UploadLimitExceeded,
    VideoRejected,
)

#: Cloudflare refuses a request body over 100 MB on the Free and Pro plans,
#: and every byte of a clip passes through the relay in one request.
MAX_UPLOAD_BYTES = 95 * 1024 * 1024

# Instagram caption: 2,200 characters and 30 hashtags.
IG_CAPTION_MAX = 2200
IG_HASHTAGS_MAX = 30
FB_TITLE_MAX = 100
FB_DESCRIPTION_MAX = 2000

#: Seconds between status reads, then the last value repeats until the
#: deadline. Short reels are usually ready in under a minute.
POLL_STEPS = (5, 5, 10, 10, 15, 20, 30)
POLL_DEADLINE = 10 * 60
#: How long a Facebook Reel whose finish was answered with an error is
#: watched for having gone up anyway, before that error is believed.
FINISH_CHECK = 2 * 60


@dataclass
class Posted:
    platform: str
    post_id: str = ""
    url: str = ""
    state: str = "published"  # published | processing
    warnings: list[str] = field(default_factory=list)


def classify(code: str, message: str, meta: dict | None = None) -> PublishError:
    """A relay or Graph refusal as a PublishError a creator can act on.

    The relay passes Meta's numeric code and subcode through untouched
    (meta_code, meta_subcode) and writes `message` itself; Meta's own text
    stays on the relay, where it can hold an id or a token fragment."""
    meta = meta or {}
    m_code = int(meta.get("meta_code") or 0)
    sub = int(meta.get("meta_subcode") or 0)
    platform = str(meta.get("platform") or "Meta").title()
    if code in ("not_approved", "expired", "not_connected", "not_registered"):
        return PublishError(message)
    if m_code == 190 or code == "token_expired":
        return AuthRequired(f"Your {platform} connection has expired. Reconnect it on the dashboard's phone page.")
    if m_code == 10 or 200 <= m_code <= 299:
        return AuthRequired(f"{platform} refused the permission this needs. Reconnect the account and "
                            "allow publishing when Meta asks.")
    if m_code in (4, 17, 32, 613) or code == "rate_limited":
        return RateLimited(f"{platform} asked us to slow down. Try again in a few minutes.")
    if sub == 2207042 or m_code == 9:
        return UploadLimitExceeded(f"{platform}'s daily publishing limit for this account is reached. "
                                   "It resets within 24 hours.")
    if sub in (2207026, 2207052) or m_code == 352:
        return VideoRejected(f"{platform} could not use this video's format.")
    if sub in (2207050, 2207051) or m_code == 368:
        return PublishError(f"{platform} is limiting this account right now, so nothing was posted. "
                            "Check the account in the app.")
    text = message or f"{platform} refused the post."
    if m_code or sub:
        # Meta's numbers, never its text: enough to look the refusal up.
        text = f"{text.rstrip('.')} (Meta error {m_code}{f', subcode {sub}' if sub else ''})."
    return PublishError(text)


class MetaGraph:
    """Publishes one clip through the relay. `ctx` names the approved command,
    the offer and the clip; the relay checks every step against it."""

    def __init__(self, relay, *, sleep=time.sleep, clock=time.monotonic):
        self.relay = relay
        self.sleep = sleep
        self.clock = clock

    def _step(self, op: str, ctx: dict, **params) -> dict:
        from gameday.remote_approval.relay import RelayError

        try:
            return self.relay.meta(op, {**ctx, **params})
        except RelayError as e:
            err = classify(e.code, e.message, getattr(e, "meta", None))
            # No answer, or the relay's 5xx for Meta's passing trouble: the
            # step may have been done all the same, so it isn't a refusal.
            err.unanswered = e.code == "network" or e.status >= 500
            raise err from None

    def _upload(self, ctx: dict, platform: str, ref: str, path: Path, size: int, on_progress) -> None:
        from gameday.remote_approval.relay import RelayError

        query = {**{k: str(v) for k, v in ctx.items()}, "platform": platform, "ref": ref,
                 "offset": "0", "file_size": str(size)}
        if on_progress:
            on_progress(f"Uploading to {platform.title()}", 0, size)
        try:
            self.relay.upload_chunk(query, path, 0, size)
        except RelayError as e:
            raise classify(e.code, e.message, getattr(e, "meta", None)) from None
        if on_progress:
            on_progress(f"Uploaded to {platform.title()}", size, size)

    def _poll(self, check, should_cancel=None, deadline: float = POLL_DEADLINE) -> dict:
        """Ask until `check` says done (returns a dict), giving up at the
        deadline. Raises whatever check raises (a processing error)."""
        started = self.clock()
        i = 0
        while True:
            got = check()
            if got is not None:
                return got
            if self.clock() - started > deadline:
                return {}
            if should_cancel and should_cancel():
                raise PublishCancelled("Cancelled.")
            self.sleep(POLL_STEPS[min(i, len(POLL_STEPS) - 1)])
            i += 1

    @staticmethod
    def _size(path: Path) -> int:
        size = path.stat().st_size
        if size > MAX_UPLOAD_BYTES:
            raise PublishError(f"This clip is {size // (1024 * 1024)} MB, over the "
                               f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB a relay request can carry. "
                               "Export it smaller and approve again.")
        if size < 1024:
            raise PublishError("This clip's file is empty or unreadable.")
        return size

    # ---- Instagram ---------------------------------------------------------------

    def instagram(self, ctx: dict, path: Path, caption: str, *, on_progress=None,
                  should_cancel=None) -> Posted:
        size = self._size(path)
        made = self._step("ig_create", ctx, caption=fit_caption(caption), file_size=size)
        container = str(made.get("container_id") or "")
        if not container:
            raise PublishError("Instagram did not open an upload for this clip.")
        self._upload(ctx, "instagram", container, path, size, on_progress)

        def ready():
            status = self._step("ig_status", ctx, container_id=container)
            code = str(status.get("status_code") or "").upper()
            if code == "FINISHED":
                return status
            if code in ("ERROR", "EXPIRED"):
                raise VideoRejected("Instagram could not process this video"
                                    + (f" ({status['status']})." if status.get("status") else "."))
            if code == "PUBLISHED":
                return status
            return None

        if not self._poll(ready, should_cancel):
            return Posted("instagram", state="processing", warnings=[
                "Instagram is still processing this video, so it was not published yet. "
                "Approve it again later if it does not appear."])
        published = self._step("ig_publish", ctx, container_id=container)
        media_id = str(published.get("media_id") or "")
        if not media_id:
            raise PublishError("Instagram did not confirm the post. Check the account before trying again.")
        url = self._link_of_live_post("ig_permalink", ctx, media_id)
        return Posted("instagram", post_id=media_id, url=url,
                      warnings=[] if url else ["Instagram confirmed the post but did not return its link yet."])

    def _link_of_live_post(self, op: str, ctx: dict, media_id: str) -> str:
        """The permalink of a post the API has already confirmed with its id.
        The post is up whatever this read says, so a refusal here costs only
        the link, never a report that the post failed."""
        try:
            return str(self._step(op, ctx, media_id=media_id).get("permalink") or "")
        except PublishError:
            return ""

    # ---- Facebook ------------------------------------------------------------------

    def facebook(self, ctx: dict, path: Path, title: str, description: str, *, on_progress=None,
                 should_cancel=None) -> Posted:
        size = self._size(path)
        started = self._step("fb_start", ctx, file_size=size)
        video_id = str(started.get("video_id") or "")
        if not video_id:
            raise PublishError("Facebook did not open an upload for this clip.")
        self._upload(ctx, "facebook", video_id, path, size, on_progress)
        # On November 1, 2026 a Reel went up while the PC was told Facebook
        # refused: either the finish or the first status read after it, which
        # one is not known. So an error at the finish is checked against the
        # Reel's own status before it is called a failure, and a lost answer
        # is never one. A refusal that says nothing was taken (a permission,
        # the daily limit, the format) is believed at once.
        finish_error = None
        try:
            self._step("fb_finish", ctx, video_id=video_id, title=title[:FB_TITLE_MAX],
                       description=description[:FB_DESCRIPTION_MAX])
        except (AuthRequired, UploadLimitExceeded, VideoRejected):
            raise
        except PublishError as e:
            finish_error = e
        lost = (bool(finish_error) and getattr(finish_error, "unanswered", False)
                and not isinstance(finish_error, RateLimited))
        answered = False

        def live():
            nonlocal answered
            # Straight after the finish Facebook may refuse to show the Reel
            # it is publishing: a refused read is asked again, not a failure.
            try:
                status = self._step("fb_status", ctx, video_id=video_id)
            except PublishError:
                return None
            answered = True
            state = str(status.get("video_status") or "").lower()
            if state in ("error", "upload_failed", "expired"):
                raise VideoRejected("Facebook could not process this video.")
            if str(status.get("publish_status") or "").lower() == "published":
                return status
            return None

        refused = finish_error is not None and not lost
        status = self._poll(live, should_cancel, deadline=FINISH_CHECK if refused else POLL_DEADLINE)
        if not status:
            if refused:
                raise finish_error
            # Not confirmed, and maybe up: "processing" is never posted again
            # by Retry failed, where a failure would post a second Reel.
            if lost:
                why = "Facebook's answer to the publish step was lost, so it isn't known whether the Reel went up."
            elif not answered:
                why = "Facebook took the video, but its status could not be read."
            else:
                why = "Facebook accepted the video and is still processing it."
            return Posted("facebook", post_id=video_id, state="processing",
                          warnings=[f"{why} Check the Page before trying again."])
        url = str(status.get("permalink_url") or "")
        return Posted("facebook", post_id=str(status.get("post_id") or video_id), url=url,
                      warnings=[] if url else ["Facebook confirmed the post but did not return its link yet."])


# ---- text that fits ----------------------------------------------------------------


def fit_caption(text: str) -> str:
    """An Instagram caption inside its limits: 2,200 characters, 30 hashtags
    (any past the 30th are dropped, not the words)."""
    words = (text or "").split(" ")
    kept, tags = [], 0
    for word in words:
        if word.startswith("#"):
            tags += 1
            if tags > IG_HASHTAGS_MAX:
                continue
        kept.append(word)
    return " ".join(kept).strip()[:IG_CAPTION_MAX]
