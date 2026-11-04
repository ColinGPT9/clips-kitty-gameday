"""Phone alerts through ntfy (https://ntfy.sh), a free, open-source push
notification service with apps for iPhone and Android and no account. It is
a notification service, not a model.

The owner's phone subscribes to a topic in the ntfy app, and one HTTPS POST
to NTFY_SERVER/<topic> (send) shows up on it. Anyone who knows a topic can
read what is sent to it, and send to it, so the topic is a secret:
"clipskitty-" and 24 random URL-safe characters (new_topic), kept encrypted
in the secrets store like the Meta key (secrets.py, NTFY_TOPIC), in the one
answer that makes it (routes/api.py) and never in a log line, an event, an
error or any other answer.

What alerts (worker.py calls these, for the owner's games only: a game
uploaded with the judge login never alerts, as it never reaches the phone):
a game that finished with clips (game_ready), a game that didn't finish
(game_failed), and an approval whose uploads have all finished (posted:
"Posted to Instagram + Facebook", or "Posting failed"). Nothing at all while
no topic is saved.

alert() only queues and never raises: one daemon thread, started by the
first alert, sends each one, so a slow or unreachable ntfy never holds up a
job or the relay loop. A failure adds one warn event, "Phone alert not sent:
<reason>", and nothing else.
"""

from __future__ import annotations

import base64
import logging
import queue
import re
import secrets as _random
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests

from gameday import access
from gameday.config import DEFAULT_NTFY_SERVER
from gameday.types import APP_NAME

log = logging.getLogger("gameday")

#: The topic's name in the secrets store.
NTFY_TOPIC = "ntfy_topic"
TOPIC_PREFIX = "clipskitty-"
TOPIC = re.compile(r"[A-Za-z0-9_-]{1,64}")  # what ntfy takes as a topic name
TIMEOUT_S = 10
#: ntfy takes a body of up to 4,096 bytes as the message; a bigger one would arrive as an attachment.
MAX_MESSAGE_BYTES = 3000
MAX_QUEUED = 50
TEST_TITLE = f"Test alert from {APP_NAME}"
#: The places an approval posts to, in the order an alert names them.
PLACES = {"instagram": "Instagram", "facebook": "Facebook"}

# Replaced in tests, so no test reaches ntfy.
transport = requests.request

_STOP = object()
# A file path, Windows or POSIX, with at least one folder in it. Not a URL: the
# slash that starts it doesn't follow a letter, a colon, a dot or another slash.
_PATH = re.compile(r"(?:\b[A-Za-z]:[\\/]|(?<![\w:/.~-])/)(?:[^\\/\s\"'<>|:*?]+[\\/])+([^\\/\s\"'<>|:*?]*)")


class AlertError(Exception):
    """An alert that wasn't sent. `reason` is safe to show: it never holds the topic."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def new_topic() -> str:
    """A fresh topic: "clipskitty-" and 24 random URL-safe characters (144 bits)."""
    return TOPIC_PREFIX + _random.token_urlsafe(18)


def saved_topic(store) -> str:
    """The saved topic, or "" while phone alerts are off."""
    secrets = getattr(store, "secrets", None)
    topic = (secrets.get(NTFY_TOPIC) if secrets is not None else None) or ""
    return topic if TOPIC.fullmatch(topic) else ""


def subscribe_links(topic: str, server: str = DEFAULT_NTFY_SERVER) -> dict:
    """Where a phone subscribes to `topic`: its web page, and the ntfy app's own link."""
    server = (server or DEFAULT_NTFY_SERVER).rstrip("/")
    parts = urlsplit(server)
    app = f"ntfy://{parts.netloc}{parts.path}/{topic}" + ("?secure=false" if parts.scheme == "http" else "")
    return {"web": f"{server}/{topic}", "app": app}


def page_link(public_url: str, view: str) -> str | None:
    """The dashboard page `view` (gameday, offers, setup), when PUBLIC_URL is set."""
    return f"{public_url.rstrip('/')}/#{view}" if public_url and view else None


def plain(text: str, limit: int = 300) -> str:
    """Words for a phone: one line, a file path cut to its file's name, at most `limit` characters."""
    text = _PATH.sub(lambda m: m.group(1) or "…", " ".join(str(text or "").split()))
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def header_value(text: str) -> str:
    """`text` as one header line that ntfy reads back as it is. A header
    carries Latin-1 at most, so anything that isn't plain ASCII goes as RFC 2047
    encoded words (=?UTF-8?B?...?=), which ntfy decodes. Each word holds whole
    characters and stays within the 75 characters RFC 2047 allows."""
    text = "".join(c for c in " ".join(str(text or "").split()) if c.isprintable())[:250]
    if text.isascii():
        return text
    words, chunk = [], b""
    for char in text:
        piece = char.encode("utf-8")
        if chunk and len(chunk) + len(piece) > 45:  # 45 bytes are 60 characters of base64
            words.append(chunk)
            chunk = b""
        chunk += piece
    words.append(chunk)
    return " ".join(f"=?UTF-8?B?{base64.b64encode(word).decode('ascii')}?=" for word in words)


def send(topic: str, title: str, message: str, *, click: str | None = None, tags=(), priority: int | None = None,
         server: str = DEFAULT_NTFY_SERVER) -> None:
    """One alert: one HTTPS POST to {server}/{topic}, the message as the body
    and ntfy's Title, Tags, Click and Priority headers. Raises AlertError,
    whose reason never holds the topic."""
    if not TOPIC.fullmatch(topic or ""):
        raise AlertError("there is no alert channel.")
    headers = {"Title": header_value(title) or APP_NAME, "Content-Type": "text/plain; charset=utf-8"}
    tags = [t for t in (header_value(t) for t in tags) if t]
    if tags:
        headers["Tags"] = ",".join(tags)
    if click:
        headers["Click"] = header_value(click)
    if priority is not None:
        headers["Priority"] = str(max(1, min(5, int(priority))))
    body = str(message or "").strip().encode("utf-8")[:MAX_MESSAGE_BYTES].decode("utf-8", "ignore")
    try:
        # Never followed: a redirect would take the alert, and its topic, somewhere else.
        response = transport("POST", f"{(server or DEFAULT_NTFY_SERVER).rstrip('/')}/{topic}", headers=headers,
                             data=(body or title or APP_NAME).encode("utf-8"), timeout=TIMEOUT_S,
                             allow_redirects=False)
    except requests.Timeout:
        raise AlertError("ntfy took too long to answer.") from None
    except requests.RequestException:
        # Not the exception's own text: it names the address, and so the topic.
        raise AlertError("couldn't reach ntfy.") from None
    status = int(response.status_code)
    if 300 <= status < 400:
        raise AlertError("ntfy answered with a redirect. Check NTFY_SERVER.")
    if status >= 400:
        try:
            answer = response.json()
        except ValueError:
            answer = {}
        said = str(answer.get("error") or "") if isinstance(answer, dict) else ""
        said = plain(said.replace(topic, "the alert channel"), 160).rstrip(".")
        raise AlertError(f"ntfy answered {status}" + (f": {said}." if said else "."))


class Alerts:
    """The worker's phone alerts: queued here, sent by one daemon thread."""

    def __init__(self, store, config):
        self.store = store
        self.server = getattr(config, "ntfy_server", "") or DEFAULT_NTFY_SERVER
        self.public_url = getattr(config, "public_url", "") or ""
        self._queue: queue.Queue = queue.Queue(maxsize=MAX_QUEUED)
        self._lock = threading.Lock()
        self._idle = threading.Condition(self._lock)
        self._pending = 0
        self._thread: threading.Thread | None = None

    # ---- what alerts ------------------------------------------------------------

    def game_ready(self, job, clips: int, *, offered: bool) -> bool:
        """A game finished with `clips` clips; `offered` is whether they went to the phone."""
        try:
            if not clips or access.made_by_judge(job):
                return False
            rows = self.store.clips_for_job(int(job["id"]))
            best = max(rows, key=lambda c: (float(c["score"] or 0), -int(c["idx"]))) if rows else None
            lines = [plain(best["title"] or best["label"], 120) if best is not None else "",
                     "Ask Muse what's ready, or open Phone offers." if offered else "Open Game day to watch them."]
            return self.alert(f"{self._name(job)}: {clips} highlight{'s' if clips != 1 else ''} ready",
                              "\n".join(line for line in lines if line), view="gameday", tags=("basketball",))
        except Exception as e:
            log.warning("A phone alert couldn't be made (%s).", type(e).__name__)
            return False

    def game_failed(self, job, error: str) -> bool:
        try:
            if access.made_by_judge(job):
                return False
            return self.alert(f"Game didn't finish: {self._name(job)}",
                              plain(error) or "Open Game day to see why.", view="gameday", tags=("warning",),
                              priority=4)
        except Exception as e:
            log.warning("A phone alert couldn't be made (%s).", type(e).__name__)
            return False

    def posted(self, review: dict) -> bool:
        """An approval's uploads have all finished (remote_approval/service.py
        settle_publishing): one alert, from what the APIs confirmed."""
        try:
            job = self.store.get_job(int(review.get("job_id") or 0))
            if job is not None and access.made_by_judge(job):
                return False
            clips = (review.get("results") or {}).get("clips") or []
            posts = [(clip, post) for clip in clips for post in clip.get("posts") or []]
            worked = [p for p in PLACES if any(post["platform"] == p and post["state"] == "published"
                                               for _, post in posts)]
            problems = [post for _, post in posts if post["state"] in ("failed", "unconfirmed")]
            if worked:
                titles = [clip.get("title") for clip in clips
                          if any(post["state"] == "published" for post in clip.get("posts") or [])]
                lines = [_clips_line(titles), _problem_line(problems, partial=True) if problems else ""]
                return self.alert("Posted to " + " + ".join(PLACES[p] for p in worked),
                                  "\n".join(line for line in lines if line) or "Open Phone offers for the links.",
                                  view="offers", tags=("white_check_mark",))
            if problems:
                titles = [clip.get("title") for clip in clips
                          if any(post["state"] in ("failed", "unconfirmed") for post in clip.get("posts") or [])]
                lines = [_clips_line(titles), _problem_line(problems, partial=False)]
                return self.alert("Posting failed", "\n".join(line for line in lines if line), view="offers",
                                  tags=("warning",), priority=4)
            return False  # only places it was posted to already: nothing new to say
        except Exception as e:
            log.warning("A phone alert couldn't be made (%s).", type(e).__name__)
            return False

    def _name(self, job) -> str:
        """The game's name: its video's file name without the extension."""
        video = self.store.get_video(int(job["video_id"]))
        name = Path(str(video["name"])).stem if video is not None else ""
        return plain(name, 80) or f"Game {job['id']}"

    # ---- the queue and its thread -----------------------------------------------------

    def alert(self, title: str, message: str, *, view: str = "", tags=(), priority: int | None = None) -> bool:
        """Queue one alert, when a topic is saved. Returns at once, True when
        it was queued; never raises."""
        try:
            if not saved_topic(self.store):
                return False
            item = (title, message, page_link(self.public_url, view), tuple(tags), priority)
            with self._lock:
                self._queue.put_nowait(item)
                self._pending += 1
                if self._thread is None or not self._thread.is_alive():
                    self._thread = threading.Thread(target=self._run, name="gameday-alerts", daemon=True)
                    self._thread.start()
            return True
        except queue.Full:
            log.warning("Phone alerts are backing up, so one was dropped.")
        except Exception as e:
            log.warning("A phone alert couldn't be queued (%s).", type(e).__name__)
        return False

    def _run(self) -> None:
        try:
            while True:
                item = self._queue.get()
                if item is _STOP:
                    return
                try:
                    self._deliver(*item)
                except Exception as e:  # never a dead thread with alerts left in its queue
                    log.warning("A phone alert couldn't be sent (%s).", type(e).__name__)
                finally:
                    with self._lock:
                        self._pending -= 1
                        self._idle.notify_all()
        finally:
            self.store.close()

    def _deliver(self, title: str, message: str, click: str | None, tags: tuple, priority: int | None) -> None:
        topic = saved_topic(self.store)  # read again: alerts turned off since this was queued stay unsent
        if not topic:
            return
        try:
            send(topic, title, message, click=click, tags=tags, priority=priority, server=self.server)
            return
        except AlertError as e:
            reason = e.reason
        except Exception as e:
            reason = f"something went wrong ({type(e).__name__})."
        log.warning("Phone alert not sent: %s", reason)
        self.store.add_event("warn", "alerts", f"Phone alert not sent: {reason}")

    def wait_idle(self, timeout: float = 10.0) -> bool:
        """Wait until every queued alert has been sent or given up on (tests use it)."""
        deadline = time.monotonic() + timeout
        with self._lock:
            while self._pending:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._idle.wait(left)
        return True

    def stop(self, timeout: float = 5.0) -> None:
        """Send what is queued, for up to `timeout` seconds, then end the thread."""
        with self._lock:
            thread, self._thread = self._thread, None
            if thread is not None and thread.is_alive():
                try:
                    self._queue.put_nowait(_STOP)
                except queue.Full:
                    pass  # it is busy sending: a daemon thread, it ends with the process
        if thread is not None:
            thread.join(timeout)


def _clips_line(titles) -> str:
    titles = [plain(t, 120) for t in titles if t]
    if not titles:
        return ""
    more = len(titles) - 1
    return titles[0] + (f" and {more} more clip{'s' if more != 1 else ''}" if more else "")


def _problem_line(problems: list[dict], *, partial: bool) -> str:
    """Why the first post that didn't go up didn't: the platform's own words,
    with Meta's error code in them."""
    first = problems[0]
    place = PLACES.get(first["platform"], str(first["platform"]).title())
    reason = plain(first.get("error") or "", 200)
    if first["state"] == "unconfirmed":
        line = f"{place} isn't confirmed yet: {reason or 'check the account before trying again.'}"
    else:
        line = f"{place} didn't post: {reason}" if partial else f"{place}: {reason}"
        line = line if reason else f"{place} didn't post."
    more = len(problems) - 1
    return line + (f" ({more} more didn't post.)" if more else "")
