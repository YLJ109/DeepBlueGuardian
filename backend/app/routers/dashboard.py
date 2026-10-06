"""大屏总览接口：一次请求返回三栏大屏所需的全部数据。"""

from __future__ import annotations

import time
from datetime import datetime, timedelta

from fastapi import APIRouter
from sqlalchemy import func, select

from ..config import METRICS, RISK_LEVELS, settings
from ..data.sites import REGION_BY_CODE
from ..deps import DbSession
from ..models import Alert, Device, MangroveStat, MetricAggregate, PredictionLog, SensorReading, SystemEvent
from ..ml.red_tide import get_model
from ..services.aggregates import data_volume
from ..services.notifier import bus

router = APIRouter(prefix="/dashboard", tags=["大屏总览"])

START_TIME = time.time()

# 监测海域生态类型构成（公顷）。数值均取自公开资料：
# 国家级海洋牧场 258 ha、已建人工鱼礁 36 ha、三处红树林合计 460.2 ha、
# 深远海养殖平台占位约 24 ha、马鼻镇滩涂约 7.5 万亩（5000 ha）。
ECO_COMPOSITION = (
    {"name": "红树林湿地", "value": 460.2, "color": "#29e7a0", "source": "Sentinel-2 分割统计"},
    {"name": "人工鱼礁区", "value": 36.0, "color": "#33e1ff", "source": "国家级海洋牧场公示"},
    {"name": "海洋牧场核心区", "value": 258.0, "color": "#4c8dff", "source": "农业农村部第九批国家级海洋牧场"},
    {"name": "深远海养殖平台区", "value": 24.0, "color": "#a78bfa", "source": "“百台万吨”工程平台占位估算"},
    {"name": "重点潮间带滩涂", "value": 5000.0, "color": "#ffd166", "source": "马鼻镇滩涂约 7.5 万亩"},
)


def _latest_readings(db, devices: list[Device]) -> list[dict]:
    """批量取每台设备的最新读数，避免 N+1 查询。"""

    if not devices:
        return []
    codes = [d.device_code for d in devices]
    subq = (
        select(
            SensorReading.device_code,
            func.max(SensorReading.ts).label("max_ts"),
        )
        .where(SensorReading.device_code.in_(codes))
        .group_by(SensorReading.device_code)
        .subquery()
    )
    rows = db.execute(
        select(SensorReading).join(
            subq,
            (SensorReading.device_code == subq.c.device_code) & (SensorReading.ts == subq.c.max_ts),
        )
    ).scalars().all()
    return rows


@router.get("/overview", summary="大屏总览数据")
def overview(db: DbSession) -> dict:
    now = datetime.now().replace(microsecond=0)
    devices = db.execute(select(Device).order_by(Device.device_code)).scalars().all()
    buoys = [d for d in devices if d.type == "buoy"]
    online = [d for d in devices if d.status == "online"]

    readings = _latest_readings(db, buoys)
    reading_map = {r.device_code: r for r in readings}

    latest_payload: list[dict] = []
    metric_accumulator: dict[str, list[float]] = {m: [] for m in METRICS}
    for device in buoys:
        row = reading_map.get(device.device_code)
        region = REGION_BY_CODE.get(device.region_code or "")
        values = {m: (getattr(row, m) if row else None) for m in METRICS}
        for metric, value in values.items():
            if value is not None:
                metric_accumulator[metric].append(float(value))
        latest_payload.append(
            {
                "device_code": device.device_code,
                "device_name": device.name,
                "type": device.type,
                "region_code": device.region_code,
                "region_name": region.name if region else None,
                "longitude": device.longitude,
                "latitude": device.latitude,
                "world_x": device.world_x,
                "world_z": device.world_z,
                "status": device.status,
                "battery": device.battery,
                "signal": device.signal,
                "solar_voltage": device.solar_voltage,
                "firmware": device.firmware,
                "depth_m": device.depth_m,
                "site": (device.config or {}).get("site"),
                "summary": (device.config or {}).get("summary"),
                "last_seen": device.last_seen.isoformat(timespec="seconds") if device.last_seen else None,
                "ts": row.ts.isoformat(timespec="seconds") if row else None,
                "age_seconds": int((now - row.ts).total_seconds()) if row else None,
                "quality_flag": row.quality_flag if row else None,
                "source": row.source if row else None,
                "values": values,
            }
        )

    averages = {
        metric: round(sum(values) / len(values), 2) if values else None
        for metric, values in metric_accumulator.items()
    }

    # 24 小时小时级趋势（优先使用聚合表，缺失时用原始数据兜底）
    since = now - timedelta(hours=24)
    trend_labels: list[str] = []
    trend_series: dict[str, list[float | None]] = {m: [] for m in METRICS}
    buckets = [since.replace(minute=0, second=0) + timedelta(hours=i) for i in range(25)]
    agg_rows = db.execute(
        select(MetricAggregate)
        .where(MetricAggregate.granularity == "1h")
        .where(MetricAggregate.bucket >= buckets[0])
        .order_by(MetricAggregate.bucket)
    ).scalars().all()
    agg_index: dict[tuple[str, str], dict[datetime, float]] = {}
    for row in agg_rows:
        key = (row.metric, row.granularity)
        agg_index.setdefault(key, {})[row.bucket] = row.avg_value

    for bucket in buckets:
        trend_labels.append(bucket.strftime("%H:00"))
        for metric in METRICS:
            series = agg_index.get((metric, "1h"))
            if series:
                values = [v for ts, v in series.items() if ts <= bucket][-6:]
                trend_series[metric].append(round(sum(values) / len(values), 2) if values else None)
            else:
                trend_series[metric].append(None)

    if all(all(v is None for v in trend_series[m]) for m in METRICS):
        raw = db.execute(
            select(SensorReading).where(SensorReading.ts >= since).order_by(SensorReading.ts).limit(20000)
        ).scalars().all()
        acc: dict[tuple[str, str], list[float]] = {}
        for r in raw:
            key_hour = r.ts.replace(minute=0, second=0, microsecond=0).strftime("%H:00")
            for metric in METRICS:
                value = getattr(r, metric)
                if value is not None:
                    acc.setdefault((metric, key_hour), []).append(float(value))
        trend_labels = [b.strftime("%H:00") for b in buckets]
        trend_series = {
            metric: [
                round(sum(acc[(metric, label)]) / len(acc[(metric, label)]), 2) if (metric, label) in acc else None
                for label in trend_labels
            ]
            for metric in METRICS
        }

    alert_rows = db.execute(
        select(Alert).order_by(Alert.created_at.desc()).limit(20)
    ).scalars().all()
    pending_alerts = int(
        db.execute(
            select(func.count()).select_from(Alert).where(Alert.status.in_(("pending", "handling")))
        ).scalar_one()
    )
    critical_24h = int(
        db.execute(
            select(func.count())
            .select_from(Alert)
            .where(Alert.created_at >= since)
            .where(Alert.level.in_(("warning", "critical")))
        ).scalar_one()
    )
    alert_hourly: dict[str, int] = {}
    for alert in db.execute(select(Alert).where(Alert.created_at >= since)).scalars().all():
        key = alert.created_at.strftime("%H:00")
        alert_hourly[key] = alert_hourly.get(key, 0) + 1

    # 风险分布
    latest_pred = db.execute(
        select(PredictionLog).where(PredictionLog.ts >= now - timedelta(hours=48)).order_by(PredictionLog.ts.desc())
    ).scalars().all()
    seen: set[str] = set()
    risk_distribution = {level: 0 for level in RISK_LEVELS}
    region_risk: dict[str, dict] = {}
    risk_points: list[dict] = []
    for row in latest_pred:
        if row.device_code in seen:
            continue
        seen.add(row.device_code)
        risk_distribution[row.level] = risk_distribution.get(row.level, 0) + 1
        device = next((d for d in buoys if d.device_code == row.device_code), None)
        region = REGION_BY_CODE.get(row.region_code or "")
        bucket = region_risk.setdefault(
            row.region_code or "UNKNOWN",
            {
                "region_code": row.region_code,
                "region_name": region.name if region else row.region_code,
                "level": 1,
                "probability": 0.0,
                "devices": 0,
            },
        )
        bucket["devices"] += 1
        bucket["probability"] = max(bucket["probability"], round(row.probability, 4))
        bucket["level"] = max(bucket["level"], row.level)
        risk_points.append(
            {
                "device_code": row.device_code,
                "device_name": device.name if device else row.device_code,
                "region_code": row.region_code,
                "region_name": region.name if region else None,
                "world_x": device.world_x if device else None,
                "world_z": device.world_z if device else None,
                "probability": round(row.probability, 4),
                "level": row.level,
                "level_label": RISK_LEVELS[row.level]["label"],
                "level_name": RISK_LEVELS[row.level]["name"],
                "color": RISK_LEVELS[row.level]["color"],
                "top_factors": row.top_factors or [],
                "ts": row.ts.isoformat(timespec="seconds"),
            }
        )
    for bucket in region_risk.values():
        info = RISK_LEVELS[bucket["level"]]
        bucket["level_label"] = info["label"]
        bucket["level_name"] = info["name"]
        bucket["color"] = info["color"]

    overall_level = max(risk_distribution.items(), key=lambda kv: (kv[1] > 0, kv[0]))[0] if seen else 1
    if seen:
        overall_level = max((row.level for row in latest_pred if row.device_code in seen), default=1)

    # 红树林
    mangrove_rows = db.execute(select(MangroveStat).order_by(MangroveStat.region_code, MangroveStat.period)).scalars().all()
    mangrove_latest: dict[str, MangroveStat] = {}
    for row in mangrove_rows:
        mangrove_latest[row.region_code] = row
    mangrove_area = round(sum(r.area_ha for r in mangrove_latest.values()), 2)
    mangrove_health = (
        round(sum(r.health_score * r.area_ha for r in mangrove_latest.values()) / mangrove_area, 2)
        if mangrove_area
        else None
    )

    region_counter: dict[str, int] = {}
    for device in devices:
        region_counter[device.region_code or ""] = region_counter.get(device.region_code or "", 0) + 1

    volume = data_volume(db)
    model = get_model()
    readings_per_device = {item["device_code"]: item["count"] for item in volume.get("by_device", [])}

    return {
        "code": 0,
        "generated_at": now.isoformat(timespec="seconds"),
        "summary": {
            "project": settings.app_name,
            "version": settings.version,
            "device_total": len(devices),
            "device_online": len(online),
            "buoy_total": len(buoys),
            "buoy_online": sum(1 for d in buoys if d.status == "online"),
            "online_rate": round(len(online) / len(devices) * 100, 1) if devices else 0.0,
            "pending_alerts": pending_alerts,
            "critical_24h": critical_24h,
            "risk_level": overall_level,
            "risk_label": RISK_LEVELS[overall_level]["label"],
            "risk_name": RISK_LEVELS[overall_level]["name"],
            "risk_color": RISK_LEVELS[overall_level]["color"],
            "readings_total": volume["readings"],
            "aggregates_total": volume["aggregates"],
            "mangrove_area_ha": mangrove_area,
            "mangrove_health": mangrove_health,
            "model_mode": "tree-ensemble" if model.loaded else "rule-based-fallback",
            "model_version": model.version,
            "uptime_seconds": int(time.time() - START_TIME),
            "simulator_enabled": settings.simulator_enabled,
            "ws_clients": bus.manager.stats.get("connected", 0),
            "ws_sent": bus.manager.stats.get("sent", 0),
        },
        "averages": averages,
        "latest": latest_payload,
        "regions": [
            {
                "code": code,
                "name": REGION_BY_CODE[code].name,
                "kind": REGION_BY_CODE[code].kind,
                "longitude": REGION_BY_CODE[code].longitude,
                "latitude": REGION_BY_CODE[code].latitude,
                "world_x": REGION_BY_CODE[code].world[0],
                "world_z": REGION_BY_CODE[code].world[1],
                "remark": REGION_BY_CODE[code].remark,
                "device_count": count,
                "reading_count": sum(readings_per_device.get(d.device_code, 0) for d in devices if d.region_code == code),
            }
            for code, count in sorted(region_counter.items())
            if code in REGION_BY_CODE
        ],
        "alerts": [
            {
                "id": a.id,
                "alert_code": a.alert_code,
                "device_code": a.device_code,
                "region_code": a.region_code,
                "region_name": REGION_BY_CODE[a.region_code].name if a.region_code in REGION_BY_CODE else None,
                "level": a.level,
                "type": a.type,
                "title": a.title,
                "content": a.content,
                "metric": a.metric,
                "metric_label": METRICS.get(a.metric or "", {}).get("label", a.metric),
                "value": a.value,
                "threshold": a.threshold,
                "status": a.status,
                "created_at": a.created_at.isoformat(timespec="seconds"),
            }
            for a in alert_rows
        ],
        "alert_trend": [
            {"bucket": b.strftime("%H:00"), "count": alert_hourly.get(b.strftime("%H:00"), 0)}
            for b in buckets
        ],
        "risk": {
            "overall_level": overall_level,
            "overall_label": RISK_LEVELS[overall_level]["label"],
            "overall_name": RISK_LEVELS[overall_level]["name"],
            "overall_color": RISK_LEVELS[overall_level]["color"],
            "device_count": len(seen),
            "distribution": [
                {"level": level, **RISK_LEVELS[level], "count": risk_distribution.get(level, 0)}
                for level in sorted(RISK_LEVELS)
            ],
            "regions": sorted(region_risk.values(), key=lambda r: (r["level"], r["probability"]), reverse=True),
            "points": sorted(risk_points, key=lambda p: p["probability"], reverse=True),
        },
        "mangrove": {
            "total_area_ha": mangrove_area,
            "average_health": mangrove_health,
            "items": [
                {
                    "region_code": code,
                    "region_name": REGION_BY_CODE[code].name if code in REGION_BY_CODE else code,
                    "area_ha": row.area_ha,
                    "change_pct": row.change_pct,
                    "health_score": row.health_score,
                    "health_level": row.health_level,
                    "ndvi_mean": row.ndvi_mean,
                    "ndwi_mean": row.ndwi_mean,
                    "period": row.period,
                    "data_source": row.data_source,
                }
                for code, row in mangrove_latest.items()
            ],
        },
        "eco_share": list(ECO_COMPOSITION),
        "trend": {
            "labels": trend_labels,
            "unit": {m: METRICS[m]["unit"] for m in METRICS},
            "label": {m: METRICS[m]["label"] for m in METRICS},
            "series": trend_series,
        },
    }


@router.get("/health", summary="服务健康检查")
def health(db: DbSession) -> dict:
    db.execute(select(1))
    return {
        "code": 0,
        "status": "ok",
        "time": datetime.now().isoformat(timespec="seconds"),
        "uptime_seconds": int(time.time() - START_TIME),
        "ws_clients": bus.manager.stats.get("connected", 0),
    }


@router.get("/events", summary="大屏事件流（最近事件）")
def recent_events(db: DbSession, limit: int = 30) -> dict:
    rows = db.execute(select(SystemEvent).order_by(SystemEvent.id.desc()).limit(min(limit, 200))).scalars().all()
    return {
        "code": 0,
        "data": [
            {
                "id": r.id,
                "category": r.category,
                "level": r.level,
                "message": r.message,
                "detail": r.detail,
                "created_at": r.created_at.isoformat(timespec="seconds"),
            }
            for r in rows
        ],
    }
