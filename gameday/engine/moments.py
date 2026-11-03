"""Picking the plays, and where each clip starts and ends.

Muse Spark reads the commentary and the loudness peaks and proposes moments
in a strict JSON schema. Everything it says is checked here: times are kept
inside the video, names it gives must have been said, and each clip is cut
to the owner's rules:

* Commentary comes after the play, so a clip starts a few seconds before
  the call and keeps the lead-up of the same possession. It never opens on
  a cutaway, nor on a flash (under 2 s) of the shot before the play's.
* It ends at the broadcast's first cut after the play (a close-up, the
  bench, a timeout, a replay wipe), on the frame before it, keeping what the
  announcer says before it, even mid-sentence. Only in one court shot does
  the talk decide: it stops before a sentence begun more than 5.5 s after
  the play (2.5 s after a block or a steal). Nothing of the next scene
  shows at the end. A cut that comes early may leave a clip shorter than the
  minimum length, but not under 6 s.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass

from gameday.engine import loudness as loudness_mod
from gameday.types import CATEGORIES, PipelineOptions, Turn

SHORT_TAIL = ("blocks", "steals")
TAIL_S = 5.5
SHORT_TAIL_S = 2.5
LEAD_BEFORE_CALL_S = 4.0
MAX_LEAD_S = 25.0
MAX_SPEECH_AFTER_S = 15.0
CUT_GUARD_S = 0.04  # an end this far before its cut leaves out every frame of the next shot
SHORTEST_S = 6.0  # an end at an early cut may make a clip shorter than the minimum, but not than this
FLASH_S = 2.0  # an opening shot shorter than this, before a cut, is skipped
LATE_PLAY_S = 2.5  # a play time this far into a cutaway is the call heard over it: the play came before
EVIDENCE_LAG_S = 2.0  # a play time this long after its call began is a guess past the call

PLAIN_TITLES = {
    "clutch": "Clutch play", "dunks": "Huge dunk", "threes": "Big three", "blocks": "Big block",
    "steals": "Big steal", "assists": "Great pass", "fan_reactions": "The crowd goes wild",
    "big_moment": "Big moment", "other": "Highlight", "highlight": "Highlight",
}

# Capitalised words a title may start with that are not names.
_COMMON = set("""a an the and or but so what who how why when where this that these those it its he she they we you
i his her their our my no yes oh wow big huge massive monster nasty clutch late early deep corner fast full quick
great perfect sweet smooth sharp crazy wild one two three four five back to up in on at with from off over under
into through behind between dunk dunks block blocks blocked steal steals three-pointer layup jumper triple
buzzer alley fadeaway step pull stop and-one game crowd bench coach free throw throws shot shots pass
assist assists rejected rejection swat bang splash nothing count cash money unreal
""".split())


@dataclass
class Moment:
    start_s: float
    end_s: float
    play_s: float
    category: str
    label: str
    title: str
    score: float
    team: str = ""
    player: str = ""
    evidence: str = ""
    source: str = "spark"  # "spark", or "loudness" when Muse Spark wasn't used


@dataclass
class Sentence:
    start_s: float
    end_s: float
    text: str


# --- what Muse Spark is asked ---------------------------------------------------

def moment_schema() -> dict:
    """The strict JSON schema of Muse Spark's answer (every property required)."""
    text = {"type": "string"}
    number = {"type": "number"}
    item = {
        "type": "object",
        "properties": {
            "start_s": number,
            "play_s": number,
            "end_s": number,
            "category": {"type": "string", "enum": list(CATEGORIES)},
            "label": text,
            "title": text,
            "team": text,
            "player": text,
            "evidence": text,
            "score": number,
        },
        "required": ["start_s", "play_s", "end_s", "category", "label", "title", "team", "player", "evidence",
                     "score"],
        "additionalProperties": False,
    }
    return {"type": "object", "properties": {"moments": {"type": "array", "items": item}},
            "required": ["moments"], "additionalProperties": False}


BASKETBALL_BRIEF = """You are the editor of a basketball highlights channel. You get the commentary of one game \
as timed turns (seconds from the start of the video) and the times where the sound got unusually loud (the crowd \
and the commentators get loud at big plays). Pick the plays worth posting as short vertical clips.

For each play give:
- play_s: the moment the play happens (the shot goes in, the dunk, the block, the steal). Commentators call a play \
just after it happens, so play_s is usually 0 to 2 seconds before the words that call it.
- start_s: the start of the same possession's lead-up (bringing the ball up, the pass, the drive), a few seconds \
before the call, usually 6 to 15 seconds before play_s.
- end_s: when the commentators finish talking about the play. Never include a new sentence about something else.
- category: clutch, dunks, threes, blocks, steals, assists, fan_reactions, big_moment or other.
- label: a short description for the editor, like "Corner three to tie it".
- title: a short title for viewers, in sentence case, at most 50 characters, no hashtags, no emojis. Use a \
name only if the commentators said it.
- team and player: as the commentators said them, or "" when they didn't.
- evidence: the commentators' words that show the play happened, copied exactly from the transcript.
- score: 0 to 100, how good the clip is. Big plays, clutch moments and a loud crowd score high; an ordinary \
basket scores low.

Never invent a play, a name or a score that is not in the transcript. Plays must not overlap. Best first."""

TALK_BRIEF = """You are the editor of a channel that posts short vertical clips from talk videos (shows, \
podcasts, interviews, press conferences). You get the speech as timed turns (seconds from the start of the \
video) and the times where the sound got unusually loud. Pick the moments worth posting: a strong opinion, a \
funny line, a story with a point, a surprising fact.

For each moment give:
- start_s: the start of the moment's first sentence.
- play_s: when its key line is said.
- end_s: the end of its last sentence. Never stop mid-sentence; never include a new sentence about something else.
- category: highlight, big_moment, fan_reactions or other.
- label: a short description for the editor.
- title: a short title for viewers, in sentence case, at most 50 characters, no hashtags, no emojis. Use a \
name only if it was said.
- team: "". player: the speaker's name only if it was said, otherwise "".
- evidence: the key words, copied exactly from the transcript.
- score: 0 to 100, how good the clip is.

Never invent anything that is not in the transcript. Moments must not overlap. Best first."""


def transcript_lines(turns: list[Turn], *, max_chars: int = 240_000) -> str:
    """The transcript, one timed turn a line, kept under `max_chars`."""
    lines = [f"{t.start_s:.1f}-{t.end_s:.1f} {t.text}" for t in turns]
    text = "\n".join(lines)
    if len(text) <= max_chars:
        return text
    keep = max(1, int(len(lines) * max_chars / len(text)))
    step = len(lines) / keep
    return "\n".join(lines[int(i * step)] for i in range(keep))


def build_messages(turns: list[Turn], peaks: list[loudness_mod.Peak], *, duration_s: float,
                   options: PipelineOptions) -> list[dict]:
    wanted = options.max_clips + 4
    loud = ", ".join(f"{p.t:.1f} (z {p.z:.1f})" for p in sorted(peaks, key=lambda p: p.t)) or "none found"
    hint = options.hint.strip() or "not given"
    user = (f"Video length: {duration_s:.0f} s.\n"
            f"Clips are {options.min_clip_s:.0f} to {options.max_clip_s:.0f} s long. Return up to {wanted} moments.\n"
            f"Teams or players, from the creator: {hint}\n"
            f"Loud moments (seconds, how loud): {loud}\n"
            f"Transcript:\n{transcript_lines(turns)}")
    brief = TALK_BRIEF if options.mode == "talk" else BASKETBALL_BRIEF
    return [{"role": "system", "content": brief}, {"role": "user", "content": user}]


# --- checking what comes back ------------------------------------------------------

def _number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


_WORD = re.compile(r"[a-z0-9]+(?:['\-][a-z0-9]+)*")


def _words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def was_said(name: str, known: str) -> bool:
    """Whether every word of `name` appears in `known` (the transcript and the hint, lower case)."""
    words = [w for w in _words(name) if len(w) > 1]
    return bool(words) and all(re.search(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", known) for w in words)


def _clean(text, limit: int) -> str:
    text = re.sub(r"#\w+", "", str(text or ""))
    text = re.sub(r"[\U00010000-\U0010FFFF☀-➿]", "", text)  # emojis
    text = re.sub(r"\s+", " ", text).strip(" \"'-:;,")
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return text


def _names_ok(text: str, known: str) -> bool:
    """No capitalised word that looks like a name unless it was said."""
    for word in re.findall(r"\b[A-Z][A-Za-z'\-]*", text):
        lower = word.lower()
        if lower in _COMMON or len(lower) < 2:
            continue
        if not was_said(word, known):
            return False
    return True


def parse_moments(answer: dict, *, duration_s: float, known_text: str, mode: str = "basketball") -> list[Moment]:
    """Muse Spark's moments, checked. `known_text` is the transcript and the hint in lower case."""
    found = []
    items = answer.get("moments") if isinstance(answer, dict) else None
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        start, end, play = _number(item.get("start_s")), _number(item.get("end_s")), _number(item.get("play_s"))
        if start is None or end is None:
            continue
        start, end = max(0.0, min(start, duration_s)), max(0.0, min(end, duration_s))
        if end - start < 1.0:
            continue
        if play is None or not start - 2.0 <= play <= end + 2.0:
            play = (start + end) / 2 if play is None else min(max(play, start), end)
        category = str(item.get("category") or "")
        if category not in CATEGORIES:
            category = "highlight" if mode == "talk" else "other"
        score = _number(item.get("score"))
        score = 50.0 if score is None else max(0.0, min(100.0, score))
        title = _clean(item.get("title"), 60)
        if not title or not _names_ok(title, known_text):
            title = PLAIN_TITLES.get(category, "Highlight")
        label = _clean(item.get("label"), 80)
        if not label or not _names_ok(label, known_text):
            label = title
        team, player = _clean(item.get("team"), 60), _clean(item.get("player"), 60)
        found.append(Moment(
            start_s=round(start, 2), end_s=round(end, 2), play_s=round(play, 2), category=category, label=label,
            title=title, score=round(score, 1),
            team=team if team and was_said(team, known_text) else "",
            player=player if player and was_said(player, known_text) else "",
            evidence=_clean(item.get("evidence"), 240), source="spark"))
    return found


def snap_to_loudness(moments: list[Moment], loud: loudness_mod.Loudness) -> None:
    """Move each play to where the crowd's roar began, when one starts near it."""
    for m in moments:
        peak = loudness_mod.nearest_peak(loud, m.play_s, before_s=3.0, after_s=5.0, min_z=1.0)
        if peak is None:
            continue
        began = loudness_mod.onset(loud, peak)
        if m.play_s - 4.0 <= began <= m.play_s + 2.0:
            m.play_s = round(max(m.start_s, began), 2)


def evidence_time(m: Moment, turns: list[Turn]) -> float | None:
    """When the commentators began to say the moment's evidence, or None when it isn't
    in the transcript. Muse Voice times turns, not words, so inside a turn the time is
    shared out by length, as for sentences(): an estimate."""
    words = _words(m.evidence)[:5]
    if len(words) < 3:
        return None
    best = None
    for turn in turns:
        found = list(_WORD.finditer(turn.text.lower()))
        said = [w.group(0) for w in found]
        for i in range(len(said) - len(words) + 1):
            if said[i:i + len(words)] == words:
                share = found[i].start() / max(1, len(turn.text))
                at = turn.start_s + max(0.0, turn.end_s - turn.start_s) * share
                if best is None or abs(at - m.play_s) < abs(best - m.play_s):
                    best = at
    return None if best is None else round(best, 2)


def not_after_the_call(moments: list[Moment], turns: list[Turn]) -> None:
    """A play is called as it happens or just after, so a play time well after the
    commentators began to say its evidence is a guess past the call: move it back."""
    for m in moments:
        called = evidence_time(m, turns)
        if called is not None and m.play_s > called + EVIDENCE_LAG_S:
            m.play_s = round(max(m.start_s, called), 2)


def rough_range(m: Moment, duration_s: float, options: PipelineOptions) -> tuple[float, float]:
    """Where a moment's clip will roughly be, before the cuts are known."""
    if options.mode == "talk":
        start, end = m.start_s, m.end_s
    else:
        start = max(m.play_s - MAX_LEAD_S, min(m.start_s, m.play_s - LEAD_BEFORE_CALL_S))
        end = min(m.play_s + MAX_SPEECH_AFTER_S, max(m.end_s, m.play_s + 2.0))
    if end - start > options.max_clip_s:
        start = end - options.max_clip_s
    if end - start < options.min_clip_s:
        start = end - options.min_clip_s
    start = max(0.0, start)
    end = min(duration_s, max(end, start + options.min_clip_s))
    return round(start, 2), round(end, 2)


def drop_overlaps(moments: list[Moment], limit: int, duration_s: float, options: PipelineOptions,
                  gap_s: float = 1.0) -> list[Moment]:
    """The best `limit` moments whose clips don't overlap, best first."""
    kept: list[tuple[Moment, tuple[float, float]]] = []
    for m in sorted(moments, key=lambda x: -x.score):
        span = rough_range(m, duration_s, options)
        if all(span[1] + gap_s <= other[0] or other[1] + gap_s <= span[0] for _, other in kept):
            kept.append((m, span))
            if len(kept) >= limit:
                break
    return [m for m, _ in kept]


def fallback_moments(loud: loudness_mod.Loudness, *, duration_s: float, options: PipelineOptions,
                     turns: list[Turn] | None = None) -> list[Moment]:
    """The loudest moments as plain picks, for when Muse Spark can't be asked."""
    gap = max(options.min_clip_s + 2.0, 15.0)
    # A muted or silent track (or one steady hum): every hop ties, so the "loudest" would
    # just be the first minutes. It is treated as no sound: the clips spread through the video.
    flat = loudness_mod.flat(loud)
    peaks = [] if flat else loudness_mod.peaks(loud, min_z=1.5, min_gap_s=gap)
    if not flat and len(peaks) < options.max_clips:
        seen = {p.t for p in peaks}
        more = [p for p in loudness_mod.peaks(loud, min_z=-1e9, min_gap_s=gap) if p.t not in seen]
        peaks += [p for p in more if all(abs(p.t - q.t) >= gap for q in peaks)]
    picks = []
    for p in peaks:
        play = loudness_mod.onset(loud, p.t) if options.mode != "talk" else p.t
        picks.append(Moment(start_s=max(0.0, play - 10.0), end_s=min(duration_s, play + 4.0), play_s=play,
                            category="big_moment", label="", title="", score=round(min(95.0, 40.0 + 12.0 * p.z), 1),
                            source="loudness"))
    if not picks:  # no sound at all: spread the clips through the video
        n = max(1, options.max_clips)
        for i in range(n):
            mid = duration_s * (i + 0.5) / n
            picks.append(Moment(start_s=max(0.0, mid - 8.0), end_s=min(duration_s, mid + 8.0), play_s=mid,
                                category="big_moment", label="", title="", score=30.0, source="loudness"))
    picks = drop_overlaps(picks, options.max_clips, duration_s, options)
    for i, m in enumerate(picks, start=1):
        m.title = f"Big moment {i}"
        minutes, seconds = divmod(int(m.play_s), 60)
        m.label = f"Loud moment at {minutes}:{seconds:02d}"
        if turns:
            near = [t.text for t in turns if t.end_s > m.play_s - 2.0 and t.start_s < m.play_s + 4.0]
            m.evidence = _clean(" ".join(near), 240)
    return picks


# --- where each clip starts and ends -----------------------------------------------

def sentences(turns: list[Turn]) -> list[Sentence]:
    """Turns split into sentences. Muse Voice times turns, not words, so each
    sentence's time is shared out by its length: an estimate."""
    out = []
    for turn in turns:
        parts = [p for p in re.split(r"(?<=[.!?])\s+", turn.text.strip()) if p]
        total = sum(len(p) for p in parts) or 1
        t = turn.start_s
        span = max(0.0, turn.end_s - turn.start_s)
        for p in parts:
            d = span * len(p) / total
            out.append(Sentence(round(t, 2), round(t + d, 2), p))
            t += d
    out.sort(key=lambda s: s.start_s)
    return out


Cutaway = Callable[[float, float], bool]  # (shot start, shot end) -> no ball in that shot


def _open_cleanly(start: float, play: float, cuts: list[float], cutaway: Cutaway | None, lo: float) -> float:
    """Move the start so the clip never opens on a flash of the shot before or on a cutaway."""
    latest = play - 3.0
    for _ in range(6):
        nxt = next((c for c in cuts if c > start + 0.05), None)
        if nxt is None or nxt > latest:
            break
        if nxt - start < FLASH_S:
            start = nxt  # a flash of the shot before: skip it
            continue
        shot_start = max([c for c in cuts if c <= start + 0.05] or [lo])
        if cutaway is not None and cutaway(shot_start, nxt):
            start = nxt  # a crowd, bench or close-up shot without the ball
            continue
        break
    return _on_its_cut(start, cuts, lo)


def _on_its_cut(start: float, cuts: list[float], lo: float) -> float:
    """A start just after a cut moves back onto the cut itself."""
    before = [c for c in cuts if c <= start + 1e-6]
    if before and 0 < start - before[-1] < 1.0 and before[-1] >= lo:
        return before[-1]
    return start


def ending_cut(play: float, cuts: list[float], cutaway: Cutaway | None, hi: float) -> float | None:
    """The broadcast's cut that ends the play: the first one after it, or None when
    the court shot runs on. A play time just inside a cutaway is a late guess (the
    call is heard over the close-up after the play), so the play ended where that
    cutaway began."""
    after = next((c for c in cuts if c > play + 0.05), None)
    began = max((c for c in cuts if c <= play + 0.05), default=None)
    if (began is not None and cutaway is not None and play - began <= LATE_PLAY_S
            and cutaway(began, hi if after is None else after)):
        return began
    return after


def _end(m: Moment, play: float, cuts: list[float], sents: list[Sentence], hi: float,
         cutaway: Cutaway | None) -> tuple[float, bool]:
    """Where the clip ends, and whether that is at a cut (an end at a cut never moves later)."""
    tail = SHORT_TAIL_S if m.category in SHORT_TAIL else TAIL_S
    about = [s for s in sents if s.end_s > play - 1.0 and s.start_s <= play + tail]
    later = [s.start_s for s in sents if s.start_s > play + tail]
    hard_stop = min(hi, min(later) - 0.15) if later else hi
    said = bool(about)
    speech_end = min(max(s.end_s for s in about), play + MAX_SPEECH_AFTER_S) if said else play
    talk_end = min(max(play + 2.0, speech_end + 0.4) if said else play + 3.0, hard_stop)
    cut = ending_cut(play, cuts, cutaway, hi)
    # The cut wins over the talk, even mid-sentence; a cut soon after the talk is still where the play ends.
    reach = max(talk_end + (1.2 if said else 5.0), play + tail)
    if cut is not None and cut <= min(reach, hard_stop):
        return cut - CUT_GUARD_S, True
    # One court shot: the talk decides.
    return max(talk_end, min(hi, play + 1.0)), False


def refine(m: Moment, *, cuts: list[float], sents: list[Sentence], duration_s: float, options: PipelineOptions,
           window: tuple[float, float] | None = None, cutaway: Cutaway | None = None) -> tuple[float, float]:
    """The clip's final [start, end] in seconds, by the owner's rules."""
    lo, hi = window or (0.0, duration_s)
    lo, hi = max(0.0, lo), min(duration_s, hi)
    cuts = sorted(c for c in cuts if lo < c < hi)
    play = min(max(m.play_s, lo), hi)
    if options.mode == "talk":
        return _refine_talk(m, cuts=cuts, sents=sents, lo=lo, hi=hi, options=options)

    end, at_cut = _end(m, play, cuts, sents, hi, cutaway)

    # The start.
    calls = [s for s in sents if s.end_s > play - 0.5 and s.start_s < play + 3.0]
    call = calls[0].start_s if calls else play
    start = min(m.start_s, call - LEAD_BEFORE_CALL_S, play - LEAD_BEFORE_CALL_S)
    start = max(start, play - MAX_LEAD_S, lo)
    if end - start > options.max_clip_s:
        start = end - options.max_clip_s  # the ending matters most: lose lead-up
    start = _open_cleanly(start, play, cuts, cutaway, lo)
    if end - start < options.min_clip_s:
        # Short: more lead-up, still opening cleanly. Never more end: a cut that comes early
        # leaves the clip shorter than the minimum.
        start = min(start, _open_cleanly(max(lo, end - options.min_clip_s), play, cuts, cutaway, lo))
    shortest = min(SHORTEST_S, options.min_clip_s)
    if end - start < shortest - 1.0:
        # Still well under 6 s: a cutaway in the lead-up is better than running past the cut that ends
        # the play, or than a clip too short to watch.
        start = _reach_back(start, max(lo, end - shortest), cuts, lo)
    if end - start < options.min_clip_s and not at_cut:
        # One court shot, pinned at the start of the video: the end may grow, but not into the next shot.
        nxt = next((c for c in cuts if c > end + 0.05), None)
        end = max(end, min(hi if nxt is None else nxt - CUT_GUARD_S, start + options.min_clip_s))
    if end - start > options.max_clip_s:
        end = start + options.max_clip_s
    return round(start, 2), round(end, 2)


def _reach_back(start: float, want: float, cuts: list[float], lo: float) -> float:
    """An earlier start, at `want`, for a clip too short even with all its clean lead-up.
    It opens on the shot before after all, but on at least FLASH_S of it, never a flash."""
    if want >= start:
        return start
    nxt = next((c for c in cuts if want + 0.05 < c <= start + 1e-6), None)
    if nxt is not None and nxt - want < FLASH_S:
        shot_start = max([c for c in cuts if c <= want + 1e-6] or [lo])
        want = max(shot_start, nxt - FLASH_S)
    return min(start, _on_its_cut(want, cuts, lo))


def _refine_talk(m: Moment, *, cuts: list[float], sents: list[Sentence], lo: float, hi: float,
                 options: PipelineOptions) -> tuple[float, float]:
    inside = [s for s in sents if lo <= s.start_s and s.end_s <= hi]
    first = next((s for s in inside if s.end_s > m.start_s), None)
    start = max(lo, first.start_s - 0.2) if first else m.start_s
    last = [s for s in inside if s.start_s < m.end_s and s.start_s >= start]
    end = min(hi, last[-1].end_s + 0.3) if last else m.end_s
    if end - start > options.max_clip_s:
        fits = [s for s in inside if s.start_s >= start and s.end_s + 0.3 <= start + options.max_clip_s]
        end = fits[-1].end_s + 0.3 if fits else start + options.max_clip_s
    for s in inside:
        if end - start >= options.min_clip_s:
            break
        if s.start_s >= end - 0.31:
            end = min(hi, s.end_s + 0.3)
    if end - start < options.min_clip_s:
        end = min(hi, start + options.min_clip_s)
    hanging = [c for c in cuts if end - 1.0 < c < end]
    if hanging:
        end = hanging[0]
    flash = [c for c in cuts if start < c < start + 1.0]
    if flash:
        start = flash[-1]
    return round(start, 2), round(end, 2)


def transcript_between(turns: list[Turn], start: float, end: float) -> str:
    return " ".join(t.text for t in turns if t.end_s > start and t.start_s < end)


def known_words(turns: list[Turn], hint: str) -> str:
    return (" ".join(t.text for t in turns) + " " + hint).lower()
