"""Fixtures for the platform tests: a real store in a temp DATA_DIR, the app
with fake engine, Meta and ffprobe, and the real relay under Node."""

from __future__ import annotations

import base64
import os
import shutil
import socket
import subprocess
import time
import warnings

import pytest

warnings.filterwarnings("ignore", message="Using `httpx` with `starlette.testclient` is deprecated")

from fastapi.testclient import TestClient  # noqa: E402
from platform_fakes import RELAY_DIR, FakePipeline, node_ok  # noqa: E402

from gameday import media  # noqa: E402
from gameday import secrets as secret_store  # noqa: E402
from gameday.config import Config  # noqa: E402
from gameday.store import Store  # noqa: E402

PASSWORD = "owner-password-for-tests"
AUTH = {"Authorization": "Basic " + base64.b64encode(f"owner:{PASSWORD}".encode()).decode()}
ACTION = {"X-Gameday-Action": "1"}
ENROLL_KEY = "e2e-enrolment-key-0123456789abcdef"
#: The start of what the fake ffprobe takes for a video.
VIDEO_BYTES = b"FAKEVIDEO"


def pytest_configure(config):
    config.addinivalue_line("filterwarnings", "ignore:Using `httpx` with `starlette.testclient`")


def fake_probe(path):
    """ffprobe, stood in for: a file that starts with VIDEO_BYTES is a
    half-hour landscape video, anything else is not a video."""
    with open(path, "rb") as f:
        head = f.read(len(VIDEO_BYTES))
    if head != VIDEO_BYTES:
        raise media.MediaError(f"{os.path.basename(path)} has no video stream")
    return media.MediaInfo(duration_s=1800.0, width=1920, height=1080, fps=30.0, video_codec="h264",
                           has_audio=True, audio_rate=48000)


def make_config(tmp_path, **env) -> Config:
    base = {"DATA_DIR": str(tmp_path / "data"), "GAMEDAY_PASSWORD": PASSWORD, "MAX_UPLOAD_MB": "5"}
    return Config.from_env({**base, **env})


@pytest.fixture
def config(tmp_path):
    return make_config(tmp_path)


@pytest.fixture
def db(config):
    """A real, empty store in a temp DATA_DIR, with its secrets."""
    config.ensure_dirs()
    store = Store(config.db_path, data_dir=config.data_dir, budget_usd=config.budget_usd)
    secret_store.attach(store, config)
    yield store
    store.close_all()


@pytest.fixture
def data_dir(config):
    return config.data_dir


class App:
    """The app under test: its client (signed in), store, worker and fakes."""

    def __init__(self, app, pipeline):
        self.app = app
        self.client = TestClient(app, base_url="http://127.0.0.1", headers=AUTH)
        self.store = app.state.store
        self.worker = app.state.worker
        self.config = app.state.config
        self.pipeline = pipeline

    def post(self, path, **kw):
        return self.client.post(path, headers={**ACTION, **kw.pop("headers", {})}, **kw)

    def patch(self, path, **kw):
        return self.client.patch(path, headers={**ACTION, **kw.pop("headers", {})}, **kw)

    def put(self, path, **kw):
        return self.client.put(path, headers={**ACTION, **kw.pop("headers", {})}, **kw)

    def delete(self, path, **kw):
        return self.client.delete(path, headers={**ACTION, **kw.pop("headers", {})}, **kw)

    def get(self, path, **kw):
        return self.client.get(path, **kw)


@pytest.fixture
def make_app(tmp_path):
    """make_app(pipeline=..., meta_factory=..., **env): the app on its own
    DATA_DIR, threads not started (tests run the worker's steps by hand)."""
    made = []

    def make(*, pipeline=None, meta_factory=None, probe=fake_probe, feed=None, config=None, **env):
        from gameday.app import create_app

        pipeline = pipeline or FakePipeline()
        cfg = config or make_config(tmp_path, **env)
        app = create_app(cfg, pipeline=pipeline, meta_factory=meta_factory or (lambda: None), run_threads=False,
                         probe=probe, feed=feed)
        made.append(app)
        return App(app, pipeline)

    yield make
    for app in made:
        app.state.store.close_all()


@pytest.fixture
def gd(make_app):
    """The app with the fake engine and no Meta key."""
    return make_app()


# ---- the real relay under Node ------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def relay_url(tmp_path_factory):
    """meta-relay/worker.js run by meta-relay/test/serve.mjs, with Meta faked."""
    if not node_ok():
        pytest.skip("needs Node 22.5+ for the relay")
    import requests

    port = _free_port()
    env = {**os.environ, "PORT": str(port), "ENROLL_KEY": ENROLL_KEY}
    log = (tmp_path_factory.mktemp("relay") / "relay.log").open("w")
    # --experimental-sqlite: node:sqlite needs it before Node 22.13; later versions accept it.
    proc = subprocess.Popen([shutil.which("node"), "--no-warnings", "--experimental-sqlite", "test/serve.mjs"],
                            cwd=RELAY_DIR, env=env, stdout=log, stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(150):
            try:
                if requests.get(url + "/health", timeout=1).ok:
                    break
            except requests.ConnectionError:
                time.sleep(0.1)
        else:
            raise RuntimeError("the relay did not start")
        yield url
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        log.close()
