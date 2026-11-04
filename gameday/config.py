"""The app's settings from the environment (docs/API.md, Environment).

Read once at start into a Config. Nothing here is a secret store: the Meta key
and the relay's device secret live encrypted in the database (secrets.py), and
the owner password is either GAMEDAY_PASSWORD or the generated file below.
The judge login's password is GAMEDAY_JUDGE_PASSWORD; without it there is no
judge login.
"""

from __future__ import annotations

import hmac
import logging
import os
import secrets as _random
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("gameday")

OWNER_USER = "owner"
JUDGE_USER = "judge"
PASSWORD_FILE = "owner-password.txt"
SECRET_KEY_FILE = "secret.key"
DB_FILE = "gameday.db"


def _number(env: Mapping[str, str], name: str, default: float, *, low: float, high: float) -> float:
    raw = str(env.get(name, "") or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number, not {raw!r}.") from None
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low:g} and {high:g}.")
    return value


class StartRefused(ValueError):
    """A setting the app won't start with. The message is a sentence for
    people, and never holds a password."""


@dataclass
class Config:
    data_dir: Path = field(default_factory=lambda: Path("data"))
    host: str = "127.0.0.1"
    port: int = 8080
    password: str = ""  # GAMEDAY_PASSWORD; "" means the generated one in DATA_DIR
    judge_password: str = ""  # GAMEDAY_JUDGE_PASSWORD; "" means there is no judge login
    secret_key: str = ""  # GAMEDAY_SECRET_KEY; "" means DATA_DIR/secret.key
    meta_api_key: str = ""  # META_API_KEY; wins over the saved key when set
    budget_usd: float = 25.0
    watch_dir: str = ""
    max_upload_mb: int = 4096
    relay_url: str = ""
    relay_enrolment_key: str = ""
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    keep_tmp: bool = False
    log_level: str = "INFO"
    font: str = ""  # GAMEDAY_FONT, passed through untouched: the engine reads it

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        env = os.environ if env is None else env

        def text(name: str, default: str = "") -> str:
            return str(env.get(name, default) or default).strip()

        port = int(_number(env, "PORT", 8080, low=1, high=65535))
        config = cls(
            data_dir=Path(text("DATA_DIR", "data")).expanduser().resolve(),
            host=text("HOST", "127.0.0.1"),
            port=port,
            password=str(env.get("GAMEDAY_PASSWORD", "") or ""),
            judge_password=str(env.get("GAMEDAY_JUDGE_PASSWORD", "") or ""),
            secret_key=text("GAMEDAY_SECRET_KEY"),
            meta_api_key=text("META_API_KEY"),
            budget_usd=_number(env, "META_BUDGET_USD", 25.0, low=0, high=100_000),
            watch_dir=text("WATCH_DIR"),
            max_upload_mb=int(_number(env, "MAX_UPLOAD_MB", 4096, low=1, high=1_000_000)),
            relay_url=text("RELAY_URL").rstrip("/"),
            relay_enrolment_key=text("RELAY_ENROLMENT_KEY"),
            ffmpeg=text("FFMPEG", "ffmpeg"),
            ffprobe=text("FFPROBE", "ffprobe"),
            keep_tmp=text("GAMEDAY_KEEP_TMP", "0") == "1",
            log_level=(text("LOG_LEVEL", "INFO") or "INFO").upper(),
            font=str(env.get("GAMEDAY_FONT", "") or ""),
        )
        problem = judge_password_problem(config)
        if problem:
            raise StartRefused(problem)
        return config

    # ---- places under DATA_DIR ------------------------------------------------

    @property
    def db_path(self) -> Path:
        return self.data_dir / DB_FILE

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    def job_dir(self, job_id: int) -> Path:
        return self.media_dir / "jobs" / str(int(job_id))

    @property
    def max_upload_bytes(self) -> int:
        return int(self.max_upload_mb) * 1024 * 1024

    def ensure_dirs(self) -> None:
        for folder in (self.data_dir, self.uploads_dir, self.media_dir / "jobs"):
            folder.mkdir(parents=True, exist_ok=True)


def write_private(path: Path, text: str) -> bool:
    """Create `path` holding `text`, readable by this user only (0600 where
    the system has modes). False when it was already there: never overwritten."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # a file system without modes: the file is still this user's
    return True


MIN_PASSWORD = 12
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def weak_password(config: Config) -> str:
    """Why GAMEDAY_PASSWORD is too short to trust, or "" when it is fine (or unset:
    the generated one is 24 characters). Never says what the password is."""
    if config.password and len(config.password) < MIN_PASSWORD:
        return (f"GAMEDAY_PASSWORD is shorter than {MIN_PASSWORD} characters. Set a longer one, or unset it "
                f"to have one generated.")
    return ""


def judge_password_problem(config: Config, owner: str = "") -> str:
    """Why GAMEDAY_JUDGE_PASSWORD can't be used, or "" when it is fine, or unset
    (then there is no judge login). `owner` is the owner password, when known:
    the judge's must differ, or a judge could sign in as the owner. Unlike the
    owner's, a short one is refused wherever the app listens. Never says what
    either password is."""
    judge = config.judge_password
    if not judge:
        return ""
    if len(judge) < MIN_PASSWORD:
        return (f"GAMEDAY_JUDGE_PASSWORD is shorter than {MIN_PASSWORD} characters. Set a longer one, or unset "
                f"it to have no judge login.")
    if owner and hmac.compare_digest(judge.encode(), owner.encode()):
        return "GAMEDAY_JUDGE_PASSWORD is the owner password. Give the judge login a password of its own."
    return ""


def listens_beyond_this_computer(config: Config) -> bool:
    return config.host.strip().lower() not in LOOPBACK_HOSTS


def owner_password(config: Config) -> str:
    """GAMEDAY_PASSWORD, or the one generated on first start and kept in
    DATA_DIR/owner-password.txt. The log names the file, never the password."""
    if config.password:
        return config.password
    path = config.data_dir / PASSWORD_FILE
    if not path.exists():
        config.data_dir.mkdir(parents=True, exist_ok=True)
        if write_private(path, _random.token_urlsafe(18) + "\n"):
            log.info("Generated the owner password. It is in %s (user: %s).", path.resolve(), OWNER_USER)
    password = path.read_text(encoding="utf-8").strip()
    if not password:
        raise RuntimeError(f"{path} is empty. Delete it to have a new password made, or set GAMEDAY_PASSWORD.")
    return password


def password_source(config: Config) -> str:
    """Where the owner password comes from, for the log: never the password."""
    if config.password:
        return "the GAMEDAY_PASSWORD environment variable"
    return str((config.data_dir / PASSWORD_FILE).resolve())
