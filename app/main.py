"""FastAPI application: lifespan wiring + web UI routes."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .config import load_bootstrap
from .db import Database
from .logging_setup import setup_logging
from .poller import BomPoller
from .transmit import TransmitManager
from .watchdog import Liveness
from .web.routes import router

logger = logging.getLogger("wx_echo.main")


async def _heartbeat(liveness: Liveness) -> None:
    """Refresh the liveness heartbeat while the event loop is healthy."""
    while True:
        liveness.beat()
        await asyncio.sleep(5)


async def _startup_radio(tx: TransmitManager) -> None:
    await tx.reconfigure()


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = load_bootstrap()
    setup_logging()
    logger.info("starting wx-echo (db=%s)", cfg.db_path)

    db = Database(cfg.db_path)
    tx = TransmitManager(db)
    poller = BomPoller(db, tx)

    app.state.cfg = cfg
    app.state.db = db
    app.state.tx = tx
    app.state.poller = poller

    # Liveness watchdog: force a restart if the event loop ever wedges.
    liveness = Liveness(stall_seconds=90.0)
    liveness.start()
    beat_task = asyncio.create_task(_heartbeat(liveness))
    app.state.liveness = liveness

    tx.start()
    startup_task = asyncio.create_task(_startup_radio(tx))
    app.state.startup_task = startup_task
    poller.start()

    try:
        yield
    finally:
        logger.info("shutting down wx-echo")
        liveness.stop()          # first: never force-exit during a clean shutdown
        beat_task.cancel()
        if not startup_task.done():
            startup_task.cancel()
        await poller.stop()
        await tx.stop()
        db.close()


def create_app() -> FastAPI:
    app = FastAPI(title="wx-echo", lifespan=lifespan)
    app.include_router(router)
    return app


app = create_app()


def main() -> None:
    import sys
    import uvicorn

    # On Windows, asyncio defaults to the ProactorEventLoop, but the MeshCore
    # serial layer (serial_asyncio) only works on the SelectorEventLoop. Without
    # this, MeshCore serial never connects on Windows ("no response").
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    cfg = load_bootstrap()
    uvicorn.run(app, host=cfg.http_host, port=cfg.http_port, log_config=None)


if __name__ == "__main__":
    main()
