"""The /api routes and /health (docs/API.md, "The app")."""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

from fastapi import HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from gameday import __version__, media
from gameday import secrets as secret_store
from gameday.store import fresh_stages, loads, media_url
from gameday.types import APP_NAME, MODEL_SAM, MODEL_SPARK, MODEL_VOICE, MODELS, STAGES
from gameday.watch.watcher import check_folder

_UNSAFE = re.compile(r"[^A-Za-z0-9._ -]+")


# ---- views ---------------------------------------------------------------------


def stage_of_call(model: str, purpose: str) -> str | None:
    """Which stage of a job a Meta call belongs to, for its cost."""
    if purpose == "captions":
        return "captions"
    return {MODEL_VOICE: "voice", MODEL_SPARK: "spark", MODEL_SAM: "sam"}.get(model)


def clip_view(store, clip) -> dict:
    follow = loads(clip["follow"], {})
    return {
        "id": clip["id"],
        "job_id": clip["job_id"],
        "index": clip["idx"],
        "title": clip["title"],
        "category": clip["category"],
        "label": clip["label"],
        "score": float(clip["score"]),
        "team": clip["team"],
        "player": clip["player"],
        "evidence": clip["evidence"],
        "start_s": float(clip["start_s"]),
        "end_s": float(clip["end_s"]),
        "follow": {"source": follow.get("source", ""), "ball_coverage": float(follow.get("ball_coverage") or 0),
                   "note": follow.get("note", "")} if follow else None,
        "video_url": media_url(store.data_dir, clip["path"]),
        "thumb_url": media_url(store.data_dir, clip["thumb"]),
        "debug_url": media_url(store.data_dir, clip["debug"]),
    }


def job_view(store, job, *, with_clips: bool = False) -> dict:
    video = store.get_video(job["video_id"])
    saved = loads(job["stages"], {})
    costs: dict[str, float] = {}
    for call in store.job_calls(job["id"]):
        stage = stage_of_call(call["model"], call["purpose"])
        if stage:
            costs[stage] = costs.get(stage, 0.0) + float(call["cost_usd"])
    stages = {}
    for name, blank in fresh_stages().items():
        got = saved.get(name) if isinstance(saved.get(name), dict) else {}
        stages[name] = {
            "state": got.get("state", blank["state"]),
            "seconds": round(float(got.get("seconds") or 0), 1),
            "cost_usd": round(costs.get(name, 0.0), 4),
            "note": str(got.get("note") or ""),
        }
    out = {
        "id": job["id"],
        "video": {"id": job["video_id"], "name": video["name"] if video else "",
                  "duration_s": float(video["duration_s"]) if video else 0.0,
                  "source": video["source"] if video else "upload"},
        "state": job["state"],
        "stage": job["stage"] if job["stage"] in STAGES else None,
        "stage_progress": round(float(job["stage_progress"] or 0), 3),
        "message": job["message"],
        "stages": stages,
        "cost_usd": round(store.job_cost(job["id"]), 4),
        "clips": int(job["clips"]),
        "warnings": loads(job["warnings"], []),
        "error": job["error"],
        "created_at": job["created_at"],
        "started_at": job["started_at"],
        "finished_at": job["finished_at"],
    }
    if with_clips:
        out["clips_list"] = [clip_view(store, c) for c in store.clips_for_job(job["id"])]
    return out


def watch_view(store, watch) -> dict:
    return {"id": watch["id"], "path": watch["path"], "enabled": bool(watch["enabled"]),
            "last_checked_at": watch["last_checked_at"], "videos": store.videos_of_watch(watch["id"]),
            "error": watch["error"]}


def safe_name(filename: str) -> str:
    """An upload's name to keep: its last part, plain characters only."""
    base = re.split(r"[\\/]", str(filename or ""))[-1].strip()
    base = _UNSAFE.sub("_", base).strip(" .")[:120]
    return base or "video.mp4"


# ---- routes -------------------------------------------------------------------


def install(app, *, store, config, worker, meta_factory, probe=None) -> None:
    probe = probe or media.probe

    def job_or_404(job_id: int):
        job = store.get_job(job_id)
        if job is None:
            raise HTTPException(404, "There is no such job.")
        return job

    @app.api_route("/health", methods=["GET", "HEAD"])
    def health():
        return {"ok": True, "app": APP_NAME, "version": __version__}

    @app.get("/api/status")
    def status():
        key, from_env = secret_store.meta_key(config, store.secrets)
        running = store.running_job()
        return {
            "version": __version__,
            "meta_key": {"saved": bool(key), "from_env": from_env},
            "worker": {"busy": running is not None, "job_id": running["id"] if running is not None else None,
                       "queued": store.queued_count()},
            "budget": {"limit_usd": store.budget_usd, "spent_usd": round(store.spent_usd(), 4)},
            "watches": len(store.watches()),
            "upload_limit_mb": config.max_upload_mb,
        }

    # ---- settings -----------------------------------------------------------

    @app.get("/api/settings")
    def get_settings():
        return store.job_settings()

    @app.patch("/api/settings")
    def patch_settings(body: dict):
        try:
            return store.save_job_settings(body)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None

    # ---- the Meta key -------------------------------------------------------

    @app.put("/api/meta/key")
    def put_meta_key(body: dict):
        key = body.get("key")
        if not isinstance(key, str) or not 8 <= len(key.strip()) <= 500 or re.search(r"\s", key.strip()):
            raise HTTPException(400, "That doesn't look like a Meta API key.")
        store.secrets.set(secret_store.META_KEY, key.strip())
        store.add_event("info", "meta", "The Meta key was saved.")
        return {"saved": True}

    @app.delete("/api/meta/key")
    def delete_meta_key():
        store.secrets.delete(secret_store.META_KEY)
        store.add_event("info", "meta", "The saved Meta key was removed.")
        return {"saved": False}

    @app.post("/api/meta/test")
    def test_meta_key():
        from gameday.types import MetaError

        try:
            meta = meta_factory()
        except Exception as e:
            return {"ok": False, "error": f"The Meta client couldn't start ({type(e).__name__})."}
        if meta is None:
            return {"ok": False, "error": "No Meta key is saved."}
        try:
            meta.test_key()
        except MetaError as e:
            return {"ok": False, "error": str(e) or "The key didn't work."}
        return {"ok": True}

    # ---- upload -------------------------------------------------------------

    @app.post("/api/upload")
    async def upload(request: Request, filename: str = Query("")):
        limit = config.max_upload_bytes
        too_big = f"The file is bigger than the {config.max_upload_mb} MB the app takes."
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > limit:
            raise HTTPException(413, too_big)
        name = safe_name(filename)
        config.uploads_dir.mkdir(parents=True, exist_ok=True)
        target = config.uploads_dir / f"{uuid.uuid4().hex[:12]}_{name}"
        size = 0
        kept = False
        try:  # the file goes, whatever stops this (a cancelled task too), unless a video row holds it
            try:
                with open(target, "xb") as out:
                    async for chunk in request.stream():
                        size += len(chunk)
                        if size > limit:
                            raise HTTPException(413, too_big)
                        out.write(chunk)
            except (HTTPException, ClientDisconnect, OSError) as e:
                if isinstance(e, HTTPException):
                    raise
                if isinstance(e, ClientDisconnect):
                    raise HTTPException(400, "The upload stopped before the whole file arrived.") from None
                raise HTTPException(500, "The app couldn't save the upload.") from None
            if not size:
                raise HTTPException(400, "The upload was empty.")
            try:
                info = await run_in_threadpool(probe, target)
            except media.MediaError:
                raise HTTPException(400, "That file isn't a video the app can read.") from None
            stat = target.stat()

            def record():
                video_id = store.add_video(name=name, path=str(target.resolve()), source="upload",
                                           size=stat.st_size, mtime_ns=stat.st_mtime_ns, duration_s=info.duration_s)
                job_id = store.add_job(video_id, store.job_settings())
                store.add_event("info", "upload", f"Uploaded {name}: job {job_id} is queued.", job_id=job_id)
                return video_id, job_id

            video_id, job_id = await run_in_threadpool(record)
            kept = True
        finally:
            if not kept:
                _remove(target)
        worker.wake_jobs()
        return {"video_id": video_id, "job_id": job_id}

    # ---- watched folders ----------------------------------------------------

    @app.get("/api/watches")
    def list_watches():
        return [watch_view(store, w) for w in store.watches()]

    @app.post("/api/watches")
    def add_watch(body: dict):
        try:
            folder = check_folder(body.get("path") if isinstance(body.get("path"), str) else "", config.data_dir)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        if store.watch_by_path(str(folder)) is not None:
            raise HTTPException(400, "That folder is watched already.")
        watch_id = store.add_watch(str(folder))
        store.add_event("info", "watch", f"Watching {folder}.")
        return watch_view(store, store.get_watch(watch_id))

    @app.patch("/api/watches/{watch_id}")
    def patch_watch(watch_id: int, body: dict):
        watch = store.get_watch(watch_id)
        if watch is None:
            raise HTTPException(404, "There is no such watched folder.")
        if set(body) - {"enabled"} or not isinstance(body.get("enabled"), bool):
            raise HTTPException(400, 'Send {"enabled": true} or {"enabled": false}.')
        store.set_watch(watch_id, enabled=1 if body["enabled"] else 0)
        return watch_view(store, store.get_watch(watch_id))

    @app.delete("/api/watches/{watch_id}")
    def delete_watch(watch_id: int):
        watch = store.get_watch(watch_id)
        if watch is None:
            raise HTTPException(404, "There is no such watched folder.")
        store.delete_watch(watch_id)
        store.add_event("info", "watch", f"Stopped watching {watch['path']}.")
        return {"deleted": True}

    # ---- jobs and clips -----------------------------------------------------

    @app.get("/api/jobs")
    def list_jobs(limit: int = Query(20, ge=1, le=200)):
        return [job_view(store, j) for j in store.jobs(limit)]

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: int):
        return job_view(store, job_or_404(job_id), with_clips=True)

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: int):
        job = job_or_404(job_id)
        if job["state"] not in ("queued", "running"):
            raise HTTPException(409, "This job has finished already, so there is nothing to cancel.")
        if store.cancel_job(job_id) is None:
            raise HTTPException(409, "This job has finished already, so there is nothing to cancel.")
        return job_view(store, store.get_job(job_id))

    @app.post("/api/jobs/{job_id}/retry")
    def retry_job(job_id: int):
        job = job_or_404(job_id)
        if job["state"] not in ("failed", "cancelled"):
            raise HTTPException(409, "Only a failed or cancelled job can be retried.")
        video = store.get_video(job["video_id"])
        if video is None or not Path(video["path"]).is_file():
            raise HTTPException(409, "The video file is no longer there, so the job can't run again.")
        if video["source"] == "watch":
            st = Path(video["path"]).stat()
            if (st.st_size, st.st_mtime_ns) != (video["size"], video["mtime_ns"]):
                raise HTTPException(409, "The file changed since it was found. The watcher takes it as a new video.")
        new_id = store.add_job(video["id"], store.job_settings(), retry_of=job_id)
        store.add_event("info", "job", f"Job {job_id} is queued again as job {new_id}.", job_id=new_id)
        worker.wake_jobs()
        return job_view(store, store.get_job(new_id))

    @app.get("/api/clips")
    def list_clips(job_id: int | None = Query(None), limit: int = Query(50, ge=1, le=500)):
        if job_id is not None:
            return [clip_view(store, c) for c in store.clips_for_job(job_id)[:limit]]
        return [clip_view(store, c) for c in store.recent_clips(limit)]

    # ---- usage and activity -------------------------------------------------

    @app.get("/api/usage")
    def usage():
        by_model = {m: {"calls": 0, "usd": 0.0} for m in MODELS}
        by_model[MODEL_SAM]["frames"] = 0
        total = 0.0
        for call in store.calls():
            entry = by_model.setdefault(call["model"], {"calls": 0, "usd": 0.0})
            entry["calls"] += 1
            entry["usd"] += float(call["cost_usd"])
            total += float(call["cost_usd"])
            if call["model"] == MODEL_SAM:
                frames = loads(call["units"], {}).get("frames")
                entry["frames"] += int(frames) if isinstance(frames, (int, float)) else 0
        for entry in by_model.values():
            entry["usd"] = round(entry["usd"], 4)
        recent = [{"at": c["at"], "model": c["model"], "purpose": c["purpose"], "ok": bool(c["ok"]),
                   "status": c["status"], "ms": c["ms"], "cost_usd": round(float(c["cost_usd"]), 4),
                   "job_id": c["job_id"], "error": c["error"]} for c in store.recent_calls(50)]
        return {"total_usd": round(total, 4), "budget_usd": store.budget_usd, "by_model": by_model, "recent": recent}

    @app.get("/api/events")
    def events(after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)):
        return [{"id": e["id"], "at": e["at"], "level": e["level"], "kind": e["kind"], "message": e["message"],
                 "job_id": e["job_id"]} for e in store.events(after, limit)]


def _remove(path: Path) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
