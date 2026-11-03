"""Why a post to Instagram or Facebook did not happen, in words for people.

Each kind says what the creator can do about it; the publishing code
(remote_approval/publishing.py) records `message` on the post and never
shows a raw exception.
"""

from __future__ import annotations


class PublishError(Exception):
    """A post was not made. `message` is safe to show: it never holds a token."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message
        # Set by publish/meta_graph.py when the relay never answered, so the
        # step may have happened anyway.
        self.unanswered = False


class AuthRequired(PublishError):
    """The account has to be connected again, or allowed to publish."""


class RateLimited(PublishError):
    """The platform asked for fewer requests; trying later works."""


class UploadLimitExceeded(PublishError):
    """The account's daily publishing limit is reached."""


class VideoRejected(PublishError):
    """The platform could not use this video."""


class PublishCancelled(PublishError):
    """Stopped on request before the post was made."""
