"""MetaClient's budget, ledger, retries and error messages, with a fake session."""

from __future__ import annotations

import json

import pytest
import requests

from gameday.meta_api.client import MetaClient
from gameday.types import BudgetExceeded, MetaError

KEY = "test-key-not-real-0123456789"


def spark_answer(content, *, prompt=1000, cached=200, completion=300, finish="stop"):
    return {"choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": finish}],
            "usage": {"prompt_tokens": prompt, "completion_tokens": completion,
                      "prompt_tokens_details": {"cached_tokens": cached}}}


def make(fakes, answers, limit=25.0):
    session = fakes.Session(answers)
    ledger = fakes.Ledger(limit)
    client = MetaClient(KEY, ledger=ledger, session=session)
    waits = []
    client._sleep = waits.append
    return client, session, ledger, waits


SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"],
          "additionalProperties": False}


def test_chat_json_sends_a_strict_schema_and_records_the_tokens(fakes):
    client, session, ledger, _ = make(fakes, [fakes.Response(200, spark_answer('{"ok": true}'))])
    got = client.chat_json([{"role": "user", "content": "hi"}], SCHEMA, name="check", purpose="moments", job_id=3,
                           max_tokens=500, temperature=0.1)
    assert got == {"ok": True}
    call = session.calls[0]
    assert call["url"] == "https://api.meta.ai/v1/chat/completions"
    assert call["headers"]["Authorization"] == f"Bearer {KEY}"
    assert call["headers"]["User-Agent"] == "clips-kitty-gameday/0.1"
    body = call["json"]
    assert body["model"] == "muse-spark-1.3" and body["temperature"] == 0.1
    assert body["max_tokens"] == 500 + client.spark_reasoning_tokens and body["reasoning_effort"] == "low"
    assert body["response_format"] == {"type": "json_schema",
                                       "json_schema": {"name": "check", "strict": True, "schema": SCHEMA}}
    [record] = ledger.calls
    assert record.ok and record.purpose == "moments" and record.job_id == 3 and record.status == 200
    assert record.units == {"prompt_tokens": 1000, "cached_tokens": 200, "completion_tokens": 300}
    # 800 fresh at $1.25/M, 200 cached at $0.15/M, 300 out at $4.25/M
    assert record.cost_usd == pytest.approx((800 * 1.25 + 200 * 0.15 + 300 * 4.25) / 1e6)
    assert client.spent_by_model() == {"muse-spark-1.3": pytest.approx(record.cost_usd)}
    assert len(ledger.checks) == 1 and ledger.checks[0] > 0


def test_a_fenced_json_answer_is_read(fakes):
    client, *_ = make(fakes, [fakes.Response(200, spark_answer('```json\n{"ok": false}\n```'))])
    assert client.chat_json([{"role": "user", "content": "x"}], SCHEMA, name="c", purpose="t") == {"ok": False}


def test_an_answer_that_isnt_json_fails_but_its_cost_is_kept(fakes):
    client, _, ledger, waits = make(fakes, [fakes.Response(200, spark_answer("not json", finish="length"))])
    with pytest.raises(MetaError) as err:
        client.chat_json([{"role": "user", "content": "x"}], SCHEMA, name="c", purpose="moments")
    assert "ran out of tokens" in str(err.value)
    [record] = ledger.calls
    assert not record.ok and record.cost_usd > 0 and waits == []


def test_the_budget_is_checked_first_and_nothing_is_sent_past_it(fakes):
    client, session, ledger, _ = make(fakes, [], limit=0.0)
    with pytest.raises(BudgetExceeded):
        client.chat_text([{"role": "user", "content": "x"}], purpose="captions")
    assert session.calls == [] and ledger.calls == []


def test_a_429_is_tried_once_more_after_its_retry_after(fakes):
    busy = fakes.Response(429, {"error": {"message": "slow down"}}, headers={"Retry-After": "3"})
    client, session, ledger, waits = make(fakes, [busy, fakes.Response(200, spark_answer("Hello"))])
    assert client.chat_text([{"role": "user", "content": "x"}], purpose="captions") == "Hello"
    assert waits == [3.0] and len(session.calls) == 2
    assert [c.ok for c in ledger.calls] == [False, True]
    assert ledger.calls[0].status == 429 and "slow down" in ledger.calls[0].error
    assert len(ledger.checks) == 2


def test_server_errors_give_up_after_one_retry(fakes):
    client, session, ledger, waits = make(fakes, [fakes.Response(503, text="down"), fakes.Response(502, text="down")])
    with pytest.raises(MetaError) as err:
        client.chat_text([{"role": "user", "content": "x"}], purpose="captions")
    assert err.value.status == 502 and err.value.retryable
    assert len(session.calls) == 2 and len(waits) == 1 and [c.ok for c in ledger.calls] == [False, False]


def test_a_timeout_is_retried(fakes):
    client, _, ledger, waits = make(fakes, [requests.ReadTimeout("read timed out"),
                                            fakes.Response(200, spark_answer("OK"))])
    assert client.chat_text([{"role": "user", "content": "x"}], purpose="captions") == "OK"
    assert len(waits) == 1 and ledger.calls[0].status == 0 and not ledger.calls[0].ok


def test_a_bad_request_is_not_retried(fakes):
    client, session, _, waits = make(fakes, [fakes.Response(400, {"error": {"message": "bad schema"}})])
    with pytest.raises(MetaError) as err:
        client.chat_json([{"role": "user", "content": "x"}], SCHEMA, name="c", purpose="t")
    assert err.value.status == 400 and not err.value.retryable and "bad schema" in str(err.value)
    assert len(session.calls) == 1 and waits == []


def test_the_key_never_appears_in_errors_or_the_ledger(fakes):
    echo = fakes.Response(401, {"error": {"message": f"Invalid key {KEY}"}})
    client, _, ledger, _ = make(fakes, [echo])
    with pytest.raises(MetaError) as err:
        client.test_key()
    assert KEY not in str(err.value) and "refused the key" in str(err.value) and err.value.status == 401
    assert all(KEY not in c.error and KEY not in json.dumps(c.units) for c in ledger.calls)
    assert KEY not in repr(client)
    connection = requests.ConnectionError(f"failed for https://api.meta.ai/v1?key={KEY}")
    client2, *_ = make(fakes, [connection, connection])
    with pytest.raises(MetaError) as err2:
        client2.test_key()
    assert KEY not in str(err2.value) and err2.value.__cause__ is None


def test_test_key_is_one_tiny_spark_call(fakes):
    client, session, ledger, _ = make(fakes, [fakes.Response(200, spark_answer("", completion=16, finish="length"))])
    client.test_key()
    body = session.calls[0]["json"]
    assert body["model"] == "muse-spark-1.3" and body["max_tokens"] <= 16
    assert ledger.calls[0].purpose == "test" and ledger.calls[0].ok


def test_an_empty_key_is_refused():
    with pytest.raises(MetaError):
        MetaClient("  ")
