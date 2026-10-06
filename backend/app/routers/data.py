"""历史数据接口：原始时序、聚合曲线、多设备对比、CSV 导出、数据统计。"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select

from ..config import METRICS
from ..data.sites import REGION_BY_CODE
from ..deps import DbSession
from ..models import Device, MetricAggregate, SensorReading

router = APIRouter(prefix="/data", tags=["历史数据"])

GRANULARITY_HOURS = {"1m": 1 / 60, "1h": 1, "1d": 24}


@router.get("/metrics", summary="指标元数据")
def metrics_meta() -> dict:
    return {
        "code": 0,
        "data": [
            {"key": key, "label": meta["label"], "unit": meta["unit"], "range": list(meta["range"])}
            for key, meta in METRICS.items()
        ],
    }


@router.get("/history", summary="原始时序查询")
def history(
    db: DbSession,
    device_code: str = Query(..., description="设备编码"),
    start: datetime | None = None,
    end: datetime | None = None,
    metrics: str | None = Query(default=None, description="逗号分隔的指标，缺省返回全部"),
    limit: int = 5000,
    only_valid: bool = True,
) -> dict:
    end = end or datetime.now()
    start = start or (end - timedelta(hours=24))
    if start >= end:
        raise HTTPException(status_code=400, detail="start 必须早于 end")

    selected = [m.strip() for m in metrics.split(",")] if metrics else list(METRICS)
    unknown = [m for m in selected if m not in METRICS]
    if unknown:
        raise HTTPException(status_code=400, detail=f"未知指标：{', '.join(unknown)}")

    stmt = (
        select(SensorReading)
        .where(SensorReading.device_code == device_code)
        .where(SensorReading.ts >= start)
        .where(SensorReading.ts <= end)
    )
    if only_valid:
        stmt = stmt.where(SensorReading.quality_flag != 2)
    rows = db.execute(stmt.order_by(SensorReading.ts).limit(min(limit, 20000))).scalars().all()

    device = db.execute(select(Device).where(Device.device_code == device_code)).scalar_one_or_none()
    return {
        "code": 0,
        "device_code": device_code,
        "device_name": device.name if device else None,
        "start": start.isoformat(timespec="seconds"),
        "end": end.isoformat(timespec="seconds"),
        "count": len(rows),
        "metrics": [
            {"key": m, "label": METRICS[m]["label"], "unit": METRICS[m]["unit"]} for m in selected
        ],
        "timestamps": [r.ts.isoformat(timespec="seconds") for r in rows],
        "series": {m: [getattr(r, m) for r in rows] for m in selected},
        "quality": {
            "normal": sum(1 for r in rows if r.quality_flag == 0),
            "suspect": sum(1 for r in rows if r.quality_flag == 1),
            "invalid": sum(1 for r in rows if r.quality_flag == 2),
        },
    }


@router.get("/aggregate", summary="聚合曲线查询（1m/1h/1d）")
def aggregate(
    db: DbSession,
    device_code: str,
    metric: str,
    granularity: str = "1h",
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 2000,
) -> dict:
    if metric not in METRICS:
        raise HTTPException(status_code=400, detail="未知指标")
    if granularity not in GRANULARITY_HOURS:
        raise HTTPException(status_code=400, detail="granularity 仅支持 1m/1h/1d")

    end = end or datetime.now()
    start = start or (end - timedelta(days=7))
    rows = db.execute(
        select(MetricAggregate)
        .where(MetricAggregate.device_code == device_code)
        .where(MetricAggregate.metric == metric)
        .where(MetricAggregate.granularity == granularity)
        .where(MetricAggregate.bucket >= start)
        .where(MetricAggregate.bucket <= end)
        .order_by(MetricAggregate.bucket)
        .limit(min(limit, 10000))
    ).scalars().all()

    return {
        "code": 0,
        "device_code": device_code,
        "metric": metric,
        "metric_label": METRICS[metric]["label"],
        "unit": METRICS[metric]["unit"],
        "granularity": granularity,
        "timestamps": [r.bucket.isoformat(timespec="seconds") for r in rows],
        "avg": [round(r.avg_value, 3) for r in rows],
        "min": [round(r.min_value, 3) for r in rows],
        "max": [round(r.max_value, 3) for r in rows],
        "count": [r.sample_count for r in rows],
    }


@router.get("/latest", summary="全部设备最新读数")
def latest(db: DbSession, region_code: str | None = None) -> dict:
    devices = db.execute(select(Device).where(Device.type == "buoy")).scalars().all()
    result = []
    for device in devices:
        if region_code and device.region_code != region_code:
            continue
        row = db.execute(
            select(SensorReading)
            .where(SensorReading.device_code == device.device_code)
            .order_by(SensorReading.ts.desc())
            .limit(1)
        ).scalar_one_or_none()
        region = REGION_BY_CODE.get(device.region_code or "")
        result.append(
            {
                "device_code": device.device_code,
                "device_name": device.name,
                "region_code": device.region_code,
                "region_name": region.name if region else None,
                "world_x": device.world_x,
                "world_z": device.world_z,
                "longitude": device.longitude,
                "latitude": device.latitude,
                "status": device.status,
                "battery": device.battery,
                "signal": device.signal,
                "ts": row.ts.isoformat(timespec="seconds") if row else None,
                "age_seconds": int((datetime.now() - row.ts).total_seconds()) if row else None,
                "quality_flag": row.quality_flag if row else None,
                "values": {m: getattr(row, m) for m in METRICS} if row else {m: None for m in METRICS},
            }
        )
    return {"code": 0, "data": result}


@router.get("/compare", summary="多设备同指标对比")
def compare(
    db: DbSession,
    device_codes: str,
    metric: str = "chlorophyll_a",
    hours: int = 24,
    granularity: str = "1h",
) -> dict:
    if metric not in METRICS:
        raise HTTPException(status_code=400, detail="未知指标")
    codes = [c.strip() for c in device_codes.split(",") if c.strip()][:8]
    if not codes:
        raise HTTPException(status_code=400, detail="device_codes 不能为空")

    end = datetime.now()
    start = end - timedelta(hours=max(1, min(hours, 24 * 90)))
    buckets: dict[str, list[tuple[str, float]]] = {}
    for code in codes:
        rows = db.execute(
            select(MetricAggregate)
            .where(MetricAggregate.device_code == code)
            .where(MetricAggregate.metric == metric)
            .where(MetricAggregate.granularity == granularity)
            .where(MetricAggregate.bucket >= start)
            .where(MetricAggregate.bucket <= end)
            .order_by(MetricAggregate.bucket)
        ).scalars().all()
        if not rows:
            raw = db.execute(
                select(SensorReading)
                .where(SensorReading.device_code == code)
                .where(SensorReading.ts >= start)
                .order_by(SensorReading.ts)
                .limit(3000)
            ).scalars().all()
            buckets[code] = [(r.ts.isoformat(timespec="seconds"), getattr(r, metric)) for r in raw if getattr(r, metric) is not None]
        else:
            buckets[code] = [(r.bucket.isoformat(timespec="seconds"), round(r.avg_value, 3)) for r in rows]

    devices = {
        d.device_code: d.name
        for d in db.execute(select(Device).where(Device.device_code.in_(codes))).scalars().all()
    }
    return {
        "code": 0,
        "metric": metric,
        "metric_label": METRICS[metric]["label"],
        "unit": METRICS[metric]["unit"],
        "series": [
            {"device_code": code, "device_name": devices.get(code, code), "points": buckets.get(code, [])}
            for code in codes
        ],
    }


@router.get("/export", summary="历史数据 CSV 导出")
def export_csv(
    db: DbSession,
    device_code: str,
    start: datetime | None = None,
    end: datetime | None = None,
) -> StreamingResponse:
    end = end or datetime.now()
    start = start or (end - timedelta(days=1))
    rows = db.execute(
        select(SensorReading)
        .where(SensorReading.device_code == device_code)
        .where(SensorReading.ts >= start)
        .where(SensorReading.ts <= end)
        .order_by(SensorReading.ts)
    ).scalars().all()
    if not rows:
        raise HTTPException(status_code=404, detail="该时间段无数据")

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["时间", "设备编码", "水温(℃)", "pH", "溶解氧(mg/L)", "浊度(NTU)", "叶绿素a(μg/L)", "盐度(PSU)", "质量标记"]
    )
    quality_label = {0: "正常", 1: "可疑", 2: "无效"}
    for r in rows:
        writer.writerow(
            [
                r.ts.strftime("%Y-%m-%d %H:%M:%S"),
                r.device_code,
                r.water_temp,
                r.ph,
                r.dissolved_oxygen,
                r.turbidity,
                r.chlorophyll_a,
                r.salinity,
                quality_label.get(r.quality_flag, r.quality_flag),
            ]
        )
    buffer.seek(0)
    filename = f"{device_code}_{start:%Y%m%d%H%M}_{end:%Y%m%d%H%M}.csv"
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/volume", summary="数据量统计")
def volume(db: DbSession) -> dict:
    readings = int(db.execute(select(func.count()).select_from(SensorReading)).scalar_one())
    aggregates = int(db.execute(select(func.count()).select_from(MetricAggregate)).scalar_one())
    earliest = db.execute(select(func.min(SensorReading.ts))).scalar_one()
    latest_ts = db.execute(select(func.max(SensorReading.ts))).scalar_one()
    per_device = db.execute(
        select(SensorReading.device_code, func.count()).group_by(SensorReading.device_code)
    ).all()
    return {
        "code": 0,
        "data": {
            "readings": readings,
            "aggregates": aggregates,
            "earliest": earliest.isoformat(timespec="seconds") if earliest else None,
            "latest": latest_ts.isoformat(timespec="seconds") if latest_ts else None,
            "by_device": [{"device_code": code, "count": int(count)} for code, count in per_device],
            "estimated_daily_growth": sum(int(c) for _, c in per_device) // max(
                1, (datetime.now() - earliest).days if earliest else 1
            ),
        },
    }
