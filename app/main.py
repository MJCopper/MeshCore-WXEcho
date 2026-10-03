"""FastAPI application: lifespan wiring + web UI routes."""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .config import load_bootstrap
from .db import Database
from .logging_setup import setup_logging
from .diagnostic_logs import ProcessLogs
from .troubleshooting import Troubleshooting, utc
from .web.troubleshooting_routes import router as troubleshooting_router
from .poller import BomPoller
from .rfs.poller import RFSPoller
from .rfs.web import router as rfs_router
from .traffic.poller import TrafficPoller
from .traffic.web import router as traffic_router
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
    process_logs = ProcessLogs(Path(cfg.db_path).parent / "wxecho-process.jsonl" if cfg.db_path != ":memory:" else None)
    process_logs.install()
    logger.info("starting NoticeEcho (db=%s)", cfg.db_path)

    db = Database(cfg.db_path)
    tx = TransmitManager(db)
    poller = BomPoller(db, tx)
    rfs_poller = RFSPoller(db, tx)
    traffic_poller = TrafficPoller(db, tx)

    app.state.process_logs = process_logs
    app.state.started_epoch = time.time()
    app.state.started_at = utc()
    db.set_setting("diagnostic_previous_start", db.get_setting("diagnostic_last_start", ""))
    db.set_setting("diagnostic_last_start", app.state.started_at)
    db.set_setting("diagnostic_restart_count", int(db.get_setting("diagnostic_restart_count", 0)) + 1)
    app.state.cfg = cfg
    app.state.db = db
    app.state.tx = tx
    app.state.poller = poller
    app.state.rfs_poller = rfs_poller
    app.state.traffic_poller = traffic_poller

    app.state.troubleshooting = Troubleshooting(app)

    # Liveness watchdog: force a restart if the event loop ever wedges.
    liveness = Liveness(stall_seconds=90.0)
    liveness.start()
    beat_task = asyncio.create_task(_heartbeat(liveness))
    app.state.liveness = liveness

    tx.start()
    startup_task = asyncio.create_task(_startup_radio(tx))
    app.state.startup_task = startup_task
    poller.start()
    rfs_poller.start()
    traffic_poller.start()

    try:
        yield
    finally:
        logger.info("shutting down NoticeEcho")
        liveness.stop()          # first: never force-exit during a clean shutdown
        beat_task.cancel()
        if not startup_task.done():
            startup_task.cancel()
        await app.state.troubleshooting.close()
        await traffic_poller.stop()
        await rfs_poller.stop()
        await poller.stop()
        await tx.stop()
        db.close()
        process_logs.close()


def create_app() -> FastAPI:
    app = FastAPI(title="Meshcore NoticeEcho", lifespan=lifespan)
    app.include_router(router)
    app.include_router(rfs_router)
    app.include_router(traffic_router)
    app.include_router(troubleshooting_router)
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
