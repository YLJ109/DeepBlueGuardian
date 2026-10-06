"""海洋生态智能体：大模型决策 + 平台数据工具调用（function calling）。

角色定位
--------
大模型是「调度与表达层」，不是「数据源」。所有事实必须由工具从平台数据库读取：
设备台账、水质观测、聚合趋势、告警闭环、赤潮风险预测、红树林遥感统计。
模型负责理解意图、选择工具、组织结论与处置建议；数值一律来自工具返回值。

安全边界
--------
- 只读工具（查询类）对匿名与全部角色开放，保证大屏问答链路随时可用；
- 需要动作的工具（巡检、处置草稿）要求技术员及以上角色，并写入 AuditLog；
- 处置建议只产出「待人工确认」的草稿，不直接改写告警状态，避免模型越权落库。

降级策略
--------
第三方大模型不可用时（超时、限流、密钥失效），自动降级为本地规则编排：
按问题关键词命中同一批工具，用模板组织答复，并在返回体中标记 degraded=true，
保证演示链路不中断，同时不伪造任何数据。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Iterator

import httpx
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from ..config import ALERT_THRESHOLDS, METRICS, RISK_LEVELS, settings
from ..data.sites import REGION_BY_CODE
from ..models import Alert, AuditLog, Device, MangroveStat, PredictionLog, SensorReading
from .alerts import AlertEngine

logger = logging.getLogger("deepblue.agent")

_ROLE_WEIGHT = {"viewer": 1, "farmer": 1, "technician": 2, "admin": 3}
_OPS = {
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
}
_LEVEL_WEIGHT = {"info": 1, "warning": 2, "critical": 3}
_ALERT_LEVEL_LABEL = {"info": "提示", "warning": "警告", "critical": "严重"}
_ALERT_TYPE_LABEL = {"threshold": "阈值越限", "trend": "趋势异常", "model": "模型预警", "device": "设备异常"}


# ---------------------------------------------------------------------------
# 工具定义
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ToolSpec:
    name: str
    label: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[Session, dict, Any], dict]
    min_role: str = "viewer"
    kind: str = "read"  # read / action


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or []}


def _num(description: str, default: Any = None) -> dict[str, Any]:
    item: dict[str, Any] = {"type": "integer", "description": description}
    if default is not None:
        item["default"] = default
    return item


def _text(description: str) -> dict[str, Any]:
    return {"type": "string", "description": description}


# ---------------------------------------------------------------------------
# 通用查询辅助
# ---------------------------------------------------------------------------
def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def _clamp_hours(value: Any, default: int, minimum: int = 1, maximum: int = 24 * 30) -> int:
    try:
        hours = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, hours))


def _region_name(code: str | None) -> str | None:
    region = REGION_BY_CODE.get(code or "")
    return region.name if region else None


def _prefer_sensors(devices: list[Device]) -> list[Device]:
    """同一站点通常同时部署浮标、网关与摄像头，水质/趋势类问题应优先水质传感器。"""

    buoys = [d for d in devices if d.type == "buoy"]
    return buoys or devices


def _match_devices(db: Session, keyword: str | None, limit: int = 10) -> list[Device]:
    """按设备编码、设备名称、区域名称匹配设备。"""

    stmt = select(Device)
    if keyword:
        text = str(keyword).strip()
        like = f"%{text}%"
        region_codes = [code for code, region in REGION_BY_CODE.items() if text in region.name]
        condition = Device.device_code.like(like) | Device.name.like(like)
        if region_codes:
            condition = condition | Device.region_code.in_(region_codes)
        stmt = stmt.where(condition)
    return db.execute(stmt.order_by(Device.device_code).limit(limit)).scalars().all()


def _metric_flags(reading: SensorReading | None) -> list[dict]:
    """按平台统一的阈值规则给出越限标记，保证与告警口径一致。"""

    if reading is None:
        return []
    flags: list[dict] = []
    for metric, rules in ALERT_THRESHOLDS.items():
        value = getattr(reading, metric, None)
        if value is None:
            continue
        for rule in rules:
            if _OPS[rule["op"]](value, rule["value"]):
                flags.append(
                    {
                        "metric": metric,
                        "metric_label": METRICS[metric]["label"],
                        "level": rule["level"],
                        "value": round(float(value), 2),
                        "threshold": rule["value"],
                        "text": rule["label"],
                    }
                )
                break
    return flags


def _reading_payload(row: SensorReading | None) -> dict:
    if row is None:
        return {metric: None for metric in METRICS}
    return {metric: (round(float(v), 2) if v is not None else None) for metric, v in
            ((m, getattr(row, m)) for m in METRICS)}


def _latest_reading(db: Session, device_code: str) -> SensorReading | None:
    return db.execute(
        select(SensorReading)
        .where(SensorReading.device_code == device_code)
        .order_by(desc(SensorReading.ts))
        .limit(1)
    ).scalar_one_or_none()


# ---------------------------------------------------------------------------
# 工具 1：平台总览
# ---------------------------------------------------------------------------
def _platform_overview(db: Session, args: dict, user: Any) -> dict:
    devices = db.execute(select(Device)).scalars().all()
    by_type: dict[str, int] = {}
    for device in devices:
        by_type[device.type] = by_type.get(device.type, 0) + 1
    buoys = [d for d in devices if d.type == "buoy"]

    since = _now() - timedelta(hours=24)
    pending_alerts = int(
        db.execute(
            select(func.count()).select_from(Alert).where(Alert.status.in_(("pending", "handling")))
        ).scalar_one()
    )
    alerts_24h = int(
        db.execute(select(func.count()).select_from(Alert).where(Alert.created_at >= since)).scalar_one()
    )
    critical_24h = int(
        db.execute(
            select(func.count())
            .select_from(Alert)
            .where(Alert.created_at >= since)
            .where(Alert.level == "critical")
        ).scalar_one()
    )

    rows = db.execute(
        select(PredictionLog).where(PredictionLog.ts >= _now() - timedelta(hours=48)).order_by(desc(PredictionLog.ts))
    ).scalars().all()
    latest: dict[str, PredictionLog] = {}
    for row in rows:
        latest.setdefault(row.device_code, row)
    distribution = {level: 0 for level in RISK_LEVELS}
    for row in latest.values():
        distribution[row.level] = distribution.get(row.level, 0) + 1
    overall_level = max((row.level for row in latest.values()), default=1)

    mangrove_rows = db.execute(select(MangroveStat).order_by(MangroveStat.region_code)).scalars().all()
    latest_mangrove: dict[str, MangroveStat] = {}
    for row in mangrove_rows:
        latest_mangrove[row.region_code] = row
    area = round(sum(r.area_ha for r in latest_mangrove.values()), 2)
    health = (
        round(sum(r.health_score * r.area_ha for r in latest_mangrove.values()) / area, 2) if area else None
    )

    readings_total = int(db.execute(select(func.count()).select_from(SensorReading)).scalar_one())

    return {
        "generated_at": _now().isoformat(timespec="seconds"),
        "device_total": len(devices),
        "device_online": sum(1 for d in devices if d.status == "online"),
        "devices_by_type": by_type,
        "buoy_total": len(buoys),
        "buoy_online": sum(1 for d in buoys if d.status == "online"),
        "pending_alerts": pending_alerts,
        "alerts_last_24h": alerts_24h,
        "critical_last_24h": critical_24h,
        "overall_risk_level": overall_level,
        "overall_risk_label": RISK_LEVELS[overall_level]["label"],
        "overall_risk_name": RISK_LEVELS[overall_level]["name"],
        "risk_distribution": [
            {"level": level, "name": RISK_LEVELS[level]["name"], "count": distribution.get(level, 0)}
            for level in sorted(RISK_LEVELS)
        ],
        "mangrove_area_ha": area,
        "mangrove_health": health,
        "readings_total": readings_total,
        "model_mode": "tree-ensemble" if _model_loaded() else "rule-based-fallback",
    }


def _model_loaded() -> bool:
    try:
        from ..ml.red_tide import get_model

        return bool(get_model().loaded)
    except Exception:  # pragma: no cover - 模型不可用时不影响总览
        return False


# ---------------------------------------------------------------------------
# 工具 2：设备清单
# ---------------------------------------------------------------------------
def _list_devices(db: Session, args: dict, user: Any) -> dict:
    device_type = args.get("device_type")
    status = args.get("status")
    rows = _match_devices(db, args.get("keyword"), limit=30)
    if device_type:
        rows = [d for d in rows if d.type == device_type]
    if status:
        rows = [d for d in rows if d.status == status]
    rows = rows[:20]

    payload = []
    for device in rows:
        latest = _latest_reading(db, device.device_code)
        payload.append(
            {
                "device_code": device.device_code,
                "name": device.name,
                "type": device.type,
                "region_code": device.region_code,
                "region_name": _region_name(device.region_code),
                "status": device.status,
                "battery": device.battery,
                "signal": device.signal,
                "depth_m": device.depth_m,
                "sample_interval_seconds": device.sample_interval,
                "last_seen": device.last_seen.isoformat(timespec="seconds") if device.last_seen else None,
                "last_reading_at": latest.ts.isoformat(timespec="seconds") if latest else None,
                "summary": (device.config or {}).get("summary"),
            }
        )
    return {
        "count": len(payload),
        "devices": payload,
        "_focus": [
            {"kind": "device", "device_code": d["device_code"], "region_code": d["region_code"],
             "label": d["name"], "level": None}
            for d in payload[:8]
        ],
    }


# ---------------------------------------------------------------------------
# 工具 3：水质现状
# ---------------------------------------------------------------------------
def _water_quality(db: Session, args: dict, user: Any) -> dict:
    keyword = args.get("device")
    hours = _clamp_hours(args.get("hours"), 24, maximum=24 * 14)
    devices = _match_devices(db, keyword, limit=6)
    devices = _prefer_sensors(devices)[:6]
    if not devices:
        return {"error": f"未找到与「{keyword}」匹配的监测设备", "hint": "可先调用设备清单工具确认设备名称或编码"}

    since = _now() - timedelta(hours=hours)
    result = []
    for device in devices:
        rows = db.execute(
            select(SensorReading)
            .where(SensorReading.device_code == device.device_code)
            .where(SensorReading.ts >= since)
            .where(SensorReading.quality_flag != 2)
            .order_by(SensorReading.ts)
        ).scalars().all()
        latest = rows[-1] if rows else None
        stats = {}
        for metric in METRICS:
            values = [float(getattr(r, metric)) for r in rows if getattr(r, metric) is not None]
            stats[metric] = {
                "label": METRICS[metric]["label"],
                "unit": METRICS[metric]["unit"],
                "avg": round(sum(values) / len(values), 2) if values else None,
                "min": round(min(values), 2) if values else None,
                "max": round(max(values), 2) if values else None,
                "latest": round(values[-1], 2) if values else None,
            }
        result.append(
            {
                "device_code": device.device_code,
                "device_name": device.name,
                "region_code": device.region_code,
                "region_name": _region_name(device.region_code),
                "status": device.status,
                "window_hours": hours,
                "sample_count": len(rows),
                "latest_at": latest.ts.isoformat(timespec="seconds") if latest else None,
                "latest_values": _reading_payload(latest),
                "window_stats": stats,
                "threshold_flags": _metric_flags(latest),
            }
        )

    return {
        "window_hours": hours,
        "devices": result,
        "_focus": [
            {"kind": "device", "device_code": d["device_code"], "region_code": d["region_code"],
             "label": d["device_name"], "level": None}
            for d in result
        ],
    }


# ---------------------------------------------------------------------------
# 工具 4：指标趋势
# ---------------------------------------------------------------------------
def _metric_trend(db: Session, args: dict, user: Any) -> dict:
    metric = str(args.get("metric") or "").strip()
    if metric not in METRICS:
        return {"error": f"未知指标「{metric}」", "allowed_metrics": list(METRICS)}
    devices = _match_devices(db, args.get("device"), limit=3)
    devices = _prefer_sensors(devices)[:3]
    if not devices:
        return {"error": f"未找到与「{args.get('device')}」匹配的监测设备"}
    hours = _clamp_hours(args.get("hours"), 48, maximum=24 * 30)
    since = _now() - timedelta(hours=hours)

    series_payload = []
    for device in devices:
        rows = db.execute(
            select(SensorReading)
            .where(SensorReading.device_code == device.device_code)
            .where(SensorReading.ts >= since)
            .where(SensorReading.quality_flag != 2)
            .order_by(SensorReading.ts)
        ).scalars().all()
        buckets: dict[datetime, list[float]] = {}
        for row in rows:
            value = getattr(row, metric, None)
            if value is None:
                continue
            key = row.ts.replace(minute=0, second=0, microsecond=0)
            buckets.setdefault(key, []).append(float(value))
        points = [
            {"ts": key.isoformat(timespec="seconds"), "value": round(sum(vals) / len(vals), 2)}
            for key, vals in sorted(buckets.items())
        ]
        if len(points) > 48:
            step = max(1, len(points) // 48)
            points = points[::step][-48:]
        values = [p["value"] for p in points]
        series_payload.append(
            {
                "device_code": device.device_code,
                "device_name": device.name,
                "region_name": _region_name(device.region_code),
                "points": points,
                "first": values[0] if values else None,
                "last": values[-1] if values else None,
                "min": min(values) if values else None,
                "max": max(values) if values else None,
                "delta": round(values[-1] - values[0], 2) if len(values) > 1 else None,
                "change_pct": (
                    round((values[-1] - values[0]) / values[0] * 100, 1)
                    if len(values) > 1 and values[0] not in (0, None)
                    else None
                ),
                "sample_count": len(rows),
            }
        )

    return {
        "metric": metric,
        "metric_label": METRICS[metric]["label"],
        "unit": METRICS[metric]["unit"],
        "window_hours": hours,
        "series": series_payload,
        "_focus": [
            {"kind": "device", "device_code": s["device_code"], "label": s["device_name"],
             "region_code": None, "level": None}
            for s in series_payload
        ],
    }


# ---------------------------------------------------------------------------
# 工具 5：告警清单
# ---------------------------------------------------------------------------
def _list_alerts(db: Session, args: dict, user: Any) -> dict:
    hours = _clamp_hours(args.get("hours"), 24, maximum=24 * 90)
    since = _now() - timedelta(hours=hours)
    stmt = select(Alert).where(Alert.created_at >= since)
    level = args.get("level")
    status = args.get("status")
    if level:
        stmt = stmt.where(Alert.level == level)
    if status:
        stmt = stmt.where(Alert.status == status)
    keyword = args.get("region")
    if keyword:
        codes = [code for code, region in REGION_BY_CODE.items() if str(keyword) in region.name]
        stmt = stmt.where(Alert.region_code.in_(codes)) if codes else stmt.where(Alert.region_code == str(keyword))
    rows = db.execute(stmt.order_by(desc(Alert.created_at)).limit(20)).scalars().all()

    counts = {"critical": 0, "warning": 0, "info": 0}
    for row in rows:
        counts[row.level] = counts.get(row.level, 0) + 1

    return {
        "window_hours": hours,
        "count": len(rows),
        "level_counts": counts,
        "active_count": sum(1 for r in rows if r.status in ("pending", "handling")),
        "alerts": [
            {
                "alert_code": row.alert_code,
                "level": row.level,
                "level_label": _ALERT_LEVEL_LABEL.get(row.level, row.level),
                "type": row.type,
                "type_label": _ALERT_TYPE_LABEL.get(row.type, row.type),
                "title": row.title,
                "content": row.content,
                "device_code": row.device_code,
                "region_name": _region_name(row.region_code),
                "metric_label": METRICS.get(row.metric or "", {}).get("label", row.metric),
                "value": row.value,
                "threshold": row.threshold,
                "status": row.status,
                "created_at": row.created_at.isoformat(timespec="seconds"),
                "suppressed_count": row.suppressed_count,
            }
            for row in rows
        ],
        "_focus": [
            {"kind": "device", "device_code": row.device_code, "region_code": row.region_code,
             "label": row.title, "level": _LEVEL_WEIGHT.get(row.level, 1)}
            for row in rows[:6]
            if row.device_code
        ],
    }


# ---------------------------------------------------------------------------
# 工具 6：赤潮风险
# ---------------------------------------------------------------------------
def _red_tide_risk(db: Session, args: dict, user: Any) -> dict:
    hours = _clamp_hours(args.get("hours"), 48, maximum=24 * 30)
    rows = db.execute(
        select(PredictionLog).where(PredictionLog.ts >= _now() - timedelta(hours=hours)).order_by(desc(PredictionLog.ts))
    ).scalars().all()
    latest: dict[str, PredictionLog] = {}
    for row in rows:
        latest.setdefault(row.device_code, row)
    if not latest:
        return {"error": "当前时间窗内暂无赤潮风险预测记录", "hint": "可先执行一次全域预测（技术员及以上）"}

    devices = {d.device_code: d for d in db.execute(select(Device)).scalars().all()}
    distribution = {level: 0 for level in RISK_LEVELS}
    region_bucket: dict[str, dict] = {}
    points = []
    for row in latest.values():
        distribution[row.level] = distribution.get(row.level, 0) + 1
        device = devices.get(row.device_code)
        code = row.region_code or "UNKNOWN"
        bucket = region_bucket.setdefault(
            code,
            {"region_code": code, "region_name": _region_name(code) or code,
             "max_level": 1, "max_probability": 0.0, "devices": 0},
        )
        bucket["devices"] += 1
        bucket["max_level"] = max(bucket["max_level"], row.level)
        bucket["max_probability"] = max(bucket["max_probability"], round(row.probability, 4))
        points.append(
            {
                "device_code": row.device_code,
                "device_name": device.name if device else row.device_code,
                "region_code": row.region_code,
                "region_name": _region_name(row.region_code),
                "probability": round(row.probability, 4),
                "probability_pct": round(row.probability * 100, 1),
                "level": row.level,
                "level_label": RISK_LEVELS[row.level]["label"],
                "level_name": RISK_LEVELS[row.level]["name"],
                "advice": RISK_LEVELS[row.level]["advice"],
                "top_factors": [f.get("label") for f in (row.top_factors or [])],
                "ts": row.ts.isoformat(timespec="seconds"),
            }
        )

    regions = sorted(region_bucket.values(), key=lambda r: (r["max_level"], r["max_probability"]), reverse=True)
    for item in regions:
        info = RISK_LEVELS[item["max_level"]]
        item["level_label"] = info["label"]
        item["level_name"] = info["name"]
        item["advice"] = info["advice"]
    points.sort(key=lambda p: (p["level"], p["probability"]), reverse=True)
    overall = max(latest.values(), key=lambda r: r.level).level

    return {
        "window_hours": hours,
        "generated_at": _now().isoformat(timespec="seconds"),
        "device_count": len(latest),
        "overall_level": overall,
        "overall_label": RISK_LEVELS[overall]["label"],
        "overall_name": RISK_LEVELS[overall]["name"],
        "distribution": [
            {"level": level, "name": RISK_LEVELS[level]["name"], "count": distribution.get(level, 0)}
            for level in sorted(RISK_LEVELS)
        ],
        "regions": regions[:10],
        "top_points": points[:10],
        "_focus": [
            {"kind": "device", "device_code": p["device_code"], "region_code": p["region_code"],
             "label": p["device_name"], "level": p["level"]}
            for p in points[:6]
        ],
    }


# ---------------------------------------------------------------------------
# 工具 7：红树林遥感
# ---------------------------------------------------------------------------
def _mangrove_status(db: Session, args: dict, user: Any) -> dict:
    rows = db.execute(select(MangroveStat).order_by(MangroveStat.region_code, MangroveStat.period)).scalars().all()
    latest: dict[str, MangroveStat] = {}
    for row in rows:
        latest[row.region_code] = row
    keyword = args.get("region")
    if keyword:
        latest = {
            code: row for code, row in latest.items()
            if str(keyword) in (_region_name(code) or "") or str(keyword) in code
        }
    if not latest:
        return {"error": "未匹配到红树林监测记录"}

    items = []
    for code, row in latest.items():
        items.append(
            {
                "region_code": code,
                "region_name": _region_name(code) or code,
                "period": row.period,
                "area_ha": row.area_ha,
                "change_pct": row.change_pct,
                "health_score": row.health_score,
                "health_level": row.health_level,
                "ndvi_mean": row.ndvi_mean,
                "ndwi_mean": row.ndwi_mean,
                "data_source": row.data_source,
                "remark": row.remark,
            }
        )
    area = round(sum(i["area_ha"] for i in items), 2)
    health = round(sum(i["health_score"] * i["area_ha"] for i in items) / area, 2) if area else None
    return {
        "period": items[0]["period"] if items else None,
        "total_area_ha": area,
        "average_health": health,
        "regions": items,
        "_focus": [],
    }


# ---------------------------------------------------------------------------
# 工具 8：处置建议草稿（动作类，落审计）
# ---------------------------------------------------------------------------
def _draft_disposal_plan(db: Session, args: dict, user: Any) -> dict:
    alert_code = str(args.get("alert_code") or "").strip()
    device_keyword = args.get("device")
    note = args.get("note") or ""

    alert = None
    if alert_code:
        alert = db.execute(select(Alert).where(Alert.alert_code == alert_code)).scalar_one_or_none()
        if alert is None:
            return {"error": f"未找到告警单号 {alert_code}"}

    device: Device | None = None
    if alert and alert.device_code:
        device = db.execute(select(Device).where(Device.device_code == alert.device_code)).scalar_one_or_none()
    elif device_keyword:
        matched = _match_devices(db, device_keyword, limit=1)
        device = matched[0] if matched else None

    if device is None and alert is None:
        return {"error": "请提供告警单号或监测设备，以便生成有依据的处置建议"}

    latest = _latest_reading(db, device.device_code) if device else None
    history = []
    if device:
        history = db.execute(
            select(Alert)
            .where(Alert.device_code == device.device_code)
            .order_by(desc(Alert.created_at))
            .limit(5)
        ).scalars().all()

    prediction = None
    if device:
        row = db.execute(
            select(PredictionLog)
            .where(PredictionLog.device_code == device.device_code)
            .order_by(desc(PredictionLog.ts))
            .limit(1)
        ).scalar_one_or_none()
        if row:
            prediction = {
                "probability_pct": round(row.probability * 100, 1),
                "level": row.level,
                "level_label": RISK_LEVELS[row.level]["label"],
                "level_name": RISK_LEVELS[row.level]["name"],
                "advice": RISK_LEVELS[row.level]["advice"],
                "top_factors": [f.get("label") for f in (row.top_factors or [])],
                "ts": row.ts.isoformat(timespec="seconds"),
            }

    actor = getattr(user, "username", None) or "anonymous"
    db.add(
        AuditLog(
            username=actor,
            action="agent_disposal_draft",
            target=alert.alert_code if alert else (device.device_code if device else "unknown"),
            detail={"note": note, "source": "agent", "requires_confirmation": True},
        )
    )
    db.commit()

    return {
        "requires_confirmation": True,
        "notice": "以下为智能体生成的处置草稿，需技术员或管理员确认后方可执行，系统不会自动改动告警状态",
        "alert": (
            {
                "alert_code": alert.alert_code,
                "level": alert.level,
                "level_label": _ALERT_LEVEL_LABEL.get(alert.level, alert.level),
                "type_label": _ALERT_TYPE_LABEL.get(alert.type, alert.type),
                "title": alert.title,
                "content": alert.content,
                "status": alert.status,
                "created_at": alert.created_at.isoformat(timespec="seconds"),
            }
            if alert
            else None
        ),
        "device": (
            {
                "device_code": device.device_code,
                "device_name": device.name,
                "region_name": _region_name(device.region_code),
                "status": device.status,
                "last_seen": device.last_seen.isoformat(timespec="seconds") if device.last_seen else None,
            }
            if device
            else None
        ),
        "latest_water_quality": _reading_payload(latest),
        "threshold_flags": _metric_flags(latest),
        "red_tide_risk": prediction,
        "recent_alerts": [
            {
                "alert_code": h.alert_code,
                "title": h.title,
                "level": h.level,
                "status": h.status,
                "handler": h.handler,
                "handle_note": h.handle_note,
                "created_at": h.created_at.isoformat(timespec="seconds"),
            }
            for h in history
        ],
        "scenario_note": note,
        "_focus": (
            [{"kind": "device", "device_code": device.device_code, "region_code": device.region_code,
              "label": device.name, "level": (prediction or {}).get("level")}]
            if device
            else []
        ),
    }


# ---------------------------------------------------------------------------
# 工具 9：执行巡检（动作类，需技术员及以上）
# ---------------------------------------------------------------------------
def _run_inspection(db: Session, args: dict, user: Any) -> dict:
    engine = AlertEngine(db)
    offline = engine.check_device_offline()
    actor = getattr(user, "username", None) or "anonymous"
    db.add(
        AuditLog(
            username=actor,
            action="agent_run_inspection",
            target="platform",
            detail={"offline_alerts": len(offline), "source": "agent"},
        )
    )
    db.commit()
    return {
        "message": "巡检完成",
        "offline_devices": len(offline),
        "created_alerts": [
            {"alert_code": a.alert_code, "device_code": a.device_code, "title": a.title, "content": a.content}
            for a in offline
        ],
        "_focus": [
            {"kind": "device", "device_code": a.device_code, "region_code": a.region_code,
             "label": a.title, "level": 2}
            for a in offline[:6]
        ],
    }


# ---------------------------------------------------------------------------
# 工具注册表
# ---------------------------------------------------------------------------
TOOLS: dict[str, ToolSpec] = {
    spec.name: spec
    for spec in (
        ToolSpec(
            name="platform_overview",
            label="平台总览",
            description="获取平台整体运行态势：设备在线率、待处理告警、赤潮风险等级分布、红树林总面积与健康度、数据总量。适用于“现在整体情况怎么样”“平台运行是否正常”一类问题。",
            parameters=_schema({}),
            handler=_platform_overview,
        ),
        ToolSpec(
            name="list_devices",
            label="设备清单",
            description="按设备类型、在线状态或名称/区域关键字查询监测设备台账（浮标、摄像头、网关、气象站、无人机）。当不确定设备编码时先用本工具确认。",
            parameters=_schema(
                {
                    "device_type": _text("设备类型：buoy(水质浮标)/camera(摄像头)/gateway(边缘网关)/weather(气象站)/uav(无人机)"),
                    "status": _text("在线状态：online/offline/fault"),
                    "keyword": _text("设备编码、设备名称或区域名称关键字"),
                }
            ),
            handler=_list_devices,
        ),
        ToolSpec(
            name="get_water_quality",
            label="水质现状",
            description="查询指定监测站点的最新水质与窗口统计（水温、pH、溶解氧、浊度、叶绿素a、盐度），并给出与《海水水质标准》阈值一致的越限标记。",
            parameters=_schema(
                {
                    "device": _text("设备编码、设备名称或区域名称关键字，例如“黄岐湾中部”"),
                    "hours": _num("统计窗口小时数，默认 24", 24),
                },
                ["device"],
            ),
            handler=_water_quality,
        ),
        ToolSpec(
            name="get_metric_trend",
            label="指标趋势",
            description="查询单站单指标的小时级趋势，返回首值、末值、极值、变化量与变化率，用于判断水质是在变好还是变差。",
            parameters=_schema(
                {
                    "device": _text("设备编码、设备名称或区域名称关键字"),
                    "metric": _text("指标键：water_temp/ph/dissolved_oxygen/turbidity/chlorophyll_a/salinity"),
                    "hours": _num("统计窗口小时数，默认 48", 48),
                },
                ["device", "metric"],
            ),
            handler=_metric_trend,
        ),
        ToolSpec(
            name="list_alerts",
            label="告警清单",
            description="查询告警记录，可按等级（info/warning/critical）、状态（pending/handling/resolved/ignored）、区域与时间窗过滤，用于了解当前有哪些异常需要处理。",
            parameters=_schema(
                {
                    "level": _text("告警等级：info/warning/critical"),
                    "status": _text("处置状态：pending/handling/resolved/ignored"),
                    "region": _text("区域名称关键字"),
                    "hours": _num("时间窗小时数，默认 24", 24),
                }
            ),
            handler=_list_alerts,
        ),
        ToolSpec(
            name="red_tide_risk",
            label="赤潮风险",
            description="获取全域赤潮风险预测：整体风险等级、风险分布、区域排行、风险最高的点位及其贡献因子与业务处置口径。",
            parameters=_schema({"hours": _num("预测时间窗小时数，默认 48", 48)}),
            handler=_red_tide_risk,
        ),
        ToolSpec(
            name="mangrove_status",
            label="红树林遥感",
            description="获取红树林遥感监测统计：各区域面积、面积变化率、健康评分、NDVI/NDWI 均值与数据来源。",
            parameters=_schema({"region": _text("区域名称关键字，缺省返回全部")}),
            handler=_mangrove_status,
        ),
        ToolSpec(
            name="draft_disposal_plan",
            label="处置建议草稿",
            description="针对某条告警或某个监测站点，汇总现状数据、阈值越限、赤潮风险与历史处置记录，生成处置建议草稿。只产出草稿并记录审计，不会自动修改告警状态。",
            parameters=_schema(
                {
                    "alert_code": _text("告警单号，例如 AL2026100600012"),
                    "device": _text("设备编码、名称或区域关键字，与告警单号二选一"),
                    "note": _text("补充说明的处置场景"),
                }
            ),
            handler=_draft_disposal_plan,
            kind="action",
        ),
        ToolSpec(
            name="run_inspection",
            label="执行巡检",
            description="立即执行一次全量设备巡检（离线检测并生成设备异常告警）。属于写操作，仅技术员与管理员可调用。",
            parameters=_schema({}),
            handler=_run_inspection,
            min_role="technician",
            kind="action",
        ),
    )
}

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.parameters,
        },
    }
    for spec in TOOLS.values()
]

SYSTEM_PROMPT = """你是「深蓝智护·连江海洋生态智能监测平台」的海洋生态智能体，服务对象是连江县海洋牧场养殖户、监测技术员与渔业管理部门。

工作要求：
1. 事实必须来自工具。凡涉及具体数值、设备、区域、告警、风险等级、红树林面积的问题，先调用相应工具取数，严禁凭记忆或推测编造。
2. 不确定设备名称或编码时，先用 list_devices 确认，再查询细节。
3. 回答使用简体中文，先给结论，再给关键依据，最后给可执行建议；控制在 300 字以内。
4. 引用数据时标注来源口径与时间，例如「黄岐湾中部浮标（LJ-HQ-B02-B）最新读数」「未来 48 小时预测」。
5. 涉及风险处置时，说明对应风险等级的处置口径，并提示处置动作需人工确认。
6. 不输出函数名、参数、JSON 等内部细节，用业务语言描述你查了什么。
7. 数据不足或工具返回 error 时，如实说明缺少哪类数据，不要编造替代值。"""


# ---------------------------------------------------------------------------
# 智能体主循环
# ---------------------------------------------------------------------------
def _role_weight(user: Any) -> int:
    role = getattr(user, "role", None) or "viewer"
    return _ROLE_WEIGHT.get(role, 1)


def _call_llm(messages: list[dict], tools: list[dict] | None) -> dict:
    payload: dict[str, Any] = {
        "model": settings.zhipu_model,
        "messages": messages,
        "stream": False,
        "temperature": 0.3,
        "max_tokens": 1500,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    with httpx.Client(timeout=settings.agent_timeout_seconds) as client:
        response = client.post(
            f"{settings.zhipu_base_url.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.zhipu_api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        response.raise_for_status()
        return response.json()


def _execute_tool(db: Session, name: str, raw_args: str, user: Any) -> tuple[dict, list[dict], bool]:
    spec = TOOLS.get(name)
    if spec is None:
        return {"error": f"未注册的工具 {name}"}, [], False
    if _role_weight(user) < _ROLE_WEIGHT.get(spec.min_role, 1):
        return (
            {"error": f"当前账号角色无权执行「{spec.label}」，需要 {spec.min_role} 及以上权限"},
            [],
            False,
        )
    try:
        args = json.loads(raw_args) if isinstance(raw_args, str) and raw_args.strip() else {}
    except json.JSONDecodeError:
        return {"error": f"工具参数不是合法 JSON：{raw_args}"}, [], False
    if not isinstance(args, dict):
        return {"error": "工具参数必须是对象"}, [], False

    try:
        result = spec.handler(db, args, user)
    except Exception as exc:  # pragma: no cover - 工具异常不应中断对话
        logger.exception("智能体工具 %s 执行失败", name)
        return {"error": f"工具执行失败：{exc}"}, [], False

    if not isinstance(result, dict):
        result = {"result": result}
    focus = result.pop("_focus", []) or []
    ok = "error" not in result
    return result, focus, ok


def _summarize(label: str, result: dict) -> str:
    if "error" in result:
        return f"{label}未取得数据：{result['error']}"
    if "count" in result:
        return f"{label}返回 {result['count']} 条记录"
    if "devices" in result:
        return f"{label}返回 {len(result['devices'])} 台设备"
    if "top_points" in result:
        return f"{label}覆盖 {result.get('device_count', 0)} 个点位，整体等级 {result.get('overall_level')}"
    if "total_area_ha" in result:
        return f"{label}合计 {result['total_area_ha']} 公顷"
    if "message" in result:
        return f"{label}：{result['message']}"
    return f"{label}已返回数据"


def _run_agent(
    db: Session,
    question: str,
    user: Any = None,
    history: list[dict] | None = None,
    on_event: Callable[[dict], None] | None = None,
) -> dict:
    emit = on_event or (lambda _event: None)

    if not settings.agent_enabled or not settings.zhipu_api_key:
        return _fallback(db, question, user, reason="智能体未启用或未配置模型密钥", emit=emit)

    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    for item in (history or [])[-6:]:
        role = item.get("role")
        content = (item.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content[:1000]})
    messages.append({"role": "user", "content": question})

    emit({"type": "status", "stage": "understand", "text": "正在理解问题并结合平台数据研判…"})

    trace: list[dict] = []
    focus: list[dict] = []
    usage: dict[str, Any] = {}
    answer = ""
    rounds = 0

    for round_index in range(max(1, settings.agent_max_rounds)):
        rounds = round_index + 1
        try:
            data = _call_llm(messages, TOOL_SCHEMAS)
        except Exception as exc:
            logger.warning("大模型调用失败：%s", exc)
            return _fallback(db, question, user, reason=f"大模型暂不可用（{type(exc).__name__}）", emit=emit, trace=trace)

        usage = data.get("usage") or usage
        choices = data.get("choices") or []
        if not choices:
            return _fallback(db, question, user, reason="大模型未返回结果", emit=emit, trace=trace)
        message = choices[0].get("message") or {}
        tool_calls = message.get("tool_calls") or []

        if not tool_calls:
            answer = (message.get("content") or "").strip()
            break

        messages.append(
            {
                "role": "assistant",
                "content": message.get("content") or "",
                "tool_calls": tool_calls,
            }
        )
        for call in tool_calls:
            function = call.get("function") or {}
            name = function.get("name") or ""
            raw_args = function.get("arguments") or "{}"
            spec = TOOLS.get(name)
            label = spec.label if spec else name
            emit(
                {
                    "type": "tool",
                    "name": name,
                    "label": label,
                    "arguments": _safe_args(raw_args),
                    "kind": spec.kind if spec else "read",
                }
            )
            result, hits, ok = _execute_tool(db, name, raw_args, user)
            focus.extend(hits)
            summary = _summarize(label, result)
            trace.append(
                {
                    "name": name,
                    "label": label,
                    "arguments": _safe_args(raw_args),
                    "summary": summary,
                    "ok": ok,
                    "kind": spec.kind if spec else "read",
                }
            )
            emit({"type": "tool_result", "name": name, "label": label, "summary": summary, "ok": ok})
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id") or name,
                    "content": json.dumps(result, ensure_ascii=False)[:6000],
                }
            )
    else:
        try:
            data = _call_llm(messages, None)
            usage = data.get("usage") or usage
            choices = data.get("choices") or []
            answer = ((choices[0].get("message") or {}).get("content") or "").strip() if choices else ""
        except Exception as exc:  # pragma: no cover
            logger.warning("大模型收尾调用失败：%s", exc)

    if not answer:
        return _fallback(db, question, user, reason="大模型未给出最终结论", emit=emit, trace=trace)

    focus = _dedupe_focus(focus)
    emit({"type": "answer", "text": answer})
    emit({"type": "focus", "focus": focus})
    return {
        "answer": answer,
        "tools": trace,
        "focus": focus,
        "model": settings.zhipu_model,
        "provider": "zhipu",
        "rounds": rounds,
        "usage": usage,
        "degraded": False,
    }


def _safe_args(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw) if raw else {}
    except (json.JSONDecodeError, TypeError):
        return {"_raw": str(raw)[:200]}
    return value if isinstance(value, dict) else {"_value": value}


def _dedupe_focus(items: list[dict]) -> list[dict]:
    seen: set[str] = set()
    result: list[dict] = []
    for item in items:
        key = item.get("device_code") or f"region:{item.get('region_code')}"
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(item)
        if len(result) >= 10:
            break
    return result


# ---------------------------------------------------------------------------
# 降级：本地规则编排
# ---------------------------------------------------------------------------
_FALLBACK_RULES: tuple[tuple[tuple[str, ...], str, dict], ...] = (
    (("红树", "湿地", "ndvi", "面积变化"), "mangrove_status", {}),
    (("赤潮", "藻", "风险", "预警等级", "概率"), "red_tide_risk", {"hours": 48}),
    (("告警", "异常", "处置", "报警", "越限"), "list_alerts", {"hours": 24}),
    (("设备", "在线", "离线", "浮标", "电量", "信号"), "list_devices", {}),
)


def _fallback(
    db: Session,
    question: str,
    user: Any,
    *,
    reason: str,
    emit: Callable[[dict], None],
    trace: list[dict] | None = None,
) -> dict:
    trace = list(trace or [])
    collected: dict[str, dict] = {}
    focus: list[dict] = []
    sections: list[str] = []

    def call(name: str, args: dict) -> dict | None:
        spec = TOOLS[name]
        emit({"type": "tool", "name": name, "label": spec.label, "arguments": args, "kind": "read"})
        result, hits, ok = _execute_tool(db, name, json.dumps(args, ensure_ascii=False), user)
        focus.extend(hits)
        summary = _summarize(spec.label, result)
        trace.append({"name": name, "label": spec.label, "arguments": args, "summary": summary, "ok": ok, "kind": "read"})
        emit({"type": "tool_result", "name": name, "label": spec.label, "summary": summary, "ok": ok})
        collected[name] = result
        return result

    matched = _match_devices(db, _guess_device_keyword(question), limit=3)
    keyword = _guess_device_keyword(question)
    matched = _match_devices(db, keyword, limit=3) if keyword else []
    if matched:
        call("get_water_quality", {"device": matched[0].device_code, "hours": 24})

    for keywords, tool_name, args in _FALLBACK_RULES:
        if any(word in question for word in keywords):
            call(tool_name, dict(args))

    overview = call("platform_overview", {})

    if overview and "error" not in overview:
        lines = [
            f"设备在线 {overview.get('device_online', 0)}/{overview.get('device_total', 0)} 台，"
            f"待处理告警 {overview.get('pending_alerts', 0)} 条，"
            f"全域赤潮风险 {overview.get('overall_risk_label')}（{overview.get('overall_risk_name')}），"
            f"红树林面积 {overview.get('mangrove_area_ha')} 公顷、健康度 {overview.get('mangrove_health')}。"
        ]
        sections.append("".join(lines))

    sections.extend(_fallback_lines(collected))

    sections.append(f"（说明：{reason}，以上由平台规则编排直接汇总数据库结果，未经过大模型润色。）")
    answer = "\n".join(sections)
    focus = _dedupe_focus(focus)
    emit({"type": "answer", "text": answer})
    emit({"type": "focus", "focus": focus})
    return {
        "answer": answer,
        "tools": trace,
        "focus": focus,
        "model": "rule-orchestrator",
        "provider": "local-fallback",
        "rounds": 0,
        "usage": {},
        "degraded": True,
        "degraded_reason": reason,
    }


def _fallback_lines(collected: dict[str, dict]) -> list[str]:
    """降级模式下用真实取数结果拼出可读结论，不引入任何推测值。"""

    lines: list[str] = []
    water = collected.get("get_water_quality")
    if water and water.get("devices"):
        for device in water["devices"][:1]:
            values = device.get("latest_values") or {}
            detail = "、".join(
                f"{METRICS[key]['label']} {values[key]}{METRICS[key]['unit']}"
                for key in METRICS
                if values.get(key) is not None
            )
            flags = device.get("threshold_flags") or []
            flag_text = f"，越限 {len(flags)} 项" if flags else "，指标均未越限"
            lines.append(f"· {device['device_name']}（{device['device_code']}）最新水质：{detail}{flag_text}")

    risk = collected.get("red_tide_risk")
    if risk and risk.get("top_points"):
        top = risk["top_points"][0]
        lines.append(
            f"· 赤潮风险：全域 {risk.get('overall_label')}{risk.get('overall_name')}，"
            f"最高点位 {top['device_name']}（{top['device_code']}）概率 {top['probability_pct']}%"
        )

    alerts = collected.get("list_alerts")
    if alerts and alerts.get("alerts"):
        lines.append(f"· 告警：窗口内 {alerts['count']} 条，其中待处置 {alerts.get('active_count', 0)} 条")
        for item in alerts["alerts"][:3]:
            lines.append(f"   - [{item['level_label']}] {item['title']}｜{item['content'][:60]}")

    mangrove = collected.get("mangrove_status")
    if mangrove and mangrove.get("total_area_ha") is not None:
        lines.append(
            f"· 红树林：{mangrove['total_area_ha']} 公顷，平均健康度 {mangrove.get('average_health')}"
        )

    trend = collected.get("get_metric_trend")
    if trend and trend.get("series"):
        item = trend["series"][0]
        unit = trend.get("unit") or ""
        lines.append(
            f"· {item['device_name']} {trend['metric_label']}趋势："
            f"{item.get('first')} → {item.get('last')}{unit}（变化 {item.get('delta')}）"
        )
    return lines


def _guess_device_keyword(question: str) -> str | None:
    for device in _KNOWN_NAMES:
        if device in question:
            return device
    for region in REGION_BY_CODE.values():
        if region.name in question:
            return region.name
    for token in ("黄岐", "官坞", "定海", "苔菉", "东洛", "马鼻", "浦口", "鉴江", "松山", "浮曦", "敖江"):
        if token in question:
            return token
    return None


_KNOWN_NAMES = (
    "黄湾屿", "黄岐湾", "官坞", "定海湾", "苔菉", "北茭", "东洛岛", "马鼻", "浮曦", "松坞", "敖江", "鉴江", "松山",
)


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------
def chat(db: Session, question: str, user: Any = None, history: list[dict] | None = None) -> dict:
    """非流式问答：一次性返回结论、工具轨迹与高亮点位。"""

    result = _run_agent(db, question, user, history)
    _audit(db, question, result, user)
    return result


def chat_stream(db: Session, question: str, user: Any = None, history: list[dict] | None = None) -> Iterator[dict]:
    """流式问答：逐条产出事件（工具调用过程 + 最终结论 + 高亮点位）。"""

    events: list[dict] = []
    yield {"type": "status", "stage": "start", "text": "智能体已接收问题"}
    result = _run_agent(db, question, user, history, on_event=events.append)

    # _run_agent 内部以回调收集事件；此处按顺序回放，保证前端能看到工具调用过程
    emitted_answer = False
    for event in events:
        if event.get("type") == "answer":
            emitted_answer = True
        yield event
    if not emitted_answer:
        yield {"type": "answer", "text": result.get("answer", "")}
    yield {
        "type": "done",
        "degraded": result.get("degraded", False),
        "model": result.get("model"),
        "provider": result.get("provider"),
        "tools": result.get("tools", []),
        "focus": result.get("focus", []),
        "usage": result.get("usage", {}),
    }
    _audit(db, question, result, user)


def _audit(db: Session, question: str, result: dict, user: Any) -> None:
    try:
        db.add(
            AuditLog(
                username=getattr(user, "username", None) or "anonymous",
                action="agent_chat",
                target="agent",
                detail={
                    "question": question[:200],
                    "tools": [t.get("name") for t in result.get("tools", [])],
                    "focus": [f.get("device_code") for f in result.get("focus", []) if f.get("device_code")],
                    "model": result.get("model"),
                    "degraded": result.get("degraded", False),
                },
            )
        )
        db.commit()
    except Exception:  # pragma: no cover - 审计失败不应影响问答
        logger.exception("智能体审计写入失败")
        db.rollback()


def status_payload(db: Session) -> dict:
    """智能体运行状态：供前端展示与自检使用。"""

    configured = bool(settings.zhipu_api_key)
    reachable = False
    detail = None
    if configured and settings.agent_enabled:
        try:
            with httpx.Client(timeout=8) as client:
                response = client.get(
                    f"{settings.zhipu_base_url.rstrip('/')}/models",
                    headers={"Authorization": f"Bearer {settings.zhipu_api_key}"},
                )
                reachable = response.status_code < 500
                detail = f"HTTP {response.status_code}"
        except Exception as exc:  # pragma: no cover - 探测失败按不可达处理
            detail = f"{type(exc).__name__}"

    chat_count = int(
        db.execute(select(func.count()).select_from(AuditLog).where(AuditLog.action == "agent_chat")).scalar_one()
    )
    draft_count = int(
        db.execute(
            select(func.count()).select_from(AuditLog).where(AuditLog.action == "agent_disposal_draft")
        ).scalar_one()
    )
    return {
        "enabled": settings.agent_enabled,
        "configured": configured,
        "reachable": reachable,
        "probe": detail,
        "provider": "智谱 AI（BigModel）",
        "model": settings.zhipu_model,
        "base_url": settings.zhipu_base_url,
        "max_rounds": settings.agent_max_rounds,
        "tool_count": len(TOOLS),
        "tools": [
            {"name": spec.name, "label": spec.label, "kind": spec.kind, "min_role": spec.min_role,
             "description": spec.description}
            for spec in TOOLS.values()
        ],
        "stats": {"chat_total": chat_count, "disposal_draft_total": draft_count},
    }
