"""The signed client for the creator's relay (meta-relay/).

Ported, protocol unchanged, from this week's event branch. This app (the
"PC" below) never needs a connection from the internet to reach it. Everything goes out
from here, over HTTPS, to the relay the creator deployed: notifications go
up, decisions made on the phone come back as the answer to a poll. The
relay holds the WhatsApp and Meta credentials and the app secret; this PC
holds only its own device secret, which signs every request:

    X-CK-Device     dev_...
    X-CK-Timestamp  unix seconds (the relay refuses anything 5 minutes off)
    X-CK-Nonce      random, never accepted twice (replay protection)
    X-CK-Signature  hex HMAC-SHA256(device secret,
                        METHOD \\n PATH?QUERY \\n TIMESTAMP \\n NONCE \\n SHA256(body))

An upload's body is a chunk of a video, streamed through: its hash would
mean reading it twice, so it is signed as UNSIGNED-PAYLOAD and everything
that says what it is (command, platform, offset, size) rides in the signed
query instead. TLS carries the bytes. A clip's preview for the approval page
goes up the same way, its offer, clip and size in the signed query.

Commands coming back are signed by the relay with the same secret over
their canonical JSON (sorted keys, no spaces), and verify_command checks
that before commands.py looks at one.
"""

import hashlib
import hmac
import json
import secrets as _random
import time
from pathlib import Path
from urllib.parse import quote, urlencode

import requests

TIMEOUT = 20
UPLOAD_TIMEOUT = 300
PREVIEW_TIMEOUT = 120
UNSIGNED = "UNSIGNED-PAYLOAD"

# Replaced in tests, so no test touches the network.
transport = requests.request


class RelayError(Exception):
    """Something the relay refused or could not do. `message` is safe to
    show: the relay writes it, and never puts a token or a number in it."""

    def __init__(self, code: str, message: str, *, status: int = 0, retryable: bool = False,
                 meta: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.retryable = retryable
        # Meta's numeric code and subcode, and which platform, when the relay
        # was refused by a Graph call (publish/meta_graph.classify reads them).
        self.meta = meta or {}


def canonical_json(value) -> str:
    """The exact text both sides sign: sorted keys, no spaces, UTF-8 as is.
    Matches canonical() in meta-relay/worker.js."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sign(secret: str, method: str, path: str, timestamp: str, nonce: str, body_hash: str) -> str:
    message = "\n".join([method.upper(), path, timestamp, nonce, body_hash])
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def verify_command(secret: str, command: dict) -> bool:
    given = str(command.get("sig") or "")
    body = {k: v for k, v in command.items() if k != "sig"}
    expected = hmac.new(secret.encode(), canonical_json(body).encode(), hashlib.sha256).hexdigest()
    return bool(given) and hmac.compare_digest(given, expected)


class Relay:
    def __init__(self, base_url: str, device_id: str = "", device_secret: str = "", *, clock=time.time):
        self.base_url = base_url.rstrip("/")
        self.device_id = device_id
        self.device_secret = device_secret
        self.clock = clock

    # ---- plumbing -------------------------------------------------------------

    def _headers(self, method: str, path: str, body_hash: str) -> dict:
        timestamp = str(int(self.clock()))
        nonce = _random.token_hex(12)
        return {
            "X-CK-Device": self.device_id,
            "X-CK-Timestamp": timestamp,
            "X-CK-Nonce": nonce,
            "X-CK-Signature": sign(self.device_secret, method, path, timestamp, nonce, body_hash),
            "X-Clips-Kitty": "1",
        }

    def call(self, method: str, path: str, body: dict | None = None, *, query: dict | None = None,
             auth: str = "") -> dict:
        """One signed JSON request. `auth` replaces the signature with a
        bearer key, for the one call that has none yet (registration)."""
        if query:
            path = f"{path}?{urlencode(query, quote_via=quote)}"
        raw = canonical_json(body).encode() if body is not None else b""
        if auth:
            headers = {"Authorization": f"Bearer {auth}", "X-Clips-Kitty": "1"}
        else:
            if not (self.device_id and self.device_secret):
                raise RelayError("not_registered", "This PC is not registered with your relay yet.")
            headers = self._headers(method, path, hashlib.sha256(raw).hexdigest())
        if body is not None:
            headers["Content-Type"] = "application/json"
        return self._send(method, path, headers, raw if body is not None else None, TIMEOUT)

    def _send(self, method: str, path: str, headers: dict, data, timeout: float) -> dict:
        try:
            # Never followed: a redirect could send this request (and its body) to another
            # address, a service on this machine included. The relay itself never redirects.
            response = transport(method, self.base_url + path, headers=headers, data=data, timeout=timeout,
                                 allow_redirects=False)
        except requests.Timeout:
            raise RelayError("network", "Your relay took too long to answer.", retryable=True) from None
        except requests.RequestException:
            # Not the exception's text: it names the URL and can carry headers.
            raise RelayError("network", "Couldn't reach your relay. Check the address and your "
                                        "internet connection.", retryable=True) from None
        if 300 <= response.status_code < 400:
            raise RelayError("redirect", "Your relay answered with a redirect. Use its final https:// address.",
                             status=response.status_code)
        try:
            answer = response.json()
        except ValueError:
            answer = {}
        if not isinstance(answer, dict):
            answer = {}
        if response.status_code >= 400:
            raise RelayError(
                str(answer.get("code") or "error"),
                str(answer.get("error") or f"Your relay answered {response.status_code}.")[:300],
                status=response.status_code,
                retryable=response.status_code in (408, 425, 429, 500, 502, 503, 504),
                meta={k: answer[k] for k in ("meta_code", "meta_subcode", "platform") if k in answer},
            )
        return answer

    # ---- this PC --------------------------------------------------------------

    def register(self, enroll_key: str, name: str = "Clips Kitty Game Day") -> dict:
        return self.call("POST", "/v1/devices", {"device_id": self.device_id,
                                                 "device_secret": self.device_secret,
                                                 "name": name[:60]}, auth=enroll_key)

    def status(self) -> dict:
        return self.call("GET", "/v1/status")

    def preferences(self, prefs: dict) -> dict:
        return self.call("POST", "/v1/preferences", prefs)

    # ---- pairing a WhatsApp number -------------------------------------------

    def pair_start(self) -> dict:
        return self.call("POST", "/v1/pair/start", {})

    def pair_confirm(self, confirm: bool) -> dict:
        return self.call("POST", "/v1/pair/confirm", {"confirm": bool(confirm)})

    def set_pin(self, pin: str) -> dict:
        return self.call("POST", "/v1/pin", {"pin": pin})

    def disconnect(self) -> dict:
        return self.call("POST", "/v1/disconnect", {})

    # ---- the creator's Muse agent -------------------------------------------

    def muse_token(self) -> dict:
        """A new key for the creator's Muse agent, replacing any older one:
        {"key": "ckm_...", "muse": {...}}. The answer is the only place the
        key ever is: the relay keeps its hash, and this PC passes it to
        Settings once and keeps nothing."""
        return self.call("POST", "/v1/muse/token", {})

    def muse_revoke(self) -> dict:
        return self.call("POST", "/v1/muse/revoke", {})

    # ---- notifications and decisions ----------------------------------------

    def notify(self, body: dict) -> dict:
        return self.call("POST", "/v1/notify", body)

    def commands(self) -> list[dict]:
        return list(self.call("GET", "/v1/commands").get("commands") or [])

    def ack(self, command_id: str, outcome: str, reason: str = "") -> dict:
        return self.call("POST", f"/v1/commands/{quote(command_id)}/ack",
                         {"outcome": outcome, "reason": reason[:300]})

    def cancel(self, approval_id: str) -> dict:
        return self.call("POST", f"/v1/approvals/{quote(approval_id)}/cancel", {})

    def templates(self, create: bool = False) -> dict:
        return self.call("POST" if create else "GET", "/v1/templates", {} if create else None)

    # ---- Meta accounts and publishing ----------------------------------------

    def accounts_connect(self, platform: str) -> dict:
        return self.call("POST", "/v1/accounts/connect", {"platform": platform})

    def accounts_disconnect(self, platform: str) -> dict:
        return self.call("POST", "/v1/accounts/disconnect", {"platform": platform})

    def meta(self, op: str, params: dict) -> dict:
        """One Graph step of an approved publish (publish/meta_graph.py). The
        relay refuses any step outside an approved command."""
        return self.call("POST", f"/v1/meta/{quote(op)}", params)

    def upload_chunk(self, query: dict, path: Path, offset: int, length: int) -> dict:
        """Stream one chunk of the file at `offset` to the relay, which passes
        it to Meta's upload host with the right token."""
        target = f"/v1/meta/upload?{urlencode(query, quote_via=quote)}"
        headers = self._headers("POST", target, UNSIGNED)
        headers["Content-Type"] = "application/octet-stream"
        headers["Content-Length"] = str(length)
        with open(path, "rb") as f:
            f.seek(offset)
            return self._send("POST", target, headers, _Chunk(f, length), UPLOAD_TIMEOUT)

    def upload_preview(self, approval_id: str, clip_id: int, data: bytes) -> dict:
        """One offered clip's preview, an MP4 of at most 4 MiB, for the
        approval page to play (remote_approval/previews.py). Sent whole: it
        is small enough to hold."""
        if not (self.device_id and self.device_secret):
            raise RelayError("not_registered", "This PC is not registered with your relay yet.")
        query = {"approval_id": approval_id, "clip_id": int(clip_id), "size": len(data)}
        target = f"/v1/previews?{urlencode(query, quote_via=quote)}"
        headers = self._headers("POST", target, UNSIGNED)
        headers["Content-Type"] = "video/mp4"
        headers["Content-Length"] = str(len(data))
        return self._send("POST", target, headers, data, PREVIEW_TIMEOUT)


class _Chunk:
    """A read-only window on an open file, read in pieces, so a 60 MB clip
    is streamed rather than held in memory."""

    def __init__(self, f, length: int, piece: int = 1 << 20):
        self.f = f
        self.left = length
        self.piece = piece

    def __iter__(self):
        while self.left > 0:
            data = self.f.read(min(self.piece, self.left))
            if not data:
                break
            self.left -= len(data)
            yield data

    def __len__(self):
        return self.left
