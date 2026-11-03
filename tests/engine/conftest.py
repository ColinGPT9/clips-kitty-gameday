"""Fakes for the engine and Meta client tests: no network, no key."""

from __future__ import annotations

import json
import shutil
from types import SimpleNamespace

import pytest

from gameday import media
from gameday.types import ApiCall, BudgetExceeded

# A valid 2x2 one-bit mask in SAM's record format (every pixel set).
MASK = "data=2,2,!!!!!&t.SKi"


class FakeResponse:
    def __init__(self, status: int = 200, body=None, *, text: str = "", lines=None, headers=None):
        self.status_code = status
        self._body = body
        self.text = text if text or body is None else json.dumps(body)
        self._lines = list(lines or [])
        self.headers = headers or {}
        self.closed = False

    def json(self):
        if self._body is None:
            raise ValueError("no JSON")
        return self._body

    def iter_lines(self):
        for line in self._lines:
            if isinstance(line, Exception):
                raise line
            yield line.encode("utf-8")

    def close(self):
        self.closed = True


class FakeSession:
    """Answers each post with the next item: a FakeResponse, or an exception to raise."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls: list[dict] = []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if not self.answers:
            raise AssertionError("more requests than the test expected")
        item = self.answers.pop(0)
        if callable(item) and not isinstance(item, FakeResponse):
            item = item(url, kwargs)
        if isinstance(item, BaseException):
            raise item
        return item


class ListLedger:
    def __init__(self, limit_usd: float = 25.0):
        self.limit = limit_usd
        self.calls: list[ApiCall] = []
        self.checks: list[float] = []

    @property
    def spent(self) -> float:
        return sum(c.cost_usd for c in self.calls)

    def check_budget(self, estimate_usd: float) -> None:
        self.checks.append(estimate_usd)
        if self.spent + estimate_usd > self.limit:
            raise BudgetExceeded("The Meta budget would be passed")

    def record(self, call: ApiCall) -> None:
        self.calls.append(call)


def box_line(frame: int, records: list[tuple[int, int, int, int, int, float | None]], w: int = 640,
             h: int = 360) -> str:
    """One frame of SAM's record text: records are (id, x1, y1, x2, y2, confidence)."""
    parts = []
    for obj, x1, y1, x2, y2, conf in records:
        c = "" if conf is None else f";c={conf}"
        parts.append(f"{obj}<|box;x1={x1};y1={y1};x2={x2};y2={y2};w={w};h={h}{c}|><|mask;x=0;y=0;{MASK}|>")
    return f"<{frame}f>" + ",".join(parts)


def sse_lines(text: str, *, chunk: int = 37, fail: dict | None = None, complete: bool = True) -> list[str]:
    """A SAM 3.1 answer as server-sent event lines, the text cut into deltas."""
    events = [{"type": "response.created"}, {"type": "response.in_progress"},
              {"type": "response.output_item.added"}, {"type": "response.content_part.added"}]
    events += [{"type": "response.output_text.delta", "delta": text[i:i + chunk]}
               for i in range(0, len(text), chunk)]
    if fail is not None:
        events.append(fail)
    elif complete:
        events += [{"type": "response.output_text.done", "text": text}, {"type": "response.content_part.done"},
                   {"type": "response.output_item.done"}, {"type": "response.completed"}]
    lines = []
    for event in events:
        lines += [f"event: {event['type']}", "data: " + json.dumps(event), ""]
    return lines


@pytest.fixture
def fakes():
    return SimpleNamespace(Response=FakeResponse, Session=FakeSession, Ledger=ListLedger, box_line=box_line,
                           sse_lines=sse_lines)


@pytest.fixture
def need_ffmpeg():
    if shutil.which(media.FFMPEG) is None or shutil.which(media.FFPROBE) is None:
        pytest.skip("ffmpeg and ffprobe are needed")
