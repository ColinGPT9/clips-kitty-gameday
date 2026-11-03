"""SAM 3.1's streamed answers, read from canned server-sent events."""

from __future__ import annotations

import json

import pytest

from gameday.meta_api import client as client_mod
from gameday.meta_api.client import MetaClient
from gameday.meta_api.sam_stream import read_sam_stream
from gameday.meta_api.video import VideoFacts
from gameday.types import MODEL_SAM, MetaError

KEY = "test-key-not-real-0123456789"


def test_boxes_come_back_sorted_when_frames_arrive_out_of_order(fakes):
    text = "\n".join([
        fakes.box_line(5, [(1, 100, 50, 115, 65, 0.81)]),
        fakes.box_line(2, [(1, 90, 52, 105, 67, 0.77), (3, 600, 10, 620, 30, 0.95)]),
        fakes.box_line(9, [(1, 120, 40, 135, 55, None)]),
    ]) + "\n"
    answer = read_sam_stream(fakes.sse_lines(text, chunk=11))
    assert answer.completed
    assert [(b.frame, b.object_id) for b in answer.boxes] == [(2, 1), (2, 3), (5, 1), (9, 1)]
    first = answer.boxes[0]
    # x2 and y2 are inclusive in SAM's text; Box is half-open.
    assert (first.left, first.top, first.right, first.bottom) == (90, 52, 106, 68)
    assert first.confidence == pytest.approx(0.77)
    assert answer.boxes[-1].confidence is None


def test_a_failed_stream_raises_with_its_message(fakes):
    failed = {"type": "response.failed",
              "response": {"error": {"code": "invalid_video", "message": "The video could not be decoded"}}}
    lines = fakes.sse_lines(fakes.box_line(0, [(1, 1, 1, 9, 9, 0.9)]) + "\n", fail=failed)
    with pytest.raises(MetaError) as err:
        read_sam_stream(lines)
    assert "could not be decoded" in str(err.value) and not err.value.retryable
    assert err.value.model == MODEL_SAM


def test_an_overload_error_event_can_be_retried(fakes):
    lines = fakes.sse_lines("", fail={"type": "error", "code": "server_error", "message": "busy"})
    with pytest.raises(MetaError) as err:
        read_sam_stream(lines)
    assert err.value.retryable


def test_a_stream_that_stops_early_is_an_error(fakes):
    lines = fakes.sse_lines(fakes.box_line(0, [(1, 1, 1, 9, 9, 0.9)]) + "\n", complete=False)
    with pytest.raises(MetaError) as err:
        read_sam_stream(lines)
    assert err.value.retryable


def test_the_done_text_is_used_when_no_deltas_came(fakes):
    text = fakes.box_line(4, [(2, 10, 10, 19, 19, 0.6)])
    lines = [f"data: {json.dumps({'type': 'response.output_text.done', 'text': text})}", "",
             f"data: {json.dumps({'type': 'response.completed'})}", "", "data: [DONE]"]
    answer = read_sam_stream(lines)
    assert [(b.frame, b.object_id) for b in answer.boxes] == [(4, 2)]


def test_segment_video_sends_the_clip_and_records_its_frames(fakes, tmp_path, monkeypatch):
    video = tmp_path / "proxy.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"x" * 100)
    monkeypatch.setattr(client_mod, "video_facts", lambda path: VideoFacts(640, 360, 10.0, 390))
    text = fakes.box_line(1, [(1, 300, 200, 315, 215, 0.8)]) + "\n" + fakes.box_line(0, [(1, 298, 201, 313, 216, 0.7)])
    session = fakes.Session([fakes.Response(200, lines=fakes.sse_lines(text))])
    ledger = fakes.Ledger()
    seg = MetaClient(KEY, ledger=ledger, session=session).segment_video(video, "basketball", job_id=7)
    assert (seg.noun, seg.width, seg.height, seg.frames, seg.fps) == ("basketball", 640, 360, 390, 10.0)
    assert [b.frame for b in seg.boxes] == [0, 1]
    call = session.calls[0]
    assert call["url"] == "https://api.meta.ai/v1/responses"
    assert call["stream"] is True and call["timeout"] == (20, 900)
    assert call["headers"]["Accept"] == "text/event-stream"
    assert call["headers"]["User-Agent"] == "clips-kitty-gameday/0.1"
    body = call["json"]
    assert body["model"] == "sam-3.1" and body["stream"] is True
    assert body["metadata"] == {"include_confidence": "true", "mask_encoding": "one_bit"}
    content = body["input"][0]["content"]
    assert content[0] == {"type": "input_text", "text": "basketball"}
    assert content[1]["video_url"].startswith("data:video/mp4;base64,")
    [record] = ledger.calls
    assert record.ok and record.model == "sam-3.1" and record.endpoint == "/responses" and record.job_id == 7
    assert record.units["frames"] == 390 and record.cost_usd == pytest.approx(0.078)
    assert ledger.checks == [pytest.approx(0.078)]


def test_a_video_too_big_for_one_request_is_refused_before_sending(fakes, tmp_path, monkeypatch):
    video = tmp_path / "big.mp4"
    video.write_bytes(b"x" * (client_mod.SAM_MAX_BYTES + 1))
    session = fakes.Session([])
    with pytest.raises(MetaError) as err:
        MetaClient(KEY, session=session).segment_video(video, "basketball")
    assert "parts" in str(err.value) and session.calls == []
