"""Access to the app-wide singletons created in main.py's lifespan."""
from __future__ import annotations

from fastapi import Request

from app.core.pipeline import Pipeline
from app.db.store import AlertStore
from app.publishers.dispatcher import Dispatcher


def get_pipeline(request: Request) -> Pipeline:
    return request.app.state.pipeline


def get_store(request: Request) -> AlertStore:
    return request.app.state.store


def get_dispatcher(request: Request) -> Dispatcher:
    return request.app.state.dispatcher
