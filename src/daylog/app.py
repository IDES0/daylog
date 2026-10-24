"""The entrypoint: the web server (status site, web app, calendar feed, health
inlet) and the scheduler (brief, reconcile, weekly review), in one process.

    uv run python -m daylog.app
"""

from __future__ import annotations

import logging
import signal
import threading
from types import FrameType

from dotenv import load_dotenv

from daylog import calendar_server, scheduler
from daylog.webapp.api import Api

logger = logging.getLogger(__name__)


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)

    # One API for the web server and the scheduler: a scheduled plan's card
    # lands in the same card store the app reads.
    api = Api()
    server = calendar_server.start(api)
    clock = scheduler.build(api)
    clock.start()
    for job in clock.get_jobs():
        logger.info("scheduled %s: next %s", job.id, job.next_run_time)

    stop = threading.Event()

    def _stop(signum: int, frame: FrameType | None) -> None:
        logger.info("stopping (signal %d)", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    stop.wait()
    clock.shutdown(wait=False)
    if server is not None:
        server.shutdown()


if __name__ == "__main__":
    main()
