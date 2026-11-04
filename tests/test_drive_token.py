"""scripts/drive_token.py: whatever `rclone authorize` printed, as the token JSON."""

import base64
import importlib.util
import json
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "drive_token", Path(__file__).resolve().parents[1] / "scripts" / "drive_token.py")
drive_token = importlib.util.module_from_spec(spec)
spec.loader.exec_module(drive_token)

TOKEN = {"access_token": "ya29.test", "token_type": "Bearer", "refresh_token": "1//test-refresh",
         "expiry": "2026-11-04T05:09:04.123-05:00"}
TOKEN_JSON = json.dumps(TOKEN)


def packed(obj, *, urlsafe=True, padded=False) -> str:
    raw = json.dumps(obj).encode()
    text = (base64.urlsafe_b64encode(raw) if urlsafe else base64.b64encode(raw)).decode()
    return text if padded else text.rstrip("=")


def test_the_plain_json_token_is_kept():
    assert drive_token.parse(TOKEN_JSON) == TOKEN


def test_newer_rclones_packed_form_is_unpacked():
    blob = packed({"token": TOKEN_JSON})
    assert blob.startswith("eyJ")
    assert drive_token.parse(blob) == TOKEN
    assert drive_token.parse(packed({"token": TOKEN_JSON}, urlsafe=False, padded=True)) == TOKEN


def test_the_arrows_and_line_breaks_around_it_are_dropped():
    blob = packed({"token": TOKEN_JSON})
    pasted = f"Paste the following into your remote machine --->\n{blob[:40]}\n{blob[40:]}\n<---End paste"
    assert drive_token.parse(pasted) == TOKEN


def test_a_double_paste_still_works():
    blob = packed({"token": TOKEN_JSON})
    assert drive_token.parse(blob + blob) == TOKEN
    assert drive_token.parse(blob + "\n" + blob) == TOKEN
    assert drive_token.parse(TOKEN_JSON + TOKEN_JSON) == TOKEN


def test_anything_else_is_not_a_token():
    for bad in ("", "   ", "hello", "{}", '{"scope": "drive.readonly"}', packed({"scope": "drive.readonly"}),
                "eyJ!!!", "[1, 2]"):
        assert drive_token.parse(bad) is None, bad
