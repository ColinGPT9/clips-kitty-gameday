"""scripts/provenance_check.py finds code shared with a reference repository,
and lets common boilerplate through.

Each test makes a tiny reference repository with git, then a folder standing
in for this repository, and runs the script on them as CI does.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "provenance_check.py"
GIT = ["git", "-c", "user.name=Provenance Test", "-c", "user.email=provenance@example.com",
       "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", "-c", "core.autocrlf=false"]


def _load():
    spec = importlib.util.spec_from_file_location("provenance_check", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look the module up
    spec.loader.exec_module(module)
    return module


pc = _load()

# ---- the reference: code from "before the event" ----------------------------------------------

CLOCK = '''"""Time stamps for subtitle files."""

import math
import os
import re
import sys
from pathlib import Path


def seconds_to_stamp(total: float) -> str:
    """12.5 -> "00:00:12.500", the way subtitle files want it."""
    millis = int(round(total * 1000))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    seconds, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def main():
    try:
        run()
    except KeyboardInterrupt:
        pass
    finally:
        cleanup()


if __name__ == "__main__":
    main()
'''

SETTINGS = '''import json
import os


# Read the settings file and give back what is in it.
# A missing file gives an empty dict.
# So does a file that is not JSON.
def load(path):
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)
'''

CLAMP = """// Keep a value between two others.
export function clamp(value, low, high) {
  if (value < low) return low;
  if (value > high) return high;
  return value;
}

export const VERSION = "1";
"""

GUIDE = """# Guide

Start the app with one command and open the page it prints.
Upload a game from the dashboard and wait for the clips.
Each clip says how much of the time the ball was followed.
Approve the clips on your phone before anything is posted.
Nothing is posted until you type your PIN on that page.
"""

# ---- this repository ----------------------------------------------------------------------------

# CLOCK's function, moved into a class, re-indented and respaced, with
# comments and blank lines added. Still the same seven lines of code.
COPIED = '''class Stamps:
    # Reformatted, but the same code.
    @staticmethod
    def seconds_to_stamp(total: float) -> str:

        """12.5 -> "00:00:12.500", the way subtitle files want it."""
        millis = int(round(total   *   1000))
        # hours first
        hours, millis = divmod(millis, 3_600_000)

        minutes, millis = divmod(millis, 60_000)
        seconds, millis = divmod(millis, 1000)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"
'''

# Shares with the reference: the import block, the comments, four lines of
# SETTINGS' function (the fifth differs), a try/except/finally of one-word
# lines and the __main__ guard. None of it is five lines of real code in a row.
BOILERPLATE = '''"""Something else entirely."""

import math
import os
import re
import sys
from pathlib import Path

LIMITS = {
    "clips": 12,
}


# Read the settings file and give back what is in it.
# A missing file gives an empty dict.
# So does a file that is not JSON.
def read_settings(path):
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)
    # (never reached)


def start():
    try:
        run()
    except KeyboardInterrupt:
        pass
    finally:
        cleanup()


if __name__ == "__main__":
    start()
'''

# CLAMP's function and its comment: six lines in common, but the comment
# and the closing bracket don't count, which leaves four.
CLAMP_TOO = """// Keep a value between two others.
export function clamp(value, low, high) {
  if (value < low) return low;
  if (value > high) return high;
  return value;
}

export function double(value) { return value * 2; }
"""


def _git(where: Path, *args: str) -> str:
    done = subprocess.run([*GIT, "-C", str(where), *args], check=True, capture_output=True, text=True)
    return done.stdout.strip()


def _write(folder: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture
def reference(tmp_path: Path) -> tuple[Path, str]:
    """A git repository holding CLOCK, SETTINGS and GUIDE in one commit, with
    its working tree emptied afterwards: the check must read git's objects."""
    ref = tmp_path / "reference"
    ref.mkdir()
    _git(ref, "init", "-q")
    files = {"server/clock.py": CLOCK, "server/settings.py": SETTINGS, "web/clamp.js": CLAMP, "docs/guide.md": GUIDE}
    _write(ref, files)
    _git(ref, "add", "-A")
    _git(ref, "commit", "-q", "-m", "Before the event")
    rev = _git(ref, "rev-parse", "HEAD")
    for name in files:
        (ref / name).unlink()
    return ref, rev


def run_check(root: Path, ref: Path, rev: str, *extra: str) -> tuple[int, dict]:
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), "--reference", str(ref), "--rev", rev, "--json", *extra],
        capture_output=True, text=True, encoding="utf-8", cwd=root,
    )
    assert done.returncode in (0, 1), done.stderr
    return done.returncode, json.loads(done.stdout)


def test_a_copied_block_is_found(tmp_path, reference):
    ref, rev = reference
    here = tmp_path / "here"
    _write(here, {"gameday/stamps.py": COPIED, "gameday/fresh.py": "VALUE = 1\n"})

    status, out = run_check(here, ref, rev)

    assert status == 1
    assert out["ok"] is False
    assert out["files_checked"] == 2
    assert [(m["path"], m["start"], m["end"], m["windows"], m["reference"]) for m in out["matches"]] == [
        ("gameday/stamps.py", 4, 13, 3, {"path": "server/clock.py", "start": 10, "end": 16}),
    ]
    assert out["matches"][0]["text"] == "def seconds_to_stamp(total: float) -> str:"


def test_boilerplate_is_not_a_match(tmp_path, reference):
    ref, rev = reference
    here = tmp_path / "here"
    _write(here, {"gameday/app.py": BOILERPLATE, "gameday/static/clamp.js": CLAMP_TOO})

    status, out = run_check(here, ref, rev)

    assert (status, out["matches"], out["doc_matches"]) == (0, [], [])
    assert out["files_checked"] == 2


def test_shared_markdown_is_only_a_warning(tmp_path, reference):
    ref, rev = reference
    here = tmp_path / "here"
    _write(here, {"README.md": "# Read me\n\n" + GUIDE.split("\n", 2)[2]})

    status, out = run_check(here, ref, rev)

    assert status == 0
    assert out["matches"] == []
    assert [(m["path"], m["reference"]["path"]) for m in out["doc_matches"]] == [("README.md", "docs/guide.md")]


def test_excluded_paths_are_not_checked(tmp_path, reference):
    ref, rev = reference
    here = tmp_path / "here"
    _write(here, {"gameday/stamps.py": COPIED})

    status, out = run_check(here, ref, rev, "--exclude", "gameday/*")

    assert (status, out["files_checked"], out["matches"]) == (0, 0, [])


def test_in_a_git_checkout_it_checks_what_git_would_commit(tmp_path, reference):
    """New files count; ignored ones (and the folders it always skips) don't."""
    ref, rev = reference
    here = tmp_path / "here"
    here.mkdir()
    _git(here, "init", "-q")
    _write(here, {".gitignore": "scratch/\n", "scratch/copied.py": COPIED, "node_modules/x/copied.js": COPIED,
                  "gameday/stamps.py": COPIED})

    status, out = run_check(here, ref, rev)

    assert status == 1
    assert [m["path"] for m in out["matches"]] == ["gameday/stamps.py"]


def test_it_says_when_it_cannot_run(tmp_path, reference):
    ref, _ = reference
    here = tmp_path / "here"
    _write(here, {"gameday/stamps.py": COPIED})
    for args in (["--reference", str(ref), "--rev", "0" * 40], ["--reference", str(tmp_path / "missing")]):
        done = subprocess.run([sys.executable, str(SCRIPT), "--root", str(here), *args],
                              capture_output=True, text=True, encoding="utf-8")
        assert done.returncode == 2, done.stdout
        assert "provenance_check:" in done.stderr


def test_list_blobs_names_each_file_it_reads(reference):
    ref, rev = reference
    done = subprocess.run([sys.executable, str(SCRIPT), "--reference", str(ref), "--rev", rev, "--list-blobs"],
                          capture_output=True, check=True)
    expected = sorted(_git(ref, "rev-parse", f"{rev}:{name}")
                      for name in ("server/clock.py", "server/settings.py", "web/clamp.js", "docs/guide.md"))
    assert done.stdout.decode().split("\n") == [*expected, ""]  # "\n" endings, even on Windows


def test_normalizing():
    py = pc.normalize("x = {\n\n    # a note\n    'a':   1,\n}\n", ".py")
    assert (py.lines, py.numbers) == (["x = {", "'a': 1,"], [1, 4])

    js = pc.normalize("/**\n * Docs.\n */\nimport {\n  a,\n  b,\n} from './x.js';\n// note\nrun(a, b, c);\n", ".js")
    assert js.lines == ["import {", "a,", "b,", "} from './x.js';", "run(a, b, c);"]
    assert js.plain == [True, True, True, True, False]

    html = pc.normalize("<!-- a\ncomment -->\n<p>Hi</p>\n", ".html")
    assert html.lines == ["<p>Hi</p>"]

    byte_order_mark = bytes([0xEF, 0xBB, 0xBF])
    assert pc.decode(byte_order_mark + b"import os\n") == "import os\n"
    assert pc.decode(b"GIF89a" + bytes(10)) is None  # binary


def test_trivial_lines():
    for line in ("return None", 'if __name__ == "__main__":', "</div>", "except KeyboardInterrupt:",
                 '<meta name="viewport" content="width=device-width, initial-scale=1">'):
        assert pc.is_trivial(line, ".py" if "<" not in line else ".html"), line
    assert pc.is_trivial("os: [ubuntu-latest, windows-latest]", ".yml")
    assert not pc.is_trivial("if not os.path.exists(path):", ".py")
    assert not pc.is_trivial("run: python -m pip install -r requirements.txt", ".yml")
