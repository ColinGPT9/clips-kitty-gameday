"""The dashboard's files and the clips' media.

/ and /static/* serve gameday/static/ when it is there. /media serves only a
job's clips, thumbnails and debug images: .mp4, .jpg and .png files directly
in DATA_DIR/media/jobs/<id>/clips/, never its tmp/ folder or anything else.
Byte ranges come from Starlette's FileResponse, so a phone or a browser can
seek in a clip.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse, HTMLResponse

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

MEDIA_TYPES = {".mp4": "video/mp4", ".jpg": "image/jpeg", ".png": "image/png"}
# Set here rather than read from the system: Windows' registry can call .js text/plain,
# which a browser refuses to run under X-Content-Type-Options: nosniff.
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".ico": "image/x-icon",
    ".webp": "image/webp", ".woff2": "font/woff2", ".json": "application/json",
    ".webmanifest": "application/manifest+json", ".txt": "text/plain; charset=utf-8", ".map": "application/json",
}
_CLIP_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}")
_STATIC_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}")

NO_DASHBOARD = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Clips Kitty Game Day</title></head>
<body><h1>Clips Kitty Game Day</h1><p>The app is running, but its dashboard files are not installed.</p>
</body></html>
"""


def _not_found() -> HTTPException:
    return HTTPException(404, "Not found.")


def clip_file(data_dir: Path, job_id: str, name: str) -> Path:
    """The file a /media URL names, or HTTPException 404. Only a plain file
    name with an allowed suffix, directly in that job's clips folder."""
    if not job_id.isdigit() or len(job_id) > 12:
        raise _not_found()
    if not _CLIP_NAME.fullmatch(name) or ".." in name:
        raise _not_found()
    suffix = Path(name).suffix
    if suffix not in MEDIA_TYPES:  # lower case only, as the engine writes them
        raise _not_found()
    folder = Path(data_dir) / "media" / "jobs" / str(int(job_id)) / "clips"
    try:
        path = (folder / name).resolve(strict=True)
        allowed = folder.resolve(strict=True)
    except (OSError, RuntimeError):
        raise _not_found() from None
    if path.parent != allowed or not path.is_file():
        raise _not_found()
    return path


def static_file(rel: str) -> Path:
    parts = rel.split("/")
    if not rel or any(not _STATIC_PART.fullmatch(p) or ".." in p for p in parts):
        raise _not_found()
    if Path(rel).suffix.lower() not in STATIC_TYPES:
        raise _not_found()
    try:
        root = STATIC_DIR.resolve(strict=True)
        path = (STATIC_DIR / rel).resolve(strict=True)
    except (OSError, RuntimeError):
        raise _not_found() from None
    if root not in path.parents or not path.is_file():
        raise _not_found()
    return path


def install(app, *, config) -> None:
    @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
    def index():
        page = STATIC_DIR / "index.html"
        if page.is_file():
            return FileResponse(page, media_type=STATIC_TYPES[".html"], headers={"Cache-Control": "no-cache"})
        return HTMLResponse(NO_DASHBOARD, headers={"Cache-Control": "no-cache"})

    @app.api_route("/static/{rel:path}", methods=["GET", "HEAD"], include_in_schema=False)
    def static(rel: str):
        path = static_file(rel)
        return FileResponse(path, media_type=STATIC_TYPES[path.suffix.lower()], headers={"Cache-Control": "no-cache"})

    @app.api_route("/media/jobs/{job_id}/clips/{name}", methods=["GET", "HEAD"], include_in_schema=False)
    def media(job_id: str, name: str):
        path = clip_file(config.data_dir, job_id, name)
        return FileResponse(path, media_type=MEDIA_TYPES[path.suffix],
                            headers={"Cache-Control": "private, max-age=3600"})
