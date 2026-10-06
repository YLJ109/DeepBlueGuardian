"""告警引擎：阈值规则 + 趋势规则 + 设备心跳规则。"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from ..config import ALERT_THRESHOLDS, METRICS, TREND_RULES, settings
from ..models import Alert, Device, NotificationLog, SensorReading
from .notifier import notify_alert

logger = logging.getLogger("deepblue.alerts")

_LEVEL_WEIGHT = {"info": 1, "warning": 2, "critical": 3}
_OPS = {
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
}


class AlertEngine:
    """评估单条读数并生成告警。"""

    def __init__(self, db: Session) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------
    def evaluate_reading(self, device: Device, reading: SensorReading) -> list[Alert]:
        if reading.quality_flag == 2:
            return []
        created: list[Alert] = []
        created.extend(self._check_thresholds(device, reading))
        created.extend(self._check_trends(device, reading))
        return created

    # ------------------------------------------------------------------
    # 阈值规则
    # ------------------------------------------------------------------
    def _check_thresholds(self, device: Device, reading: SensorReading) -> list[Alert]:
        created: list[Alert] = []
        for metric, rules in ALERT_THRESHOLDS.items():
            value = getattr(reading, metric, None)
            if value is None:
                continue
            for rule in sorted(rules, key=lambda r: _LEVEL_WEIGHT[r["level"]], reverse=True):
                if not _OPS[rule["op"]](value, rule["value"]):
                    continue
                alert = self._create_alert(
                    device=device,
                    level=rule["level"],
                    alert_type="threshold",
                    title=f"{_metric_label(metric)}{'超限' if rule['op'].startswith('>') else '过低'}",
                    content=f"{device.name}（{device.device_code}）{rule['label']}，当前实测值 {value:.2f}",
                    metric=metric,
                    value=value,
                    threshold=rule["value"],
                    dedup_key=f"threshold:{device.device_code}:{metric}:{rule['level']}",
                )
                if alert:
                    created.append(alert)
                break  # 同一指标只报最高等级
        return created

    # ------------------------------------------------------------------
    # 趋势规则
    # ------------------------------------------------------------------
    def _check_trends(self, device: Device, reading: SensorReading) -> list[Alert]:
        created: list[Alert] = []
        for rule in TREND_RULES:
            metric = rule["metric"]
            current = getattr(reading, metric, None)
            if current is None:
                continue
            since = reading.ts - timedelta(hours=rule["window_hours"])
            past = self.db.execute(
                select(getattr(SensorReading, metric))
                .where(SensorReading.device_code == device.device_code)
                .where(SensorReading.ts >= since)
                .where(SensorReading.ts < reading.ts)
                .where(SensorReading.quality_flag != 2)
                .order_by(SensorReading.ts)
                .limit(1)
            ).scalar_one_or_none()
            if past is None:
                continue
            delta = current - past
            triggered = (rule["delta"] > 0 and delta >= rule["delta"]) or (rule["delta"] < 0 and delta <= rule["delta"])
            if not triggered:
                continue
            alert = self._create_alert(
                device=device,
                level=rule["level"],
                alert_type="trend",
                title=f"{_metric_label(metric)}异常{'上升' if delta > 0 else '下降'}",
                content=f"{device.name}（{device.device_code}）{rule['label']}，实测变化 {delta:+.2f}",
                metric=metric,
                value=current,
                threshold=rule["delta"],
                dedup_key=f"trend:{device.device_code}:{metric}:{rule['window_hours']}",
            )
            if alert:
                created.append(alert)
        return created

    # ------------------------------------------------------------------
    # 模型告警（由预测服务调用）
    # ------------------------------------------------------------------
    def raise_model_alert(self, device: Device | None, payload: dict[str, Any]) -> Alert | None:
        if device is None or payload.get("level", 1) < 3:
            return None
        level_name = payload.get("level_label", "Ⅲ级")
        factors = "、".join(f["label"] for f in (payload.get("top_factors") or [])[:2]) or "叶绿素a、水温"
        return self._create_alert(
            device=device,
            level="critical" if payload.get("level", 3) >= 4 else "warning",
            alert_type="model",
            title=f"赤潮风险预警（{level_name}）",
            content=(
                f"{device.name} 未来 {payload.get('horizon_hours', 48)} 小时赤潮风险概率 "
                f"{payload.get('probability', 0) * 100:.1f}%，主要贡献因子：{factors}"
            ),
            metric="chlorophyll_a",
            value=payload.get("probability"),
            threshold=0.5,
            dedup_key=f"model:{device.device_code}:{payload.get('horizon_hours', 48)}",
            extra={"factors": payload.get("top_factors") or [], "model_version": payload.get("model_version")},
        )

    # ------------------------------------------------------------------
    # 设备离线
    # ------------------------------------------------------------------
    def raise_vision_alert(self, device: Device, payload: dict[str, Any]) -> Alert | None:
        """图像识别告警：由视觉识别结果触发，纳入同一套去重、通知与处置链路。"""

        label = payload.get("label", "目标")
        confidence = float(payload.get("confidence") or 0.0)
        advice = payload.get("advice") or "建议人工复核识别结果。"
        note = (payload.get("note") or "").strip()
        content = (
            f"{device.name}（{device.device_code}）AI 视频巡视识别到{label}，"
            f"置信度 {confidence * 100:.1f}%。{advice}"
        )
        if note:
            content += f" 处置备注：{note}"
        return self._create_alert(
            device=device,
            level=payload.get("level", "warning"),
            alert_type="vision",
            title=payload.get("title") or f"视觉识别：{label}",
            content=content,
            dedup_key=f"vision:{device.device_code}:{payload.get('category') or label}",
            metric="vision_confidence",
            value=confidence,
            extra={
                "label": label,
                "category": payload.get("category"),
                "confidence": confidence,
                "note": note,
                **(payload.get("extra") or {}),
            },
        )

    def check_device_offline(self, now: datetime | None = None) -> list[Alert]:
        now = now or datetime.now()
        created: list[Alert] = []
        devices = self.db.execute(select(Device)).scalars().all()
        for device in devices:
            if device.last_seen is None:
                continue
            silent = (now - device.last_seen).total_seconds()
            if silent > 1800 and device.status != "offline":
                device.status = "offline"
                alert = self._create_alert(
                    device=device,
                    level="warning",
                    alert_type="device",
                    title="设备离线",
                    content=f"{device.name}（{device.device_code}）已失联 {int(silent / 60)} 分钟，请检查供电与通信链路",
                    dedup_key=f"device:{device.device_code}:offline",
                )
                if alert:
                    created.append(alert)
        self.db.commit()
        return created

    # ------------------------------------------------------------------
    # 内部：建告警（带去重与抑制）
    # ------------------------------------------------------------------
    def _create_alert(
        self,
        *,
        device: Device | None,
        level: str,
        alert_type: str,
        title: str,
        content: str,
        dedup_key: str,
        metric: str | None = None,
        value: float | None = None,
        threshold: float | None = None,
        extra: dict[str, Any] | None = None,
    ) -> Alert | None:
        since = datetime.now() - timedelta(minutes=settings.alert_suppress_minutes)
        existing = self.db.execute(
            select(Alert)
            .where(Alert.dedup_key == dedup_key)
            .where(Alert.created_at >= since)
            .where(Alert.status != "resolved")
            .order_by(desc(Alert.created_at))
            .limit(1)
        ).scalar_one_or_none()
        if existing is not None:
            existing.suppressed_count += 1
            existing.content = content
            if value is not None:
                existing.value = value
            self.db.commit()
            return None

        seq = self.db.execute(select(Alert.id).order_by(desc(Alert.id)).limit(1)).scalar_one_or_none() or 0
        alert = Alert(
            alert_code=f"AL{datetime.now():%Y%m%d}{seq + 1:05d}",
            device_code=device.device_code if device else None,
            region_code=device.region_code if device else None,
            level=level,
            type=alert_type,
            title=title,
            content=content,
            metric=metric,
            value=value,
            threshold=threshold,
            dedup_key=dedup_key,
            extra=extra or {},
        )
        self.db.add(alert)
        self.db.commit()
        self.db.refresh(alert)
        self._notify(alert, device)
        return alert

    def _notify(self, alert: Alert, device: Device | None) -> None:
        payload = {
            "id": alert.id,
            "alert_code": alert.alert_code,
            "device_code": alert.device_code,
            "device_name": device.name if device else None,
            "region_code": alert.region_code,
            "level": alert.level,
            "type": alert.type,
            "title": alert.title,
            "content": alert.content,
            "metric": alert.metric,
            "value": alert.value,
            "threshold": alert.threshold,
            "created_at": alert.created_at.isoformat(timespec="seconds"),
        }
        for result in notify_alert(payload):
            self.db.add(
                NotificationLog(
                    alert_id=alert.id,
                    channel=result["channel"],
                    target=settings.sms_webhook or None,
                    status=result["status"],
                    detail=alert.title,
                )
            )
        self.db.commit()


def _metric_label(metric: str) -> str:
    return METRICS.get(metric, {}).get("label", metric)
