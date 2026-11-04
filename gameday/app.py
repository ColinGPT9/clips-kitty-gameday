"""The web app: create_app() builds it, routes and rules (docs/API.md).

Rules for every route, in one ASGI layer (Guard), so nothing can skip them:
* HTTP Basic auth as `owner`, or as `judge` when GAMEDAY_JUDGE_PASSWORD is
  set, compared in constant time, on everything but GET /health; a wrong or
  missing password is 401 with WWW-Authenticate. After LOCK_AFTER wrong
  passwords (either user's) from one address within LOCK_WINDOW_S, that
  address gets 429 for LOCK_FOR_S, without its password being checked.
* Every POST, PUT, PATCH and DELETE also needs X-Gameday-Action: 1, else 403,
  so a page on another site can't make the owner's browser change anything.
* The judge login gets 403 for anything access.JUDGE_MAY doesn't list, so a
  new route is the owner's alone until it is listed there.
* Every answer carries the Content-Security-Policy, nosniff and no-referrer
  headers, errors and files included.
* An error is {"error": "<a sentence for people>"}.

The background threads (worker.py) start and stop with the app's lifespan
when run_threads is true, as `python -m gameday` runs it.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import ipaddress
import logging
import math
import os
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException

from gameday import __version__, access
from gameday import secrets as secret_store
from gameday.config import JUDGE_USER, OWNER_USER, Config, StartRefused, judge_password_problem, owner_password
from gameday.store import Store
from gameday.types import APP_NAME
from gameday.watch.watcher import check_folder
from gameday.worker import Worker

log = logging.getLogger("gameday")

CSP = ("default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; style-src 'self'; "
       "script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
       "form-action 'self'")
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
ACTION_HEADER = "x-gameday-action"
CHANGING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
WATCH_DIR_FLAG = "watch_dir_added"
LOCK_AFTER = 10  # wrong passwords from one address...
LOCK_WINDOW_S = 600.0  # ...within 10 minutes...
LOCK_FOR_S = 900.0  # ...lock that address out for 15 minutes
MAX_TRACKED = 10_000  # addresses remembered at once


def client_address(scope, headers: Headers) -> str:
    """Who is asking. A request from a private or loopback address came
    through the proxy (Caddy on this host, or in a container beside the app),
    so the visitor is the address that proxy saw: the last X-Forwarded-For
    entry. Anything else is the peer itself."""
    peer = (scope.get("client") or ("", 0))[0] or ""
    try:
        behind_proxy = not ipaddress.ip_address(peer).is_global
    except ValueError:
        behind_proxy = False
    if behind_proxy:
        forwarded = [part.strip() for part in headers.get("x-forwarded-for", "").split(",") if part.strip()]
        if forwarded:
            return forwarded[-1][:64]
    return peer[:64]


class Guard:
    """Basic auth, the action header, the judge login's limits and the
    security headers, for every request. Who signed in goes into the request's
    scope (access.ROLE_KEY) for the routes."""

    def __init__(self, app, *, password: str, user: str = OWNER_USER, judge_password: str = "",
                 clock=time.monotonic):
        self.app = app
        # (user, password, role): the owner, and the judge login when it has a password.
        self._logins = [(user.encode(), password.encode(), access.OWNER)]
        if judge_password:
            self._logins.append((JUDGE_USER.encode(), judge_password.encode(), access.JUDGE))
        self._sign_in = f"Sign in as {user} or {JUDGE_USER}" if judge_password else f"Sign in as {user}"
        self._clock = clock
        self._fails: OrderedDict[str, list[float]] = OrderedDict()  # address -> times of wrong passwords
        self._locked: OrderedDict[str, float] = OrderedDict()  # address -> locked until

    def locked_for(self, who: str, now: float) -> float:
        """Seconds `who` is still locked out for, 0 when it isn't."""
        until = self._locked.get(who)
        if until is None:
            return 0.0
        if until <= now:
            del self._locked[who]
            return 0.0
        return until - now

    def failed(self, who: str, now: float) -> None:
        times = [t for t in self._fails.pop(who, []) if now - t < LOCK_WINDOW_S]
        times.append(now)
        if len(times) >= LOCK_AFTER:
            self._locked[who] = now + LOCK_FOR_S
            self._locked.move_to_end(who)
            log.warning("%d wrong passwords from %s in %d minutes: refusing it for %d minutes.", len(times), who,
                        int(LOCK_WINDOW_S // 60), int(LOCK_FOR_S // 60))
        else:
            self._fails[who] = times
        for table in (self._fails, self._locked):
            while len(table) > MAX_TRACKED:
                table.popitem(last=False)

    def signed_in_as(self, headers: Headers) -> str:
        """The role of a right user and password, "owner" or "judge"; "" for anything else."""
        value = headers.get("authorization", "")
        scheme, _, encoded = value.partition(" ")
        if scheme.lower() != "basic" or not encoded:
            return ""
        try:
            decoded = base64.b64decode(encoded.strip(), validate=True)
        except (binascii.Error, ValueError):
            return ""
        user, sep, password = decoded.partition(b":")
        role = ""
        # Every login's user and password compared, every time, in constant time.
        for login_user, login_password, login_role in self._logins:
            user_ok = hmac.compare_digest(user, login_user)
            password_ok = hmac.compare_digest(password, login_password)
            if sep and user_ok and password_ok and not role:
                role = login_role
        return role

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def send_secure(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers[name] = value
            await send(message)

        method, path = scope["method"], scope["path"]
        if not (path == "/health" and method in ("GET", "HEAD")):
            headers = Headers(scope=scope)
            who, now = client_address(scope, headers), self._clock()
            wait = self.locked_for(who, now)
            if wait:
                response = JSONResponse(
                    {"error": "Too many wrong passwords from your address. Try again in 15 minutes."},
                    status_code=429, headers={"Retry-After": str(math.ceil(wait))})
                await response(scope, receive, send_secure)
                return
            role = self.signed_in_as(headers)
            if not role:
                # A browser's first request carries no password at all: only a wrong one counts.
                if "authorization" in headers:
                    self.failed(who, now)
                response = JSONResponse(
                    {"error": f"{self._sign_in} to use {APP_NAME}."}, status_code=401,
                    headers={"WWW-Authenticate": f'Basic realm="{APP_NAME}", charset="UTF-8"'})
                await response(scope, receive, send_secure)
                return
            if role == access.OWNER:
                # The owner got it right: earlier typos are forgiven. The judge's
                # password forgives nothing, so it can't reset guesses at the owner's.
                self._fails.pop(who, None)
            if method in CHANGING and headers.get(ACTION_HEADER) != "1":
                response = JSONResponse({"error": "This change needs the X-Gameday-Action: 1 header."},
                                        status_code=403)
                await response(scope, receive, send_secure)
                return
            if role == access.JUDGE and not access.judge_may(method, path):
                refusal = access.CANT_CHANGE if method in CHANGING else access.CANT_OPEN
                await JSONResponse({"error": refusal}, status_code=403)(scope, receive, send_secure)
                return
            scope[access.ROLE_KEY] = role
        try:
            await self.app(scope, receive, send_secure)
        except Exception:
            log.exception("%s %s failed.", method, path)
            if started:
                raise
            await JSONResponse({"error": "Something went wrong in the app."}, status_code=500)(
                scope, receive, send_secure)


def _readable(errors) -> str:
    for error in errors:
        where = ".".join(str(p) for p in error.get("loc", ()) if p not in ("body", "query", "path"))
        return f"{where}: {error.get('msg', 'not valid')}" if where else str(error.get("msg", "not valid"))
    return "The request isn't valid."


def add_watch_dir(store: Store, config: Config) -> None:
    """WATCH_DIR is watched from the first start. Only once: a watch the
    owner removes stays removed."""
    if not config.watch_dir or store.get_flag(WATCH_DIR_FLAG):
        return
    try:
        folder = check_folder(config.watch_dir, config.data_dir)
    except ValueError as e:
        log.warning("WATCH_DIR can't be watched: %s", e)
        store.add_event("warn", "watch", f"WATCH_DIR can't be watched: {e}")
        return
    if store.watch_by_path(str(folder)) is None:
        store.add_watch(str(folder))
        store.add_event("info", "watch", f"Watching {folder} (WATCH_DIR).")
    store.set_flag(WATCH_DIR_FLAG, str(folder))


def sweep_uploads(store: Store, config: Config, *, older_than_s: float = 3600.0) -> int:
    """Delete what an upload cut off by a kill or a crash left in DATA_DIR/uploads:
    files no video row points at, untouched for an hour. Returns how many went."""
    def key(path) -> str:
        return os.path.normcase(str(Path(path).resolve()))

    kept = {key(row["path"]) for row in store.conn.execute("SELECT path FROM videos").fetchall()}
    gone = 0
    cutoff = time.time() - older_than_s
    try:
        files = [p for p in config.uploads_dir.iterdir() if p.is_file()]
    except OSError:
        return 0
    for path in files:
        try:
            if key(path) not in kept and path.stat().st_mtime < cutoff:
                path.unlink()
                gone += 1
        except OSError:
            continue
    if gone:
        log.info("Removed %d unfinished upload%s left by an earlier run.", gone, "" if gone == 1 else "s")
    return gone


def create_app(config: Config | None = None, *, pipeline=None, meta_factory=None, run_threads: bool = True,
               feed=None, probe=None) -> FastAPI:
    """The app. `pipeline`, `meta_factory`, `feed` and `probe` are seams for
    tests (fakes of the engine, Meta, the folder feed and ffprobe). Raises
    StartRefused for a judge password it won't use."""
    config = config or Config.from_env()
    config.ensure_dirs()
    password = owner_password(config)
    problem = judge_password_problem(config, password)
    if problem:
        raise StartRefused(problem)
    store = Store(config.db_path, data_dir=config.data_dir, budget_usd=config.budget_usd)
    secret_store.attach(store, config)
    worker = Worker(store, config, pipeline=pipeline, meta_factory=meta_factory, feed=feed, probe=probe)
    add_watch_dir(store, config)
    sweep_uploads(store, config)

    @asynccontextmanager
    async def lifespan(app):
        if run_threads:
            worker.start()
        try:
            yield
        finally:
            if run_threads:
                worker.stop()

    app = FastAPI(title=APP_NAME, version=__version__, docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan)
    app.state.config = config
    app.state.store = store
    app.state.worker = worker
    app.state.meta_factory = worker.meta_factory

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request, exc: StarletteHTTPException):
        detail = exc.detail if isinstance(exc.detail, str) else "The request couldn't be handled."
        if exc.status_code == 404 and detail == "Not Found":
            detail = "Not found."
        elif exc.status_code == 405:
            detail = "That method isn't allowed here."
        return JSONResponse({"error": detail}, status_code=exc.status_code, headers=getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def invalid(request, exc: RequestValidationError):
        return JSONResponse({"error": f"The request isn't valid: {_readable(exc.errors())}"}, status_code=400)

    from gameday.routes import api, files, remote

    api.install(app, store=store, config=config, worker=worker, meta_factory=worker.meta_factory, probe=probe)
    remote.install(app, store_db=store, config=config, worker=worker, meta_factory=worker.meta_factory)
    files.install(app, config=config)
    app.add_middleware(Guard, password=password, judge_password=config.judge_password)
    return app
