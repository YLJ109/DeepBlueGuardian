"""告警接口：列表、统计、处置闭环、推送记录、离线巡检。"""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException
from sqlalchemy import func, select

from ..config import ALERT_THRESHOLDS, METRICS, TREND_RULES
from ..data.sites import REGION_BY_CODE
from ..deps import DbSession, OperatorUser, TechnicianUser
from ..models import Alert, AuditLog, Device, NotificationLog
from ..schemas import AlertHandle, AlertOut
from ..services.alerts import AlertEngine
from ..services.notifier import bus

router = APIRouter(prefix="/alerts", tags=["告警管理"])

LEVEL_LABEL = {"info": "提示", "warning": "警告", "critical": "严重"}
TYPE_LABEL = {"threshold": "阈值越限", "trend": "趋势异常", "model": "模型预警", "device": "设备异常"}


def _payload(alert: Alert) -> dict:
    data = AlertOut.model_validate(alert).model_dump(mode="json")
    data["level_label"] = LEVEL_LABEL.get(alert.level, alert.level)
    data["type_label"] = TYPE_LABEL.get(alert.type, alert.type)
    data["metric_label"] = METRICS.get(alert.metric or "", {}).get("label", alert.metric)
    region = REGION_BY_CODE.get(alert.region_code or "")
    device = None
    data["region_name"] = region.name if region else None
    return data


@router.get("", summary="告警列表")
def list_alerts(
    db: DbSession,
    status: str | None = None,
    level: str | None = None,
    type: str | None = None,
    region_code: str | None = None,
    hours: int = 72,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    since = datetime.now() - timedelta(hours=max(1, min(hours, 24 * 365)))
    stmt = select(Alert).where(Alert.created_at >= since)
    if status:
        stmt = stmt.where(Alert.status == status)
    if level:
        stmt = stmt.where(Alert.level == level)
    if type:
        stmt = stmt.where(Alert.type == type)
    if region_code:
        stmt = stmt.where(Alert.region_code == region_code)

    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = db.execute(stmt.order_by(Alert.created_at.desc()).limit(min(limit, 500)).offset(offset)).scalars().all()
    return {"code": 0, "total": int(total), "data": [_payload(a) for a in rows]}


@router.get("/stats", summary="告警统计（大屏使用）")
def alert_stats(db: DbSession, hours: int = 24) -> dict:
    since = datetime.now() - timedelta(hours=max(1, min(hours, 24 * 30)))
    rows = db.execute(select(Alert).where(Alert.created_at >= since)).scalars().all()

    by_level = {k: 0 for k in LEVEL_LABEL}
    by_type = {k: 0 for k in TYPE_LABEL}
    by_region: dict[str, int] = {}
    by_hour: dict[str, int] = {}
    pending = 0
    for alert in rows:
        by_level[alert.level] = by_level.get(alert.level, 0) + 1
        by_type[alert.type] = by_type.get(alert.type, 0) + 1
        key = alert.region_code or "UNKNOWN"
        by_region[key] = by_region.get(key, 0) + 1
        bucket = alert.created_at.strftime("%m-%d %H:00")
        by_hour[bucket] = by_hour.get(bucket, 0) + 1
        if alert.status in ("pending", "handling"):
            pending += 1

    recent = db.execute(select(Alert).order_by(Alert.created_at.desc()).limit(12)).scalars().all()
    return {
        "code": 0,
        "data": {
            "window_hours": hours,
            "total": len(rows),
            "pending": pending,
            "resolved": sum(1 for a in rows if a.status == "resolved"),
            "by_level": by_level,
            "by_type": by_type,
            "by_region": [
                {
                    "region_code": code,
                    "region_name": REGION_BY_CODE[code].name if code in REGION_BY_CODE else code,
                    "count": count,
                }
                for code, count in sorted(by_region.items(), key=lambda kv: kv[1], reverse=True)
            ],
            "hourly": [{"bucket": k, "count": v} for k, v in sorted(by_hour.items())[-24:]],
            "recent": [_payload(a) for a in recent],
            "rules": {
                "threshold": [
                    {
                        "metric": metric,
                        "metric_label": METRICS[metric]["label"],
                        "unit": METRICS[metric]["unit"],
                        "conditions": rules,
                    }
                    for metric, rules in ALERT_THRESHOLDS.items()
                ],
                "trend": [
                    {
                        **rule,
                        "metric_label": METRICS[rule["metric"]]["label"],
                        "unit": METRICS[rule["metric"]]["unit"],
                    }
                    for rule in TREND_RULES
                ],
            },
        },
    }


@router.post("/{alert_id}/handle", summary="处置告警")
def handle_alert(
    alert_id: int,
    payload: AlertHandle,
    db: DbSession,
    user: OperatorUser,
) -> dict:
    alert = db.get(Alert, alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="告警不存在")
    alert.status = payload.status
    alert.handler = user.display_name
    alert.handle_note = payload.note
    alert.handled_at = datetime.now().replace(microsecond=0)
    db.add(AuditLog(username=user.username, action="handle_alert", target=alert.alert_code,
                    detail={"status": payload.status, "note": payload.note}))
    db.commit()
    db.refresh(alert)
    bus.broadcast("alert_update", _payload(alert))
    return {"code": 0, "message": "处置信息已记录", "data": _payload(alert)}


@router.get("/{alert_id}/notifications", summary="告警推送记录")
def notifications(alert_id: int, db: DbSession) -> dict:
    rows = db.execute(
        select(NotificationLog).where(NotificationLog.alert_id == alert_id).order_by(NotificationLog.id)
    ).scalars().all()
    return {
        "code": 0,
        "data": [
            {
                "id": r.id,
                "channel": r.channel,
                "target": r.target,
                "status": r.status,
                "detail": r.detail,
                "created_at": r.created_at.isoformat(timespec="seconds"),
            }
            for r in rows
        ],
    }


@router.post("/scan", summary="执行一次全量巡检（离线检测 + 规则复评）")
def scan(db: DbSession, _: TechnicianUser) -> dict:
    engine = AlertEngine(db)
    offline = engine.check_device_offline()
    latest = db.execute(select(Alert).order_by(Alert.id.desc()).limit(1)).scalar_one_or_none()
    return {
        "code": 0,
        "message": "巡检完成",
        "offline_alerts": len(offline),
        "latest_alert": _payload(latest) if latest else None,
    }
