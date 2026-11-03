"""Estimated prices of Meta calls, from the published figures in gameday.types."""

from __future__ import annotations

from gameday.types import SAM_USD_PER_1K_FRAMES, SPARK_USD_PER_M, VOICE_USD_PER_HOUR


def voice_cost(audio_seconds: float) -> float:
    """Muse Voice is priced per hour of audio sent."""
    return round(max(0.0, audio_seconds) / 3600.0 * VOICE_USD_PER_HOUR, 6)


def sam_cost(frames: int) -> float:
    """SAM 3.1 is priced per frame of the video sent."""
    return round(max(0, frames) / 1000.0 * SAM_USD_PER_1K_FRAMES, 6)


def spark_cost(prompt_tokens: int, cached_tokens: int, completion_tokens: int) -> float:
    """Muse Spark: fresh input, cached input and output tokens each have a price."""
    cached = max(0, min(cached_tokens, prompt_tokens))
    fresh = max(0, prompt_tokens - cached)
    usd = (fresh * SPARK_USD_PER_M["input"] + cached * SPARK_USD_PER_M["cached"]
           + max(0, completion_tokens) * SPARK_USD_PER_M["output"])
    return round(usd / 1_000_000, 6)


def spark_usage(usage: dict | None) -> dict:
    """The token counts in a chat answer's `usage` block (cached tokens included)."""
    usage = usage if isinstance(usage, dict) else {}
    details = usage.get("prompt_tokens_details")
    details = details if isinstance(details, dict) else {}

    def count(value) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    out = {
        "prompt_tokens": count(usage.get("prompt_tokens")),
        "cached_tokens": count(details.get("cached_tokens")),
        "completion_tokens": count(usage.get("completion_tokens")),
    }
    done = usage.get("completion_tokens_details")
    if isinstance(done, dict) and done.get("reasoning_tokens") is not None:
        out["reasoning_tokens"] = count(done.get("reasoning_tokens"))  # part of completion_tokens
    return out


def estimate_spark_tokens(messages: list[dict]) -> int:
    """A generous guess at the input tokens of `messages`, for the budget check.

    Text is counted at about three characters a token; an attached image is
    counted as 2,000 tokens whatever its size.
    """
    chars = 0
    images = 0
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            chars += len(content)
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "image_url":
                    images += 1
                else:
                    chars += len(str(part.get("text") or ""))
        chars += 16  # role and framing
    return chars // 3 + 1 + images * 2000
