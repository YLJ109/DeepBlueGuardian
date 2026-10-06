"""API 路由聚合。"""

from __future__ import annotations

from fastapi import APIRouter

from . import (
    agent,
    alerts,
    auth,
    dashboard,
    data,
    devices,
    ingest,
    mangrove,
    model_config,
    patrol,
    predict,
    vision,
)

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(devices.router)
api_router.include_router(ingest.router)
api_router.include_router(data.router)
api_router.include_router(alerts.router)
api_router.include_router(predict.router)
api_router.include_router(mangrove.router)
api_router.include_router(model_config.router)
api_router.include_router(dashboard.router)
api_router.include_router(agent.router)
api_router.include_router(patrol.router)
api_router.include_router(vision.router)

__all__ = ["api_router"]
