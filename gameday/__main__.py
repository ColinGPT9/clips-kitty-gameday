"""python -m gameday: the app in one process, its background threads inside.

Reads .env when there is one, then the environment (docs/API.md). The log
says where the owner password is kept, never what it is, and whether the
judge login is on.
"""

from __future__ import annotations

import logging
import sys


def main() -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    import uvicorn

    from gameday.app import create_app
    from gameday.config import (
        JUDGE_USER,
        OWNER_USER,
        Config,
        StartRefused,
        listens_beyond_this_computer,
        password_source,
        weak_password,
    )

    try:
        config = Config.from_env()  # StartRefused, a ValueError, for a short judge password
    except ValueError as e:
        print(f"Clips Kitty Game Day can't start: {e}", file=sys.stderr)
        return 2
    weak = weak_password(config)
    if weak and listens_beyond_this_computer(config):
        print(f"Clips Kitty Game Day can't start: {weak} HOST is {config.host}, so others can reach the app.",
              file=sys.stderr)
        return 2
    logging.basicConfig(level=getattr(logging, config.log_level, logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # urllib3's debug lines name each request's path, and a phone alert's path holds its secret topic.
    logging.getLogger("urllib3").setLevel(max(logging.getLogger().level, logging.INFO))
    log = logging.getLogger("gameday")
    if weak:
        log.warning("%s It is accepted only because HOST is %s.", weak, config.host)
    try:
        app = create_app(config)
    except StartRefused as e:  # a judge password that is the owner's
        print(f"Clips Kitty Game Day can't start: {e}", file=sys.stderr)
        return 2
    log.info("Clips Kitty Game Day on http://%s:%s. Sign in as %s; the password is in %s.",
             config.host, config.port, OWNER_USER, password_source(config))
    if config.judge_password:
        log.info("The judge login is on: user %s, with the password in GAMEDAY_JUDGE_PASSWORD. It can upload "
                 "and look, but not change settings or post.", JUDGE_USER)
    level = {"warn": "warning", "fatal": "critical"}.get(config.log_level.lower(), config.log_level.lower())
    if level not in ("critical", "error", "warning", "info", "debug", "trace"):
        level = "info"
    uvicorn.run(app, host=config.host, port=config.port, log_level=level, proxy_headers=False,
                server_header=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
