"""python -m gameday: the app in one process, its background threads inside.

Reads .env when there is one, then the environment (docs/API.md). The log
says where the owner password is kept, never what it is.
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
    from gameday.config import OWNER_USER, Config, password_source

    try:
        config = Config.from_env()
    except ValueError as e:
        print(f"Clips Kitty Game Day can't start: {e}", file=sys.stderr)
        return 2
    logging.basicConfig(level=getattr(logging, config.log_level, logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("gameday")
    app = create_app(config)
    log.info("Clips Kitty Game Day on http://%s:%s. Sign in as %s; the password is in %s.",
             config.host, config.port, OWNER_USER, password_source(config))
    level = {"warn": "warning", "fatal": "critical"}.get(config.log_level.lower(), config.log_level.lower())
    if level not in ("critical", "error", "warning", "info", "debug", "trace"):
        level = "info"
    uvicorn.run(app, host=config.host, port=config.port, log_level=level, proxy_headers=False,
                server_header=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
