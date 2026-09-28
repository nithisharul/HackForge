"""FastAPI entry point.

    cd backend
    uvicorn app.main:app --reload

Then open http://localhost:8000 for the dashboard, /docs for the API.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import (routes_alerts, routes_benchmark, routes_demo, routes_health,
                     routes_metrics, ws)
from app.config import PROJECT_DIR, settings
from app.core.pipeline import Pipeline
from app.db.store import AlertStore
from app.publishers.base import Publisher
from app.publishers.dispatcher import Dispatcher
from app.publishers.local_file import LocalFilePublisher
from app.publishers.websocket import ConnectionManager, WebSocketPublisher

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
log = logging.getLogger("app")

FRONTEND = PROJECT_DIR / "frontend" / "index.html"


def build_publishers(ws_manager: ConnectionManager) -> tuple[list[Publisher], LocalFilePublisher]:
    fallback = LocalFilePublisher(settings.fallback_alert_file)
    pubs: list[Publisher] = [WebSocketPublisher(ws_manager)]
    if settings.aws_enabled:
        from app.publishers.cloudwatch import CloudWatchPublisher
        from app.publishers.sns import SNSPublisher
        pubs.append(CloudWatchPublisher(settings.cloudwatch_log_group, settings.cloudwatch_log_stream,
                                        settings.aws_region, settings.aws_endpoint_url))
        if settings.sns_topic_arn:
            pubs.append(SNSPublisher(settings.sns_topic_arn, settings.aws_region,
                                     settings.aws_endpoint_url, settings.sns_min_severity))
    else:
        log.info("AWS disabled - alerts also written to %s", settings.fallback_alert_file)
        pubs.append(fallback)
    return pubs, fallback


@asynccontextmanager
async def lifespan(app: FastAPI):
    ws_manager = ConnectionManager()
    store = AlertStore(settings.db_path)
    publishers, fallback = build_publishers(ws_manager)
    dispatcher = Dispatcher(publishers, fallback, retries=settings.publish_retries,
                            on_delivered=store.save_alert)
    pipeline = Pipeline(settings, store, dispatcher, ws_manager)
    app.state.ws_manager, app.state.store = ws_manager, store
    app.state.dispatcher, app.state.pipeline = dispatcher, pipeline
    await dispatcher.start()
    await pipeline.start()
    try:
        yield
    finally:
        await pipeline.stop()
        await dispatcher.stop()
        store.close()


app = FastAPI(title="Real-Time Log Anomaly Detector", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins.split(","),
                   allow_methods=["*"], allow_headers=["*"])

for r in (routes_health.router, routes_alerts.router, routes_metrics.router,
          routes_demo.router, routes_benchmark.router, ws.router):
    app.include_router(r)


if (FRONTEND.parent / "vendor").exists():
    app.mount("/vendor", StaticFiles(directory=FRONTEND.parent / "vendor"), name="vendor")


@app.get("/", include_in_schema=False)
async def dashboard():
    if FRONTEND.exists():
        return FileResponse(FRONTEND)
    return JSONResponse({"message": "dashboard not found; see /docs"})
