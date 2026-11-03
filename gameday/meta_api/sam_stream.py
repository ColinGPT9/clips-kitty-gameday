"""Reading SAM 3.1's streamed answer.

SAM 3.1 answers POST /responses as server-sent events. The boxes arrive as
text in `response.output_text.delta` events, in SAM's record format, which
Meta's own `meta_sam_parser` reads. This module turns the event lines into
`Box` records and stops with a MetaError when the stream says it failed.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field

import meta_sam_parser

from gameday.types import MODEL_SAM, Box, MetaError

# Error codes in a failed stream that are worth one more try.
_RETRYABLE_CODES = {"server_error", "rate_limit_exceeded", "rate_limited", "overloaded", "timeout",
                    "service_unavailable", "internal_error"}


@dataclass
class SamAnswer:
    boxes: list[Box]
    completed: bool  # the stream ended with response.completed
    incomplete: str = ""  # why, when it ended with response.incomplete
    text_chars: int = 0
    events: Counter = field(default_factory=Counter)
    problems: int = 0  # lines the parser couldn't read


def _event_error(event: dict) -> tuple[str, str]:
    """(message, code) from a response.failed or error event."""
    candidates = [event.get("error"), (event.get("response") or {}).get("error"), event]
    for item in candidates:
        if isinstance(item, dict) and (item.get("message") or item.get("code")):
            return str(item.get("message") or "")[:200], str(item.get("code") or item.get("type") or "")
    return "", ""


def _object_id(text: str, ids: dict[str, int]) -> int:
    text = str(text).strip()
    if text.isdigit():
        return int(text)
    # Not a decimal id (the format says it always is): give it a stable number of its own.
    return ids.setdefault(text, 1_000_000 + len(ids))


def _events(lines: Iterable[str | bytes]) -> Iterable[dict | str]:
    """The JSON payloads of the stream's `data:` lines, in order ("[DONE]" as text).

    A payload normally fits on one line; one split over several `data:` lines
    is joined until it reads as JSON or a blank line ends the event.
    """
    pending: list[str] = []
    for raw in lines:
        line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        line = line.rstrip("\r")
        if not line:
            if pending:
                text = "\n".join(pending)
                pending = []
                try:
                    yield json.loads(text)
                except ValueError:
                    continue
            continue
        if not line.startswith("data:"):
            continue  # "event:", "id:", "retry:" and comments
        data = line[5:]
        if data.startswith(" "):
            data = data[1:]
        if not pending and data.strip() == "[DONE]":
            yield "[DONE]"
            continue
        pending.append(data)
        try:
            payload = json.loads("\n".join(pending))
        except ValueError:
            continue
        pending = []
        yield payload
    if pending:
        try:
            yield json.loads("\n".join(pending))
        except ValueError:
            return


def read_sam_stream(lines: Iterable[str | bytes]) -> SamAnswer:
    """Every box in a SAM 3.1 answer, sorted by frame then object id.

    Raises MetaError when the stream reports a failure or stops before it
    says it is complete.
    """
    parser = meta_sam_parser.video_segmentation_format().create_parser()
    events: Counter = Counter()
    chars = 0
    pushed = False
    completed = False
    incomplete = ""
    for event in _events(lines):
        if event == "[DONE]":
            break
        if not isinstance(event, dict):
            continue
        kind = str(event.get("type") or "")
        events[kind] += 1
        if kind == "response.output_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str) and delta:
                parser.push(delta, emit=False)
                chars += len(delta)
                pushed = True
        elif kind == "response.output_text.done":
            text = event.get("text")
            if not pushed and isinstance(text, str) and text:
                # The whole answer came in one piece instead of as deltas.
                parser.push(text, emit=False)
                chars += len(text)
                pushed = True
        elif kind in ("response.failed", "error"):
            message, code = _event_error(event)
            words = f"SAM 3.1 failed: {message}" if message else "SAM 3.1 failed"
            if code:
                words += f" ({code})"
            raise MetaError(words, status=200, retryable=code in _RETRYABLE_CODES, model=MODEL_SAM)
        elif kind == "response.incomplete":
            details = (event.get("response") or {}).get("incomplete_details") or {}
            incomplete = str(details.get("reason") or "incomplete")[:80]
            break
        elif kind == "response.completed":
            completed = True
            break
    if not completed and not incomplete:
        raise MetaError("SAM 3.1's answer stopped before it was complete", status=200, retryable=True,
                        model=MODEL_SAM)
    outcome = (meta_sam_parser.CompletedOutcome() if completed
               else meta_sam_parser.IncompleteOutcome(reason="response", detail=incomplete))
    result = parser.finish(outcome).result
    ids: dict[str, int] = {}
    boxes: list[Box] = []
    for record in result.records:
        if getattr(record, "kind", "") != "box" or record.frame is None:
            continue
        boxes.append(Box(
            frame=int(record.frame.frame_index),
            object_id=_object_id(record.object_id, ids),
            left=float(record.left),
            top=float(record.top),
            right=float(record.right),
            bottom=float(record.bottom),
            confidence=None if record.confidence is None else float(record.confidence),
        ))
    boxes.sort(key=lambda b: (b.frame, b.object_id))
    problems = sum(1 for d in result.diagnostics if d.severity == "error")
    return SamAnswer(boxes=boxes, completed=completed, incomplete=incomplete, text_chars=chars,
                     events=events, problems=problems)
