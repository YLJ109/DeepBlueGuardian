"""赤潮风险预测服务：从数据库取时序窗口，调用模型并记录预测日志。"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from ..config import RISK_LEVELS
from ..data.sites import REGION_BY_CODE
from ..ml.features import METRIC_ORDER
from ..ml.red_tide import get_model
from ..models import Device, PredictionLog, SensorReading
from .alerts import AlertEngine

logger = logging.getLogger("deepblue.predictor")

WINDOW_LENGTH = 24


def fetch_window(db: Session, device_code: str, end: datetime | None = None,
                 length: int = WINDOW_LENGTH) -> list[dict]:
    """按时间倒序取最近的 N 条有效记录，再转为正序。"""

    end = (end or datetime.now()).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(hours=length)
    rows = db.execute(
        select(SensorReading)
        .where(SensorReading.device_code == device_code)
        .where(SensorReading.ts > start)
        .where(SensorReading.ts <= end)
        .where(SensorReading.quality_flag != 2)
        .order_by(SensorReading.ts)
    ).scalars().all()
    if not rows:
        return []

    # 按小时重采样：每个小时取均值。设备采样间隔不同（10 min / 5 min）时，
    # 模型输入窗口始终保持「最近 24 个小时均值」的固定语义。
    buckets: dict[datetime, dict[str, list[float]]] = {}
    for row in rows:
        bucket = row.ts.replace(minute=0, second=0, microsecond=0)
        item = buckets.setdefault(bucket, {metric: [] for metric in METRIC_ORDER})
        for metric in METRIC_ORDER:
            value = getattr(row, metric)
            if value is not None:
                item[metric].append(float(value))

    records: list[dict] = []
    previous: dict[str, float] | None = None
    for offset in range(length, 0, -1):
        bucket = end - timedelta(hours=offset - 1)
        item = buckets.get(bucket)
        if not item or all(not v for v in item.values()):
            if previous is None:
                continue
            values = dict(previous)
        else:
            values = {
                metric: (round(sum(series) / len(series), 3) if series else (previous or {}).get(metric))
                for metric, series in item.items()
            }
            if any(v is None for v in values.values()):
                values = {k: (v if v is not None else (previous or {}).get(k, 0.0)) for k, v in values.items()}
            previous = values
        records.append({"ts": bucket.isoformat(timespec="seconds"), **values})
    return records


def predict_device(
    db: Session,
    device: Device,
    horizon_hours: int = 48,
    persist: bool = True,
    raise_alert: bool = False,
) -> dict[str, Any]:
    """对单台设备执行一次预测。"""

    records = fetch_window(db, device.device_code)
    if not records:
        return {
            "device_code": device.device_code,
            "region_code": device.region_code,
            "error": "该设备暂无可用于预测的有效观测数据",
        }

    model = get_model()
    ts = datetime.now().replace(microsecond=0)
    result = model.predict_records(records, ts=ts, horizon_hours=horizon_hours, window_length=WINDOW_LENGTH)
    region = REGION_BY_CODE.get(device.region_code or "")
    result.update(
        {
            "device_code": device.device_code,
            "device_name": device.name,
            "region_code": device.region_code,
            "region_name": region.name if region else None,
            "sample_count": len(records),
        }
    )

    if persist:
        db.add(
            PredictionLog(
                device_code=device.device_code,
                region_code=device.region_code,
                ts=ts,
                horizon_hours=horizon_hours,
                probability=result["probability"],
                level=result["level"],
                model_version=result["model_version"],
                top_factors=result["top_factors"],
                features=result["basis"],
            )
        )
        db.commit()

    if raise_alert:
        AlertEngine(db).raise_model_alert(device, {**result, "level": result["level"]})

    return result


def predict_all(db: Session, horizon_hours: int = 48, persist: bool = True) -> list[dict[str, Any]]:
    devices = db.execute(
        select(Device).where(Device.type == "buoy").order_by(Device.device_code)
    ).scalars().all()
    return [predict_device(db, d, horizon_hours=horizon_hours, persist=persist) for d in devices]


def region_risk_summary(db: Session, hours: int = 48) -> dict[str, Any]:
    """按区域汇总最新预测，用于大屏环形图与风险排行。"""

    since = datetime.now() - timedelta(hours=hours)
    rows = db.execute(
        select(PredictionLog).where(PredictionLog.ts >= since).order_by(desc(PredictionLog.ts))
    ).scalars().all()

    latest: dict[str, PredictionLog] = {}
    for row in rows:
        latest.setdefault(row.device_code, row)

    distribution = {level: 0 for level in RISK_LEVELS}
    regions: dict[str, dict[str, Any]] = {}
    for row in latest.values():
        distribution[row.level] = distribution.get(row.level, 0) + 1
        code = row.region_code or "UNKNOWN"
        bucket = regions.setdefault(
            code,
            {
                "region_code": code,
                "region_name": REGION_BY_CODE[code].name if code in REGION_BY_CODE else code,
                "max_level": 1,
                "max_probability": 0.0,
                "devices": 0,
            },
        )
        bucket["devices"] += 1
        if row.probability > bucket["max_probability"]:
            bucket["max_probability"] = round(row.probability, 4)
        bucket["max_level"] = max(bucket["max_level"], row.level)

    ordered = sorted(regions.values(), key=lambda r: (r["max_level"], r["max_probability"]), reverse=True)
    for item in ordered:
        info = RISK_LEVELS[item["max_level"]]
        item["level_label"] = info["label"]
        item["level_name"] = info["name"]
        item["color"] = info["color"]

    overall = max((r.level for r in latest.values()), default=1)
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "device_count": len(latest),
        "distribution": [
            {
                "level": level,
                "label": RISK_LEVELS[level]["label"],
                "name": RISK_LEVELS[level]["name"],
                "color": RISK_LEVELS[level]["color"],
                "count": distribution.get(level, 0),
            }
            for level in sorted(RISK_LEVELS)
        ],
        "regions": ordered,
        "overall_level": overall,
        "overall_label": RISK_LEVELS[overall]["label"],
        "overall_name": RISK_LEVELS[overall]["name"],
    }
