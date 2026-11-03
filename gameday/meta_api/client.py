"""MetaClient: every call the app makes to Meta's Model API.

One class for the three models (Muse Voice, Muse Spark, SAM 3.1). Each call
checks the budget with an estimate first, records an ApiCall on the ledger
whether it worked or not, tries once more after a 429, a 5xx or a timeout,
and fails with a MetaError whose message never holds the key.
"""

from __future__ import annotations

import base64
import json
import random
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import requests

from gameday import media
from gameday.meta_api import costs, sam_stream, voice
from gameday.meta_api.video import video_facts
from gameday.types import (
    META_API_BASE,
    MODEL_SAM,
    MODEL_SPARK,
    MODEL_VOICE,
    ApiCall,
    Ledger,
    MetaError,
    Segmentation,
    Turn,
)

USER_AGENT = "clips-kitty-gameday/0.1"
SAM_TIMEOUT = (20, 900)  # (connect, read): SAM answers a long clip slowly
SPARK_TIMEOUT = (20, 300)
VOICE_TIMEOUT = (20, 600)
SAM_MAX_BYTES = 6_000_000  # a bigger video goes in parts (the caller splits it)
VOICE_AT_ONCE = 3
RETRY_WAIT_S = 2.0
SPARK_REASONING_TOKENS = 4000
RETRY_AFTER_MAX_S = 30.0

NAMES = {MODEL_VOICE: "Muse Voice", MODEL_SPARK: "Muse Spark", MODEL_SAM: "SAM 3.1"}

Attempt = Callable[[], tuple[Any, float, dict, int]]  # (result, cost_usd, units, http status)


def _retry_after(resp) -> float | None:
    try:
        value = float((getattr(resp, "headers", None) or {}).get("Retry-After"))
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _message_text(answer: dict) -> tuple[str, str, str]:
    """(text, finish_reason, refusal) of the first choice in a chat answer."""
    choices = answer.get("choices") if isinstance(answer, dict) else None
    choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(str(p.get("text") or "") for p in content if isinstance(p, dict))
    return str(content or ""), str(choice.get("finish_reason") or ""), str(message.get("refusal") or "")


def _json_object(text: str, finish_reason: str) -> dict:
    """The JSON object in Muse Spark's answer (a ```json fence is allowed)."""
    body = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", body, flags=re.S)
    if fenced:
        body = fenced.group(1)
    try:
        value = json.loads(body)
    except ValueError:
        if finish_reason == "length":
            raise MetaError("Muse Spark ran out of tokens before it finished the JSON", status=200,
                            model=MODEL_SPARK) from None
        raise MetaError("Muse Spark's answer wasn't JSON", status=200, model=MODEL_SPARK) from None
    if not isinstance(value, dict):
        raise MetaError("Muse Spark's answer wasn't a JSON object", status=200, model=MODEL_SPARK)
    return value


class MetaClient:
    """The real gameday.types.MetaAPI."""

    def __init__(self, api_key: str, *, ledger: Ledger | None = None, base_url: str = META_API_BASE,
                 session: requests.Session | None = None):
        key = (api_key or "").strip()
        if not key:
            raise MetaError("No Meta key was given")
        self._key = key
        self._ledger = ledger
        self._base = base_url.rstrip("/")
        self._session = session if session is not None else requests.Session()
        self._sleep: Callable[[float], None] = time.sleep
        self._lock = threading.Lock()
        self._spent: dict[str, float] = {}
        self.voice_piece_s = voice.PIECE_S
        # Extra fields for every Muse Spark request. Muse Spark reasons before it answers, and at its
        # default effort a 1,000-token prompt used 6,000 tokens without finishing; "low" finished.
        self.spark_options: dict = {"reasoning_effort": "low"}
        # Its reasoning counts against max_tokens, so this much is added to what the caller asked for.
        self.spark_reasoning_tokens = SPARK_REASONING_TOKENS

    def __repr__(self) -> str:  # never the key
        return f"MetaClient(base_url={self._base!r})"

    # --- what the app calls -------------------------------------------------

    def transcribe(self, audio: Path, *, job_id: int | None = None) -> list[Turn]:
        audio = Path(audio)
        pieces = voice.plan_pieces(audio, self.voice_piece_s)
        if not pieces:
            return []

        def one(piece: voice.WavPiece) -> list[Turn]:
            data = voice.read_piece(audio, piece)
            fields = json.dumps(voice.request_fields())

            def attempt():
                resp = self._post("/asr/transcribe", model=MODEL_VOICE, timeout=VOICE_TIMEOUT, files={
                    "request": (None, fields, "application/json"),
                    "audio": (f"part{piece.index:03d}.wav", data, "audio/wav"),
                })
                answer = self._read_json(resp, MODEL_VOICE)
                return voice.parse_turns(answer, piece.offset_s), costs.voice_cost(piece.seconds), {}, \
                    resp.status_code

            units = {"audio_seconds": round(piece.seconds, 3), "bytes": len(data)}
            return self._call(MODEL_VOICE, "/asr/transcribe", "transcribe", job_id,
                              costs.voice_cost(piece.seconds), units, attempt)

        if len(pieces) == 1:
            found = [one(pieces[0])]
        else:
            with ThreadPoolExecutor(max_workers=min(VOICE_AT_ONCE, len(pieces)),
                                    thread_name_prefix="muse-voice") as pool:
                found = list(pool.map(one, pieces))
        return sorted((turn for part in found for turn in part), key=lambda t: (t.start_s, t.end_s))

    def chat_json(self, messages: list[dict], schema: dict, *, name: str, purpose: str,
                  job_id: int | None = None, max_tokens: int = 2048, temperature: float = 0.2) -> dict:
        response_format = {"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}}

        def parse(text: str, finish_reason: str, refusal: str) -> dict:
            if refusal and not text:
                raise MetaError(f"Muse Spark declined: {refusal[:160]}", status=200, model=MODEL_SPARK)
            return _json_object(text, finish_reason)

        return self._spark(messages, purpose=purpose, job_id=job_id, max_tokens=max_tokens,
                           temperature=temperature, response_format=response_format, parse=parse)

    def chat_text(self, messages: list[dict], *, purpose: str, job_id: int | None = None,
                  max_tokens: int = 600, temperature: float = 0.4) -> str:
        def parse(text: str, finish_reason: str, refusal: str) -> str:
            if not text.strip():
                if refusal:
                    raise MetaError(f"Muse Spark declined: {refusal[:160]}", status=200, model=MODEL_SPARK)
                why = " (it ran out of tokens)" if finish_reason == "length" else ""
                raise MetaError(f"Muse Spark answered with no text{why}", status=200, model=MODEL_SPARK)
            return text.strip()

        return self._spark(messages, purpose=purpose, job_id=job_id, max_tokens=max_tokens,
                           temperature=temperature, parse=parse)

    def segment_video(self, video: Path, noun: str, *, job_id: int | None = None) -> Segmentation:
        video = Path(video)
        size = video.stat().st_size
        if size > SAM_MAX_BYTES:
            raise MetaError(f"The video for SAM 3.1 is {size / 1e6:.1f} MB; send it in parts of at most "
                            f"{SAM_MAX_BYTES / 1e6:.0f} MB", model=MODEL_SAM)
        facts = video_facts(video)
        frames = facts.frames
        if frames <= 0:
            frames = max(1, int(round(media.probe(video).duration_s * (facts.fps or 1.0))))
        body = {
            "model": MODEL_SAM,
            "stream": True,
            "input": [{"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": noun},
                {"type": "input_video",
                 "video_url": "data:video/mp4;base64," + base64.b64encode(video.read_bytes()).decode("ascii")},
            ]}],
            "metadata": {"include_confidence": "true", "mask_encoding": "one_bit"},
        }

        def attempt():
            resp = self._post("/responses", model=MODEL_SAM, timeout=SAM_TIMEOUT, stream=True, json=body,
                              headers={"Accept": "text/event-stream"})
            try:
                answer = sam_stream.read_sam_stream(resp.iter_lines())
            except requests.RequestException:
                raise MetaError("SAM 3.1's answer was cut off", status=200, retryable=True,
                                model=MODEL_SAM) from None
            finally:
                resp.close()
            seg = Segmentation(noun=noun, width=facts.width, height=facts.height, frames=frames,
                               fps=facts.fps, boxes=answer.boxes)
            more = {"boxes": len(answer.boxes)}
            if answer.incomplete:
                more["incomplete"] = answer.incomplete
            return seg, costs.sam_cost(frames), more, resp.status_code

        return self._call(MODEL_SAM, "/responses", "track", job_id, costs.sam_cost(frames),
                          {"frames": frames, "bytes": size}, attempt)

    def test_key(self) -> None:
        self._spark([{"role": "user", "content": "Reply with the word OK."}], purpose="test", job_id=None,
                    max_tokens=16, temperature=0.0, parse=lambda text, finish_reason, refusal: None, headroom=False)

    def spent_by_model(self) -> dict[str, float]:
        """Estimated USD of this client's successful calls so far, per model."""
        with self._lock:
            return dict(self._spent)

    # --- the plumbing ---------------------------------------------------------

    def _spark(self, messages: list[dict], *, purpose: str, job_id: int | None, max_tokens: int,
               temperature: float, parse: Callable[[str, str, str], Any], response_format: dict | None = None,
               headroom: bool = True):
        if headroom:
            max_tokens = int(max_tokens) + int(self.spark_reasoning_tokens)
        body: dict = {**self.spark_options, "model": MODEL_SPARK, "messages": messages,
                      "max_tokens": int(max_tokens), "temperature": float(temperature)}
        if response_format is not None:
            body["response_format"] = response_format
        estimate = costs.spark_cost(costs.estimate_spark_tokens(messages), 0, int(max_tokens))

        def attempt():
            resp = self._post("/chat/completions", model=MODEL_SPARK, timeout=SPARK_TIMEOUT, json=body)
            answer = self._read_json(resp, MODEL_SPARK)
            usage = costs.spark_usage(answer.get("usage"))
            cost = costs.spark_cost(usage["prompt_tokens"], usage["cached_tokens"], usage["completion_tokens"])
            try:
                result = parse(*_message_text(answer))
            except MetaError as exc:
                exc.cost_usd = cost  # it was answered, so it was paid for
                exc.units = usage
                raise
            return result, cost, usage, resp.status_code

        return self._call(MODEL_SPARK, "/chat/completions", purpose, job_id, estimate, {}, attempt)

    def _call(self, model: str, endpoint: str, purpose: str, job_id: int | None, estimate: float,
              units: dict, attempt: Attempt):
        for tries in (1, 2):
            if self._ledger is not None:
                self._ledger.check_budget(estimate)  # BudgetExceeded: not made, so not recorded
            started = time.monotonic()
            try:
                result, cost, more, status = attempt()
            except MetaError as exc:
                exc.model = exc.model or model
                cost = float(getattr(exc, "cost_usd", 0.0) or 0.0)
                self._record(ApiCall(model=model, endpoint=endpoint, purpose=purpose, ok=False, status=exc.status,
                                     ms=int((time.monotonic() - started) * 1000), cost_usd=cost,
                                     units={**units, **(getattr(exc, "units", None) or {})}, job_id=job_id,
                                     error=self._scrub(str(exc))[:300]))
                self._add_spent(model, cost)
                if exc.retryable and tries == 1:
                    self._sleep(self._wait_before_retry(exc))
                    continue
                raise
            self._record(ApiCall(model=model, endpoint=endpoint, purpose=purpose, ok=True, status=status,
                                 ms=int((time.monotonic() - started) * 1000), cost_usd=cost,
                                 units={**units, **more}, job_id=job_id))
            self._add_spent(model, cost)
            return result
        raise AssertionError("unreachable")  # the loop either returns or raises

    def _post(self, path: str, *, model: str, timeout, stream: bool = False, headers: dict | None = None,
              **kwargs) -> requests.Response:
        name = NAMES.get(model, model)
        sent_headers = {"Authorization": f"Bearer {self._key}", "User-Agent": USER_AGENT}
        sent_headers.update(headers or {})
        try:
            resp = self._session.post(self._base + path, headers=sent_headers, timeout=timeout, stream=stream,
                                      **kwargs)
        except requests.Timeout:
            raise MetaError(f"{name} took too long to answer", retryable=True, model=model) from None
        except requests.ConnectionError:
            raise MetaError(f"Couldn't reach Meta for {name}", retryable=True, model=model) from None
        except requests.RequestException as exc:
            raise MetaError(f"The request to {name} failed ({type(exc).__name__})", model=model) from None
        status = int(getattr(resp, "status_code", 0) or 0)
        if 200 <= status < 300:
            return resp
        detail = self._error_detail(resp)
        try:
            resp.close()
        except Exception:  # noqa: BLE001 - closing is best effort
            pass
        if status in (401, 403):
            words = f"Meta refused the key for {name} ({status})"
        else:
            words = f"{name} answered {status}"
        if detail:
            words += f": {detail}"
        error = MetaError(self._scrub(words), status=status, retryable=status == 429 or status >= 500, model=model)
        error.retry_after = _retry_after(resp)
        raise error

    def _read_json(self, resp, model: str) -> dict:
        try:
            value = resp.json()
        except ValueError:
            raise MetaError(f"{NAMES.get(model, model)} sent an answer that isn't JSON", status=resp.status_code,
                            model=model) from None
        if not isinstance(value, dict):
            raise MetaError(f"{NAMES.get(model, model)} sent an unexpected answer", status=resp.status_code,
                            model=model)
        return value

    def _error_detail(self, resp) -> str:
        detail = ""
        try:
            value = resp.json()
            error = value.get("error") if isinstance(value, dict) else None
            if isinstance(error, dict):
                detail = str(error.get("message") or error.get("code") or "")
            elif isinstance(error, str):
                detail = error
            elif isinstance(value, dict):
                detail = str(value.get("message") or value.get("detail") or "")
        except Exception:  # noqa: BLE001 - any body at all
            try:
                detail = str(getattr(resp, "text", "") or "")
            except Exception:  # noqa: BLE001
                detail = ""
        detail = re.sub(r"\s+", " ", self._scrub(detail)).strip()
        return detail[:200]

    def _wait_before_retry(self, exc: MetaError) -> float:
        after = getattr(exc, "retry_after", None)
        if after is not None:
            return min(RETRY_AFTER_MAX_S, after)
        return RETRY_WAIT_S + random.uniform(0, 1.0)

    def _scrub(self, text: str) -> str:
        return str(text).replace(self._key, "[key]")

    def _record(self, call: ApiCall) -> None:
        if self._ledger is not None:
            self._ledger.record(call)

    def _add_spent(self, model: str, usd: float) -> None:
        if usd:
            with self._lock:
                self._spent[model] = round(self._spent.get(model, 0.0) + usd, 6)
