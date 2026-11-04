"""Turn what `rclone authorize` printed into the token JSON that `rclone config create` wants.

Newer rclone prints the result packed in base64 (it starts with "eyJ"); older rclone prints the
JSON itself (it starts with "{"). A paste may also carry the arrows around it, line breaks from
the terminal, or the whole thing twice. Prints the token JSON, or nothing when the paste isn't
a token. Used by setup-drive-sync.sh:

    python3 scripts/drive_token.py < pasted.txt
"""

import base64
import json
import re
import sys


def _from_json(text: str, depth: int):
    try:
        obj, _ = json.JSONDecoder().raw_decode(text)  # the first object only: a double paste is fine
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    if obj.get("access_token") or obj.get("refresh_token"):
        return obj
    for value in obj.values():  # rclone wraps it: {"token": "<the token JSON>"}
        if isinstance(value, str):
            found = _parse(value, depth + 1)
            if found:
                return found
    return None


def _from_base64(text: str, depth: int):
    blob = re.match(r"[A-Za-z0-9+/_=-]+", text)
    if not blob:
        return None
    blob = blob.group(0)
    half = len(blob) // 2
    if len(blob) % 2 == 0 and blob[:half] == blob[half:]:
        blob = blob[:half]  # pasted twice
    blob = blob.replace("+", "-").replace("/", "_").rstrip("=")
    try:
        data = base64.urlsafe_b64decode(blob + "=" * (-len(blob) % 4)).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    return _from_json(data.strip(), depth + 1)


def _parse(raw: str, depth: int = 0):
    if depth > 4:
        return None
    text = (raw or "").strip()
    between = re.search(r"--->(.*?)<---", text, re.S)
    if between:
        text = between.group(1)
    text = "".join(text.split())  # a token has no spaces; the terminal's line breaks go
    if not text:
        return None
    if text.startswith("{"):
        return _from_json(text, depth)
    return _from_base64(text, depth)


def parse(raw: str) -> dict | None:
    """The token as a dict, or None."""
    return _parse(raw)


if __name__ == "__main__":
    token = parse(sys.stdin.read())
    if token:
        print(json.dumps(token, separators=(",", ":")))
