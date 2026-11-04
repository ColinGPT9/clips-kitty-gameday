"""The contracts between the engine, the platform and the dashboard.

They are frozen while the parts are built in parallel: change them only
together. Only Meta's models are called at runtime (MODELS below). ffmpeg does
the cutting; it is a tool, not a model.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

APP_NAME = "Clips Kitty Game Day"

# --- Meta Model API ---------------------------------------------------------

META_API_BASE = "https://api.meta.ai/v1"
MODEL_VOICE = "muse-voice-transcribe-1.0"  # POST /asr/transcribe
MODEL_SPARK = "muse-spark-1.3"  # POST /chat/completions
MODEL_SAM = "sam-3.1"  # POST /responses, streamed
MODELS = (MODEL_VOICE, MODEL_SPARK, MODEL_SAM)

# Published prices in USD. Every figure made from them is shown as an estimate.
SPARK_USD_PER_M = {"input": 1.25, "cached": 0.15, "output": 4.25}  # per 1M tokens
VOICE_USD_PER_HOUR = 0.18
SAM_USD_PER_1K_FRAMES = 0.20


class MetaError(Exception):
    """A Meta API call failed. `status` is the HTTP status, 0 when there was no answer."""

    def __init__(self, message: str, *, status: int = 0, retryable: bool = False, model: str = ""):
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.model = model


class BudgetExceeded(MetaError):
    """The call would take spending past META_BUDGET_USD, so it was not made."""


@dataclass
class ApiCall:
    """One Meta API call, for the usage ledger (the `api_calls` table)."""

    model: str
    endpoint: str  # "/asr/transcribe", "/chat/completions", "/responses"
    purpose: str  # "transcribe", "moments", "captions", "whatsapp_text", "track", "test", ...
    ok: bool
    status: int  # HTTP status, 0 when there was no answer
    ms: int
    cost_usd: float  # estimate, from the prices above
    units: dict = field(default_factory=dict)  # tokens, audio_seconds, frames, bytes
    job_id: int | None = None
    error: str = ""  # short; never holds the key


class Ledger(Protocol):
    def check_budget(self, estimate_usd: float) -> None:
        """Raise BudgetExceeded if spending `estimate_usd` more would pass the budget."""

    def record(self, call: ApiCall) -> None:
        """Store one call, whether it worked or not."""


@dataclass
class Turn:
    """A stretch of speech from Muse Voice, in seconds from the start of the video."""

    start_s: float
    end_s: float
    text: str
    speaker: str = ""


@dataclass
class Box:
    """One SAM 3.1 box on one frame of the video that was sent, in that video's pixels."""

    frame: int
    object_id: int
    left: float
    top: float
    right: float
    bottom: float
    confidence: float | None = None


@dataclass
class Segmentation:
    noun: str
    width: int  # of the video that was sent
    height: int
    frames: int  # frames in that video
    fps: float
    boxes: list[Box] = field(default_factory=list)  # sorted by frame, then object_id


class MetaAPI(Protocol):
    """What the app needs from Meta.

    gameday.meta_api.MetaClient is the real one; tests pass fakes. Every method
    checks the budget first, records an ApiCall on the ledger (ok or not), and
    raises MetaError (or BudgetExceeded) when it fails.
    """

    def transcribe(self, audio: Path, *, job_id: int | None = None) -> list[Turn]:
        """Muse Voice on a WAV file of any length (chunked inside)."""

    def chat_json(self, messages: list[dict], schema: dict, *, name: str, purpose: str,
                  job_id: int | None = None, max_tokens: int = 2048, temperature: float = 0.2) -> dict:
        """Muse Spark held to a strict JSON schema; returns the parsed object.

        `messages` are chat messages. A content list may hold {"type": "text", "text": ...}
        and {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}} parts.
        """

    def chat_text(self, messages: list[dict], *, purpose: str, job_id: int | None = None,
                  max_tokens: int = 600, temperature: float = 0.4) -> str:
        """Muse Spark, plain text."""

    def segment_video(self, video: Path, noun: str, *, job_id: int | None = None) -> Segmentation:
        """SAM 3.1 on a short H.264 MP4 the caller made: every box it finds for `noun`."""

    def test_key(self) -> None:
        """The cheapest call that proves the key works; raises MetaError if it doesn't."""


MetaFactory = Callable[[], "MetaAPI | None"]  # None: no Meta key saved

# --- The engine -------------------------------------------------------------

STAGES = ("voice", "spark", "sam", "render", "captions")  # the dashboard's stepper, in order
STAGE_LABELS = {"voice": "Muse Voice", "spark": "Muse Spark", "sam": "SAM 3.1", "render": "9:16",
                "captions": "Captions"}

Mode = Literal["basketball", "talk"]
CATEGORIES = ("clutch", "dunks", "threes", "blocks", "steals", "assists", "fan_reactions", "big_moment",
              "other", "highlight")


@dataclass
class PipelineOptions:
    mode: Mode = "basketball"
    max_clips: int = 8
    min_clip_s: float = 12.0
    max_clip_s: float = 40.0
    hint: str = ""  # the creator's "Teams or players", passed to Muse Spark
    ball_noun: str = "basketball"
    sam_fps: float = 10.0
    title_cards: bool = True


ProgressFn = Callable[[str, float, str], None]  # (stage from STAGES, 0..1 within it, short message)
StopFn = Callable[[], bool]  # True: stop at the next safe point


class JobCancelled(Exception):
    """should_stop() said so."""


class PipelineError(Exception):
    """The video can't be processed: unreadable, no video stream, or too short."""


@dataclass
class Follow:
    """How a clip's 9:16 window was steered."""

    source: Literal["ball", "blend", "players", "centre", "speaker"]
    ball_coverage: float = 0.0  # share of the clip's frames with a usable ball box, 0..1
    frames: int = 0  # frames SAM 3.1 looked at
    note: str = ""  # for people, e.g. "Followed the ball 74% of the time"


@dataclass
class ClipResult:
    index: int  # 1 = best
    path: Path  # 1080x1920 H.264 + AAC MP4 under work_dir/clips/
    thumb: Path  # JPEG beside it
    start_s: float  # in the source video
    end_s: float
    title: str
    category: str  # one of CATEGORIES
    label: str  # short, e.g. "Corner three to tie it"
    score: float  # 0..100, higher is better
    debug: Path | None = None  # the wide frame with SAM's box and the crop window
    team: str = ""
    player: str = ""
    evidence: str = ""  # the commentary that made Muse Spark pick it
    transcript: str = ""
    follow: Follow | None = None


@dataclass
class PipelineResult:
    clips: list[ClipResult]
    duration_s: float
    transcript: list[Turn] = field(default_factory=list)
    stats: dict = field(default_factory=dict)  # seconds per stage, cost per model, fallbacks used
    warnings: list[str] = field(default_factory=list)  # for people: "Muse Voice failed, so ..."


class Pipeline(Protocol):
    """gameday.engine.pipeline.run.

    `source` is only read. Clips, thumbnails and debug images go to
    work_dir/clips/; scratch files go to work_dir/tmp/, removed at the end
    unless GAMEDAY_KEEP_TMP=1. With `meta` None (no key), or when a Meta call
    fails, the run still finishes on fallbacks (loudness picks, a centre crop)
    and says so in `warnings`. Only PipelineError and JobCancelled escape.
    """

    def __call__(self, job_id: int, source: Path, work_dir: Path, options: PipelineOptions,
                 meta: MetaAPI | None, progress: ProgressFn, should_stop: StopFn) -> PipelineResult: ...
