"""后台任务：聚合重建、预测刷新、离线巡检、数据清理。"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta

from sqlalchemy import select

from .config import settings
from .database import SessionLocal
from .models import Device, SensorReading, SystemEvent
from .services.aggregates import cleanup_old_data, rebuild_aggregates
from .services.alerts import AlertEngine
from .services.notifier import bus
from .services.predictor import predict_all

logger = logging.getLogger("deepblue.tasks")


def run_aggregation() -> dict:
    db = SessionLocal()
    try:
        result = rebuild_aggregates(db, since=datetime.now() - timedelta(days=2))
        return result
    finally:
        db.close()


def run_prediction(horizon_hours: int = 48) -> int:
    db = SessionLocal()
    try:
        results = predict_all(db, horizon_hours=horizon_hours, persist=True)
        return len([r for r in results if "error" not in r])
    finally:
        db.close()


def run_offline_check() -> int:
    db = SessionLocal()
    try:
        return len(AlertEngine(db).check_device_offline())
    finally:
        db.close()


def detect_sensor_stuck() -> int:
    """检出连续 2 小时数值无变化的通道（传感器卡死/结垢）。"""

    db = SessionLocal()
    created = 0
    try:
        devices = db.execute(select(Device).where(Device.type == "buoy")).scalars().all()
        engine = AlertEngine(db)
        since = datetime.now() - timedelta(hours=2)
        for device in devices:
            rows = db.execute(
                select(SensorReading)
                .where(SensorReading.device_code == device.device_code)
                .where(SensorReading.ts >= since)
                .order_by(SensorReading.ts)
            ).scalars().all()
            if len(rows) < 12:
                continue
            for metric in ("water_temp", "ph", "dissolved_oxygen", "turbidity", "chlorophyll_a", "salinity"):
                values = [getattr(r, metric) for r in rows if getattr(r, metric) is not None]
                if len(values) >= 12 and len(set(values)) == 1:
                    result = engine._create_alert(  # noqa: SLF001 - 复用统一告警落库与推送逻辑
                        device=device,
                        level="warning",
                        alert_type="device",
                        title=f"{metric} 通道疑似卡死",
                        content=f"{device.name} 的 {metric} 通道连续 2 小时输出恒定值 {values[0]}，请检查探头结垢或接口故障",
                        metric=metric,
                        value=values[0],
                        dedup_key=f"stuck:{device.device_code}:{metric}",
                    )
                    if result:
                        created += 1
        return created
    finally:
        db.close()


class BackgroundScheduler(threading.Thread):
    """轻量调度线程：聚合 60 s、预测 15 min、离线巡检 5 min、清理 1 天。"""

    def __init__(self) -> None:
        super().__init__(name="deepblue-scheduler", daemon=True)
        self._stop = threading.Event()
        self.counters = {"aggregate": 0, "predict": 0, "offline": 0, "cleanup": 0}

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:  # pragma: no cover - 后台线程
        self._log("调度线程启动")
        last_predict = 0.0
        last_offline = 0.0
        last_cleanup = time.time()

        while not self._stop.is_set():
            now = time.time()
            try:
                if settings.aggregator_enabled:
                    run_aggregation()
                    self.counters["aggregate"] += 1
            except Exception as exc:
                self._log(f"聚合任务失败：{exc}", level="error")

            if now - last_predict >= 900:
                try:
                    count = run_prediction()
                    self.counters["predict"] += 1
                    self._log(f"全量预测完成，覆盖 {count} 个点位")
                except Exception as exc:
                    self._log(f"预测任务失败：{exc}", level="error")
                last_predict = now

            if now - last_offline >= 300:
                try:
                    self.counters["offline"] += run_offline_check()
                    self.counters["offline"] += detect_sensor_stuck()
                except Exception as exc:
                    self._log(f"离线巡检失败：{exc}", level="error")
                last_offline = now

            if now - last_cleanup >= 86400:
                db = SessionLocal()
                try:
                    removed = cleanup_old_data(db, keep_days=180)
                    self.counters["cleanup"] += removed
                    self._log(f"历史数据清理完成，删除 {removed} 条超期原始记录")
                except Exception as exc:
                    self._log(f"数据清理失败：{exc}", level="error")
                finally:
                    db.close()
                last_cleanup = now

            self._stop.wait(max(15, settings.aggregator_interval_seconds))

    def _log(self, message: str, level: str = "info") -> None:
        logger.info(message)
        db = SessionLocal()
        try:
            db.add(SystemEvent(category="scheduler", level=level, message=message, detail={"counters": self.counters}))
            db.commit()
        except Exception:  # pragma: no cover
            db.rollback()
        finally:
            db.close()
        bus.broadcast("system", {"message": message, "level": level, "ts": datetime.now().isoformat(timespec="seconds")})
