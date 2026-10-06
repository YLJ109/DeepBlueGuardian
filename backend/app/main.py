"""FastAPI 应用入口。

启动：
    uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

首次启动会自动建表、写入连江本地基础数据、回填历史观测并启动后台任务。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select, text

from .config import PROJECT_DIR, settings
from .database import SessionLocal, init_db
from .models import SensorReading, SystemEvent
from .routers import api_router
from .services.notifier import bus
from .services import vision as vision_service
from .tasks import BackgroundScheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("deepblue.main")

FRONTEND_DIR = PROJECT_DIR / "frontend"
SIMULATOR_THREAD = None
SCHEDULER_THREAD = None
VISION_THREAD: threading.Thread | None = None


def _bootstrap() -> None:
    """建表、补齐基础数据、必要时回填历史。"""

    from .seed import seed_reference, seed_users
    from .services.simulator import backfill_history

    init_db()
    db = SessionLocal()
    try:
        refs = seed_reference(db)
        users = seed_users(db)
        if any(refs.values()) or users:
            logger.info("基础数据初始化：%s，账号 %s 个", refs, users)

        has_readings = int(db.execute(select(func.count()).select_from(SensorReading)).scalar_one())
        if has_readings == 0 and settings.simulator_enabled:
            stats = backfill_history(db, days=settings.simulator_history_days)
            logger.info("历史观测回填完成：%s", stats)
            from .services.aggregates import rebuild_aggregates
            from .services.predictor import predict_all

            logger.info("聚合数据重建：%s", rebuild_aggregates(db))
            predict_all(db, persist=True)
            logger.info("初始风险预测完成")
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global SIMULATOR_THREAD, SCHEDULER_THREAD
    global VISION_THREAD

    bus.bind_loop(asyncio.get_running_loop())
    _bootstrap()

    db = SessionLocal()
    try:
        db.add(
            SystemEvent(
                category="system",
                level="info",
                message="平台服务启动",
                detail={"version": settings.version, "simulator": settings.simulator_enabled},
            )
        )
        db.commit()
    finally:
        db.close()

    if settings.simulator_enabled:
        from .services.simulator import SimulatorWorker

        SIMULATOR_THREAD = SimulatorWorker()
        SIMULATOR_THREAD.start()
        logger.info("仿真数据源已启动，采样间隔 %s 秒", SIMULATOR_THREAD.interval)

    SCHEDULER_THREAD = BackgroundScheduler()
    SCHEDULER_THREAD.start()

    if settings.vision_enabled:
        def _warm_vision() -> None:
            # 推理节点加载权重需要数秒，放到后台线程，不拖慢接口服务启动
            if vision_service.ensure_service():
                logger.info("视觉推理节点可用：%s", settings.vision_base_url)
            else:
                logger.warning("视觉推理节点未就绪，视频 AI 识别暂不可用")

        VISION_THREAD = threading.Thread(target=_warm_vision, name="vision-warmup", daemon=True)
        VISION_THREAD.start()

    yield

    vision_service.shutdown()

    if SIMULATOR_THREAD is not None:
        SIMULATOR_THREAD.stop()
        SIMULATOR_THREAD.join(timeout=5)
    if SCHEDULER_THREAD is not None:
        SCHEDULER_THREAD.stop()
        SCHEDULER_THREAD.join(timeout=5)
    logger.info("后台任务已停止")


app = FastAPI(
    title=settings.app_name,
    version=settings.version,
    description="面向连江县海洋牧场与红树林湿地的软硬件一体化海洋生态智能监测平台。",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix=settings.api_prefix)


@app.get("/api/health", tags=["系统"], summary="运行健康检查")
def health() -> dict:
    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
        database = "ok"
    finally:
        db.close()
    return {
        "code": 0,
        "status": "ok",
        "version": settings.version,
        "database": database,
        "simulator": settings.simulator_enabled,
        "vision": settings.vision_enabled and vision_service.health(timeout=1.5) is not None,
    }

@app.get("/api", tags=["系统"], summary="接口索引")
def api_index() -> dict:
    return {
        "code": 0,
        "name": settings.app_name,
        "version": settings.version,
        "docs": "/api/docs",
        "prefix": settings.api_prefix,
        "websocket": "/ws",
        "modules": [
            "认证 /api/v1/auth",
            "设备管理 /api/v1/devices",
            "数据接入 /api/v1/ingest",
            "历史数据 /api/v1/data",
            "告警管理 /api/v1/alerts",
            "赤潮预测 /api/v1/predict",
            "红树林 /api/v1/mangrove",
            "模型配置 /api/v1/models",
            "大屏总览 /api/v1/dashboard",
            "海洋生态智能体 /api/v1/agent",
            "视频AI识别 /api/v1/vision",
            "巡护任务 /api/v1/patrol",
        ],
    }


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """实时通道：推送观测数据、告警、系统事件。"""

    await bus.manager.connect(websocket)
    try:
        await websocket.send_json(
            {
                "type": "welcome",
                "ts": datetime.now().isoformat(timespec="seconds"),
                "data": {"message": "已接入实时数据通道", "recent": bus.recent[-20:]},
            }
        )
        while True:
            try:
                message = await asyncio.wait_for(websocket.receive_text(), timeout=30)
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "heartbeat", "ts": datetime.now().isoformat(timespec="seconds")})
                continue
            if message == "ping":
                await websocket.send_json({"type": "pong", "ts": datetime.now().isoformat(timespec="seconds")})
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # pragma: no cover
        logger.debug("WebSocket 异常关闭：%s", exc)
    finally:
        await bus.manager.disconnect(websocket)


# ---------------------------------------------------------------------------
# 前端静态资源
# ---------------------------------------------------------------------------
if FRONTEND_DIR.exists():
    assets = FRONTEND_DIR / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")
    vendor = FRONTEND_DIR / "vendor"
    if vendor.exists():
        app.mount("/vendor", StaticFiles(directory=str(vendor)), name="vendor")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(FRONTEND_DIR / "index.html")

    @app.get("/console", include_in_schema=False)
    def console() -> FileResponse:
        return FileResponse(FRONTEND_DIR / "console.html")

    @app.get("/m", include_in_schema=False)
    def mobile() -> FileResponse:
        return FileResponse(FRONTEND_DIR / "mobile.html")

    @app.get("/screen", include_in_schema=False)
    def screen() -> FileResponse:
        return FileResponse(FRONTEND_DIR / "screen.html")

    @app.get("/favicon.svg", include_in_schema=False)
    def favicon() -> FileResponse:
        return FileResponse(FRONTEND_DIR / "favicon.svg")
else:  # pragma: no cover
    @app.get("/", include_in_schema=False)
    def index_missing() -> JSONResponse:
        return JSONResponse(
            status_code=200,
            content={"code": 0, "message": "前端资源目录不存在，请检查 frontend/ 是否完整"},
        )


@app.exception_handler(404)
async def not_found(_request, exc):  # pragma: no cover
    return JSONResponse(status_code=404, content={"code": 404, "message": getattr(exc, "detail", "资源不存在")})
