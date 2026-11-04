"""Who may do what: the owner everything, the judge login only what JUDGE_MAY lists.

The judge login (user `judge`, password GAMEDAY_JUDGE_PASSWORD) lets the
event's judges try the hosted app without the owner password: they can open
the dashboard, read what it shows, upload a game, and cancel or retry the
games uploaded with it. Guard (app.py) refuses everything else with 403
before any route runs, so a route added later is the owner's alone until it
is listed here (docs/API.md, "The judge login").

A game uploaded with the judge login is marked (jobs.created_by) and is never
offered on the owner's phone (worker.py, remote_approval/notifier.py). It
uses the same models, so META_BUDGET_USD caps its spending like any other.
"""

from __future__ import annotations

import re

from gameday.config import JUDGE_USER, OWNER_USER

OWNER = OWNER_USER
JUDGE = JUDGE_USER
#: Where Guard puts who signed in, in each request's ASGI scope.
ROLE_KEY = "gameday.role"

CANT_CHANGE = "The judge login can't change this."
CANT_OPEN = "The judge login can't open this."
NOT_ITS_GAME = "The judge login can only cancel or retry games uploaded with it."
NOT_OFFERED = "Uploaded with the judge login, so it isn't offered on the phone."

_READ = frozenset({"GET", "HEAD"})
#: What the judge login may use: (methods, path). The reads are the ones the
#: dashboard makes, and none of them returns a secret.
JUDGE_MAY: tuple[tuple[frozenset[str], re.Pattern[str]], ...] = (
    (_READ, re.compile(r"/")),
    (_READ, re.compile(r"/static/.+")),
    (_READ, re.compile(r"/media/jobs/[^/]+/clips/[^/]+")),
    (_READ, re.compile(r"/api/(?:status|settings|watches|jobs|clips|usage|events)")),
    (_READ, re.compile(r"/api/jobs/[^/]+")),
    (_READ, re.compile(r"/remote-approval/(?:status|reviews)")),
    (frozenset({"POST"}), re.compile(r"/api/upload")),
    # Only a game uploaded with the judge login: the routes check that.
    (frozenset({"POST"}), re.compile(r"/api/jobs/[^/]+/(?:cancel|retry)")),
)


def judge_may(method: str, path: str) -> bool:
    """Whether the judge login may send `method` to `path`."""
    return any(method in methods and pattern.fullmatch(path) for methods, pattern in JUDGE_MAY)


def role_of(request) -> str:
    """Who signed in for this request, as Guard found it: "owner" or "judge".
    A request Guard didn't see gets the judge's role, the one that may do least."""
    return request.scope.get(ROLE_KEY) or JUDGE


def made_by_judge(job) -> bool:
    """Whether this job is a game uploaded with the judge login, or a retry of one."""
    try:
        return job["created_by"] == JUDGE
    except (KeyError, IndexError):  # a row or dict from before the judge login
        return False
