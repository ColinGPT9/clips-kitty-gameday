"""The states an offer moves through, and the only moves allowed.

    PROCESSING -> RENDERING -> READY_FOR_REVIEW -> NOTIFICATION_PENDING
      -> NOTIFICATION_SENT -> AWAITING_APPROVAL -> (DEFERRED | CANCELLED | EXPIRED)
      -> APPROVED -> PUBLISH_QUEUED -> PUBLISHING
      -> (PUBLISHED | PARTIAL_FAILURE | FAILED)

PROCESSING and RENDERING are the existing jobs row's own state, read from
there (for_job below), not copied into a second table that could disagree
with it. Every state from READY_FOR_REVIEW on is stored on the
remote_reviews row and only ever changed through move(), so a duplicate
webhook, a command delivered twice or a restart in the middle can only ever
repeat a move that is allowed, never skip one.

A move that is not in ALLOWED raises. The callers treat that as "this has
already happened" or "this is no longer possible", never as something to
force through.
"""

PROCESSING = "PROCESSING"
RENDERING = "RENDERING"
READY_FOR_REVIEW = "READY_FOR_REVIEW"
NOTIFICATION_PENDING = "NOTIFICATION_PENDING"
NOTIFICATION_SENT = "NOTIFICATION_SENT"
AWAITING_APPROVAL = "AWAITING_APPROVAL"
DEFERRED = "DEFERRED"
CANCELLED = "CANCELLED"
EXPIRED = "EXPIRED"
APPROVED = "APPROVED"
PUBLISH_QUEUED = "PUBLISH_QUEUED"
PUBLISHING = "PUBLISHING"
PARTIAL_FAILURE = "PARTIAL_FAILURE"
FAILED = "FAILED"
PUBLISHED = "PUBLISHED"

ALLOWED: dict[str, frozenset[str]] = {
    READY_FOR_REVIEW: frozenset({NOTIFICATION_PENDING, CANCELLED}),
    # Back to pending is a delivery that failed: the creator can send it again.
    NOTIFICATION_PENDING: frozenset({NOTIFICATION_SENT, CANCELLED, EXPIRED}),
    NOTIFICATION_SENT: frozenset({AWAITING_APPROVAL, NOTIFICATION_PENDING, APPROVED, DEFERRED,
                                  CANCELLED, EXPIRED}),
    AWAITING_APPROVAL: frozenset({APPROVED, DEFERRED, CANCELLED, EXPIRED}),
    # A reminder that went out puts it back to waiting; an approval can still
    # arrive straight from the deferred state (the link stayed valid).
    DEFERRED: frozenset({AWAITING_APPROVAL, APPROVED, CANCELLED, EXPIRED}),
    # Approved but refused at queue time (a clip changed, a platform went
    # away) ends FAILED with the reason; cancelled before anything was queued.
    APPROVED: frozenset({PUBLISH_QUEUED, FAILED, CANCELLED}),
    PUBLISH_QUEUED: frozenset({PUBLISHING, CANCELLED, FAILED, PARTIAL_FAILURE, PUBLISHED}),
    PUBLISHING: frozenset({PUBLISHED, PARTIAL_FAILURE, FAILED}),
    # Retrying what failed is a local action in the app (the person at the PC
    # is already authorised). It never needs, or reuses, a phone approval.
    PARTIAL_FAILURE: frozenset({PUBLISH_QUEUED}),
    FAILED: frozenset({PUBLISH_QUEUED}),
    PUBLISHED: frozenset(),
    CANCELLED: frozenset(),
    EXPIRED: frozenset(),
}

#: An offer in one of these can still be approved from the phone.
OPEN = frozenset({NOTIFICATION_SENT, AWAITING_APPROVAL, DEFERRED})
#: Nothing more will happen to an offer in one of these by itself.
SETTLED = frozenset({PUBLISHED, PARTIAL_FAILURE, FAILED, CANCELLED, EXPIRED})
#: Every stored state, for validating what is read back.
STORED = frozenset(ALLOWED)
ALL = (PROCESSING, RENDERING, *ALLOWED)


class IllegalMove(ValueError):
    """A move the state machine does not allow from where the offer is."""

    def __init__(self, current: str, wanted: str):
        super().__init__(f"{current} -> {wanted} is not allowed")
        self.current = current
        self.wanted = wanted


def can_move(current: str, wanted: str) -> bool:
    return wanted in ALLOWED.get(current, frozenset())


def check(current: str, wanted: str) -> None:
    if not can_move(current, wanted):
        raise IllegalMove(current, wanted)


def for_job(job_status: str, stage: str = "") -> str | None:
    """PROCESSING or RENDERING for a jobs row that is still working, from the
    row itself (and its live progress stage, when known). None once it has
    finished: from then on the offer's own row says where it is."""
    if job_status in ("queued", "running"):
        return RENDERING if stage == "render" else PROCESSING
    return None
