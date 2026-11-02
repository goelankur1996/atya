"""Single-process entry point — runs all three workers as daemon threads.

A crash in one worker's loop is contained (each `run()` is wrapped here in a
restart loop). The dead-drop invariant is preserved: workers still coordinate
only through Notion; they just happen to share a process.

Stop with SIGTERM/SIGINT; daemon threads exit with the process.
"""

from __future__ import annotations

import logging
import signal
import threading
import time

from . import config, processor, receiver, sender
from .notion import Notion
from .schema import RulesLayer

log = logging.getLogger("main")


def _supervise(name: str, target, *args) -> None:
    """Restart the worker if its loop body ever returns or raises."""
    while True:
        try:
            target(*args)
            log.warning("[%s] run() returned unexpectedly — restarting in 5s", name)
        except Exception:
            log.exception("[%s] crashed — restarting in 5s", name)
        time.sleep(5)


def main() -> None:
    config.setup_logging()
    cfg = config.load()
    notion = Notion(cfg.notion_token)
    rules = RulesLayer(
        notion=notion,
        schema_page_id=cfg.schema_page_id,
        reload_seconds=cfg.schema_reload_minutes * 60,
    )

    workers = [
        ("receiver", receiver.run, (cfg, notion)),
        ("processor", processor.run, (cfg, notion, rules)),
        ("sender", sender.run, (cfg, notion)),
    ]
    for name, target, args in workers:
        t = threading.Thread(
            target=_supervise, args=(name, target, *args),
            daemon=True, name=name,
        )
        t.start()
        log.info("[main] started %s", name)

    stop = threading.Event()

    def _shutdown(signum, frame):  # noqa: ARG001
        log.info("[main] signal %s received — exiting", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    stop.wait()


if __name__ == "__main__":
    main()
