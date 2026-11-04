"""Each offer's one-line summary, written by Muse Spark.

An offer's summary_text says in one line what its clips are. The relay keeps
it with the offer (it cuts it at 300 characters) and shows it in three
places: Muse says it when it lists what's ready and when it asks which offer
to give the link for (meta-relay/lib/muse.js), and the approval page shows
it under the video's title (meta-relay/lib/page.js). The phone alert for a
game that's ready says it too (gameday/notify.py, from the worker).

Whenever an offer is made of the owner's clips and there is a Meta key,
Muse Spark writes that line: one small request per offer (MetaAPI.chat_json,
purpose "offer_line"; MetaClient asks Muse Spark at low reasoning effort),
told only how many clips there are, the counts, and each clip's title and
kind of play, best first. Never the commentary, a player or team field, the
owner's "Teams or players" or the credit line. The line is kept on the
offer, so a resend sends the same words and an offer of the same clips
reuses it. The call is on the usage ledger with the job's other calls, in
its Captions stage.

What comes back is cleaned (clean): one line, with no tabs or runs of
spaces and no emoji. Quotes come off when one pair wraps all of it, then
any . ! ? ; : , or … at its end, as Muse's list puts a full stop after it.
Then it is checked (problem). It is not used when it isn't a string, is
empty or longer than MAX_UNITS, or holds a link or a hashtag. Nor is it
when it says a number the clips don't give: the counts, how many clips
there are, or a number in a clip's title or label. Nor when a number right
before a kind of play isn't that kind's count, as "3 dunks" or "Three
dunks" when there were two, or "Two steals" when there were none; with a
word or two between ("two poster dunks"), it may be fewer, never more.
Numbers are digits, and the words two to twenty before a plural noun, but
"three" (a play too) only before a kind of play or clips, highlights,
plays or moments, and never after "a" or "an" (numbers). "3-pointers",
"3s", "3's" and three-point shots or buckets are threes, not the number 3;
a "three-point play" is an and-one, not a three. Without a line, as with
no key or when Muse Spark fails, the offer carries the counted summary
(summary.ready_text). So an offer never waits on a model, and never says
"3 dunks" when there were two.

A game uploaded with the judge login is never offered (notifier.py); this
checks again, and never asks for its line.
"""

import json
import re
import unicodedata

from gameday import access
from gameday.remote_approval import store, summary
from gameday.remote_approval.captions import offer_kind, units
from gameday.types import MODEL_SPARK

PURPOSE = "offer_line"
#: Where the line is kept, in the offer's summary.
KEY = "offer_line"
#: The longest line used, counted as the relay and Meta count (the relay keeps 300).
MAX_UNITS = 200
MAX_TOKENS = 300
#: At most this many clips are described, best first.
MAX_CLIPS = 10

SCHEMA = {
    "type": "object",
    "properties": {"line": {"type": "string"}},
    "required": ["line"],
    "additionalProperties": False,
}

_TELL = (
    "You write the one-line summary of the highlight clips just cut from {what}, for {who}. It is shown "
    "under the video's title on the page where they approve the clips, their assistant gives it when they "
    "ask what's ready, and it is sent to their phone as an alert. Write only that line: friendly, and "
    "specific to the best {plays} below (each clip's title, and its kind of {play}), at most {limit} "
    "characters, on one line, with no full stop at the end. Use ONLY what is given: never invent a score, a "
    "statistic, a result, a team or a name. Name a player or a team only if a clip's title names them. If "
    "you give a number, it must be one of the counts given. No hashtags, no links, no emojis. Answer with "
    "JSON matching the schema."
)

#: The instructions for each kind of offer (summary.kind_of).
INSTRUCTIONS_FOR = {
    "basketball": _TELL.format(what="a basketball game", who="a basketball highlights creator", plays="plays",
                               play="play", limit=MAX_UNITS),
    "video": _TELL.format(what="a video", who="a creator", plays="moments", play="moment", limit=MAX_UNITS),
}

_LINK = re.compile(r"(?i)://|\bwww\.|\b[a-z0-9-]+\.(?:com|net|org|io|ly|me|app|co|tv|gg|ai)\b")
_HASHTAG = re.compile(r"#\w")
#: Not drawn: the joiner, the variation selectors and the keycap that make emoji up.
_EMOJI_PARTS = frozenset("\N{ZERO WIDTH JOINER}\N{VARIATION SELECTOR-15}\N{VARIATION SELECTOR-16}"
                         "\N{COMBINING ENCLOSING KEYCAP}")
#: Quotes around the whole line: straight, and curly.
_QUOTES = (('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’"))
#: A single quote between two letters is an apostrophe ("Smith's"), not a quote.
_APOSTROPHE = re.compile(r"(?<=[^\W\d_])['’](?=[^\W\d_])")

#: A word: letters and digits, with any apostrophes and hyphens inside it ("Smith's", "three-pointers").
_WORD = re.compile(r"[^\W_]+(?:['’-][^\W_]+)*")
#: "3-pointer", "three pointers": the play, written one way so its 3 is not read as a number.
_POINTER = re.compile(r"(?i)\b(?:3|three)[- ]?pointer(s?)\b")
#: "3s", "3's": threes too, read as "threes".
_THREES = re.compile(r"(?i)\b3['’]?s\b")
#: "three-point shots", "a 3-point bucket": threes too, read as "three(s)"; a "three-point play" is an and-one.
_THREE_POINT = re.compile(r"(?i)\b(?:3|three)[- ]?point[- ](?:shot|bucket)(s?)\b")
#: The words two to twenty: numbers when a plural noun follows ("two dunks").
_NUMBER_WORDS = {word: n for n, word in enumerate((
    "two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen "
    "eighteen nineteen twenty").split(), start=2)}
#: Each kind of play's names (summary.CATEGORIES), singular and plural, as words, and its category.
_PLAYS = {**{tuple(name.split()): cid for cid, *names in summary.CATEGORIES for name in names},
          ("three-pointer",): "threes", ("three-pointers",): "threes"}
#: Nouns any given number may count: "5 clips", "3 big plays".
_ANY = frozenset("clip clips highlight highlights play plays moment moments".split())
#: Words a number's noun is never past: "two of the dunks" doesn't count the dunks.
_STOP = frozenset("a an and as at but by for from in into of on or plus than that the then to with".split())


def facts(review: dict) -> dict:
    """What Muse Spark is told: how many clips, the counts, and each clip's
    title and kind of play, best first."""
    info = review.get("summary") or {}
    return {
        "clip_count": int(info.get("offered") or len(review["clips"])),
        "counts": str(info.get("counts_text") or ""),
        "clips": [{"title": str(c.get("title") or c.get("label") or ""),
                   "kind": summary.word_for(str(c.get("category") or ""))} for c in review["clips"][:MAX_CLIPS]],
    }


def clean(text) -> str:
    """One line: control characters and every run of whitespace become one
    space, emoji are dropped, and quotes come off when one pair wraps the
    whole line (not two quoted phrases at its ends). Then any . ! ? ; : , or
    … at its end goes, as Muse's list puts a full stop after the line. A
    line that isn't a string (only if Meta broke the schema) is empty."""
    kept = []
    for ch in (text if isinstance(text, str) else ""):
        group = unicodedata.category(ch)
        if group in ("So", "Cf", "Cs", "Co", "Cn") or ch in _EMOJI_PARTS or 0x1F3FB <= ord(ch) <= 0x1F3FF:
            continue  # pictographs and emoji, invisible marks, and the skin tones
        kept.append(" " if group == "Cc" else ch)
    line = re.sub(r"\s+", " ", "".join(kept)).strip()
    for left, right in _QUOTES:
        inside = line[1:-1]
        wrapped = len(line) > 1 and line[0] == left and line[-1] == right
        if wrapped and not any(q in _APOSTROPHE.sub("", inside) for q in (left, right)):
            line = inside.strip()
    return line.rstrip(" .!?;:,…").strip()


def _phrases(text) -> list[list[str]]:
    """The text's words in lower case, in runs that nothing but spaces part:
    "2 dunks, 1 three" is [["2", "dunks"], ["1", "three"]]."""
    text = _POINTER.sub(r"three-pointer\1", str(text or ""))
    text = _THREE_POINT.sub(r"three\1", _THREES.sub("threes", text))
    phrases, end = [], None
    for m in _WORD.finditer(text):
        if end is None or text[end:m.start()].strip():
            phrases.append([])
        phrases[-1].append(m.group().lower())
        end = m.end()
    return phrases


def _plural(word: str) -> bool:
    return len(word) > 2 and word.endswith("s") and not word.endswith(("ss", "'s", "’s"))


def _counted(words: list[str], i: int) -> tuple[str, bool] | None:
    """What the number words[i] counts, and whether it comes right before
    it: a kind of play's category, "*" for clips, highlights, plays or
    moments, or "" for another plural noun. A word or two may come between
    ("two poster dunks"); None when no such noun follows, or a number or a
    word like "of" or "and" (_STOP) comes first."""
    for j in range(i + 1, min(i + 4, len(words))):
        for name in (tuple(words[j:j + 2]), (words[j],)):  # "clutch shots", "dunks"
            if name in _PLAYS:
                return _PLAYS[name], j == i + 1
        if words[j] in _ANY:
            return "*", j == i + 1
        if words[j] in _STOP or words[j].isdecimal() or words[j] in _NUMBER_WORDS:
            return None
        if _plural(words[j]):
            return "", j == i + 1
    return None


def _after_a(words: list[str], i: int) -> bool:
    """Is words[i] the play in "a three" or "a corner three"?"""
    for word in reversed(words[max(0, i - 3):i]):
        if word in ("a", "an"):
            return True
        if word in _STOP or word.isdecimal() or word in _NUMBER_WORDS:
            return False
    return False


def numbers(text) -> list[tuple[int, str | None, bool]]:
    """Each number the text says, with what it counts and whether it comes
    right before that (_counted). Digits are numbers, but for the 3 of
    "3-pointer", "3s", "3's" and "3-point shot": those are threes, as are
    "three-point shots" and "buckets" (not a "three-point play", an
    and-one). The words two to twenty are numbers when a kind of play or
    another plural noun follows, right after or a word or two later with
    only spaces between: "two dunks", "Three big blocks", "two
    three-pointers", but not "two of the dunks". "Three" is a play too, so
    it is a number only before a kind of play or clips, highlights, plays or
    moments, and never after "a" or "an": not in "a corner three steals the
    show" or "three seals it"."""
    found = []
    for words in _phrases(text):
        for i, word in enumerate(words):
            if word in _NUMBER_WORDS:
                what = _counted(words, i)
                if word == "three" and what and (not what[0] or _after_a(words, i)):
                    continue  # the play
                if what:
                    found.append((_NUMBER_WORDS[word], *what))
            elif word.isdecimal():
                found.append((int(word), *(_counted(words, i) or (None, False))))
            else:  # digits inside a word: "4th", "2-for-2"
                found += [(int(n), None, False) for n in re.findall(r"\d+", word)]
    return found


def _counts(review: dict) -> dict[str, int]:
    """How many clips of each kind of play the offer has (summary.counts)."""
    out = {}
    for cid, value in ((review.get("summary") or {}).get("counts") or {}).items():
        try:
            out[cid] = int(value)
        except (TypeError, ValueError):
            pass
    return out


def given_numbers(review: dict) -> set[int]:
    """The numbers the line may say: the counts, how many clips, and any a
    clip's title or label says (numbers)."""
    info = review.get("summary") or {}
    out = {int(info.get("offered") or 0), int(info.get("total_clips") or 0), *_counts(review).values()}
    for clip in review["clips"]:
        out |= {n for text in (clip.get("title"), clip.get("label")) for n, _, _ in numbers(text)}
    return out


def problem(line: str, review: dict) -> str:
    """Why this line can't be the offer's summary, or "" when it can.

    It can't be empty or longer than MAX_UNITS, or hold a link or a hashtag.
    Every number it says (numbers) must be one given_numbers holds. A number
    right before a kind of play ("3 dunks", "two blocks") must be that
    kind's count, 0 when the counts don't have it ("two steals" when none
    were), and one with a word or two between ("two poster dunks") may be
    fewer, never more. Before any other noun (clips, highlights, plays or
    moments, as in "5 big plays", or another plural noun), any given number
    goes."""
    if not line:
        return "Muse Spark's line was empty."
    if units(line) > MAX_UNITS:
        return f"Muse Spark's line was longer than {MAX_UNITS} characters."
    if _LINK.search(line):
        return "Muse Spark's line held a link."
    if _HASHTAG.search(line):
        return "Muse Spark's line held a hashtag."
    said = numbers(line)
    odd = sorted({n for n, _, _ in said} - given_numbers(review))
    if odd:
        return f"Muse Spark's line said {odd[0]}, a number the clips don't have."
    counts = _counts(review)
    for n, what, right_before in said:
        have = counts.get(what, 0 if what in _PLAYS.values() else None)
        if have is not None and (n > have or (right_before and n != have)):
            told = summary.counts_text({what: n})
            there = summary.counts_text({what: have}) if have else "none"
            return f"Muse Spark's line said {told}, and the clips have {there}."
    return ""


def line_of(review: dict) -> str:
    """The line Muse Spark wrote for exactly these clips, or ""."""
    got = (review.get("summary") or {}).get(KEY)
    if not isinstance(got, dict) or got.get("fingerprint") != review.get("fingerprint"):
        return ""
    return str(got.get("line") or "")


def summary_text(review: dict) -> str:
    """The offer's summary_text, what Muse says and the approval page shows:
    Muse Spark's line, or else the counted summary."""
    return line_of(review) or summary.ready_text(review["summary"])


def _keep(db, review: dict, line: str) -> None:
    kept = {"line": line, "fingerprint": review["fingerprint"], "model": MODEL_SPARK}
    store.update_review(db, review["id"], summary={**(review.get("summary") or {}), KEY: kept})


def _reuse(db, review: dict) -> bool:
    """A line already written for exactly these clips, on an earlier offer of the same job."""
    for other in store.reviews_for_job(db, review["job_id"]):
        line = line_of(other) if other["id"] != review["id"] and other["fingerprint"] == review["fingerprint"] else ""
        if line:
            _keep(db, review, line)
            store.audit(db, review["id"], "desktop", "muse_spark_offer_line", f"the line of offer {other['id']}")
            return True
    return False


def _skipped(db, review_id: int, why: str) -> str:
    why = str(why or "Muse Spark was not available.")[:200]
    store.audit(db, review_id, "desktop", "offer_line_skipped", why)
    return why


def write(db, review_id: int, meta) -> str:
    """Muse Spark's line for this offer, kept on it. "" when it was written
    (or one already was), else why not: the offer then carries the counted
    summary."""
    review = store.get_review(db, review_id)
    if review is None:
        return "The offer is gone."
    if access.made_by_judge(db.get_job(review["job_id"]) or {}):
        return access.NOT_OFFERED
    if line_of(review) or _reuse(db, review):
        return ""
    messages = [
        {"role": "system", "content": INSTRUCTIONS_FOR[offer_kind(review)]},
        {"role": "user", "content": json.dumps(facts(review), ensure_ascii=False)},
    ]
    try:
        answer = meta.chat_json(messages, SCHEMA, name="clips_kitty_offer_line", purpose=PURPOSE,
                                job_id=review.get("job_id"), max_tokens=MAX_TOKENS, temperature=0.5)
    except Exception as e:  # the counted summary goes instead: an offer never waits on a model
        return _skipped(db, review_id, str(e))
    line = clean(answer.get("line") if isinstance(answer, dict) else "")
    why = problem(line, review)
    if why:
        return _skipped(db, review_id, why)
    _keep(db, review, line)
    store.audit(db, review_id, "desktop", "muse_spark_offer_line", f"{units(line)} characters, {MODEL_SPARK}")
    return ""
