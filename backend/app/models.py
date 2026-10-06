"""ORM 数据模型。

表结构围绕「设备 → 数据 → 告警 → 预测 → 配置」主链路设计，
可在原型阶段使用 SQLite，数据量增长后可平滑迁移至 PostgreSQL/TimescaleDB。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


class Region(Base):
    """监测区域（海域/片区）。"""

    __tablename__ = "region"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    parent_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    center_lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    center_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 三维沙盘坐标（归一化世界坐标，前端直接使用）
    world_x: Mapped[float | None] = mapped_column(Float, nullable=True)
    world_z: Mapped[float | None] = mapped_column(Float, nullable=True)
    kind: Mapped[str] = mapped_column(String(16), default="bay")  # bay/farm/mangrove/estuary
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)


class Device(Base):
    """监测设备：水质浮标、摄像头、边缘网关、气象站、无人机。"""

    __tablename__ = "device"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_code: Mapped[str] = mapped_column(String(48), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    type: Mapped[str] = mapped_column(String(16), index=True)  # buoy/camera/gateway/weather/uav
    region_code: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    world_x: Mapped[float | None] = mapped_column(Float, nullable=True)
    world_z: Mapped[float | None] = mapped_column(Float, nullable=True)

    status: Mapped[str] = mapped_column(String(16), default="online", index=True)  # online/offline/fault
    battery: Mapped[float | None] = mapped_column(Float, default=100.0)
    signal: Mapped[int | None] = mapped_column(Integer, default=-70)
    solar_voltage: Mapped[float | None] = mapped_column(Float, nullable=True)

    firmware: Mapped[str | None] = mapped_column(String(32), default="v1.0.0")
    sample_interval: Mapped[int] = mapped_column(Integer, default=300)  # 秒
    depth_m: Mapped[float | None] = mapped_column(Float, default=0.5)
    config: Mapped[dict | None] = mapped_column(JSON, default=dict)
    device_key: Mapped[str | None] = mapped_column(String(64), nullable=True)

    installed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    readings: Mapped[list["SensorReading"]] = relationship(back_populates="device", cascade="all, delete-orphan")


class SensorReading(Base):
    """水质原始观测数据。"""

    __tablename__ = "sensor_reading"
    __table_args__ = (
        Index("ix_reading_device_ts", "device_code", "ts"),
        UniqueConstraint("device_code", "ts", name="uq_reading_device_ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_code: Mapped[str] = mapped_column(String(48), ForeignKey("device.device_code", ondelete="CASCADE"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, index=True)

    water_temp: Mapped[float | None] = mapped_column(Float, nullable=True)
    ph: Mapped[float | None] = mapped_column(Float, nullable=True)
    dissolved_oxygen: Mapped[float | None] = mapped_column(Float, nullable=True)
    turbidity: Mapped[float | None] = mapped_column(Float, nullable=True)
    chlorophyll_a: Mapped[float | None] = mapped_column(Float, nullable=True)
    salinity: Mapped[float | None] = mapped_column(Float, nullable=True)

    quality_flag: Mapped[int] = mapped_column(Integer, default=0)  # 0 正常 1 可疑 2 无效
    battery: Mapped[float | None] = mapped_column(Float, nullable=True)
    signal: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="edge")  # edge/cloud/manual
    received_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    device: Mapped[Device] = relationship(back_populates="readings")


class MetricAggregate(Base):
    """聚合数据：1 分钟 / 1 小时 / 1 天，供大屏与报表快速查询。"""

    __tablename__ = "metric_aggregate"
    __table_args__ = (
        UniqueConstraint("device_code", "metric", "granularity", "bucket", name="uq_agg_unique"),
        Index("ix_agg_lookup", "device_code", "metric", "granularity", "bucket"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_code: Mapped[str] = mapped_column(String(48), index=True)
    metric: Mapped[str] = mapped_column(String(32), index=True)
    granularity: Mapped[str] = mapped_column(String(8))  # 1m/1h/1d
    bucket: Mapped[datetime] = mapped_column(DateTime, index=True)
    avg_value: Mapped[float] = mapped_column(Float)
    min_value: Mapped[float] = mapped_column(Float)
    max_value: Mapped[float] = mapped_column(Float)
    sample_count: Mapped[int] = mapped_column(Integer, default=0)


class Alert(Base):
    """告警记录。"""

    __tablename__ = "alert"
    __table_args__ = (Index("ix_alert_created_status", "created_at", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    alert_code: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    device_code: Mapped[str | None] = mapped_column(String(48), index=True, nullable=True)
    region_code: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)

    level: Mapped[str] = mapped_column(String(16), index=True)  # info/warning/critical
    type: Mapped[str] = mapped_column(String(16), index=True)  # threshold/trend/model/device
    title: Mapped[str] = mapped_column(String(96))
    content: Mapped[str] = mapped_column(Text)

    metric: Mapped[str | None] = mapped_column(String(32), nullable=True)
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    dedup_key: Mapped[str] = mapped_column(String(120), index=True)
    suppressed_count: Mapped[int] = mapped_column(Integer, default=0)

    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending/handling/resolved/ignored
    handler: Mapped[str | None] = mapped_column(String(48), nullable=True)
    handle_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    handled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    extra: Mapped[dict | None] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class PredictionLog(Base):
    """赤潮风险预测日志。"""

    __tablename__ = "prediction_log"
    __table_args__ = (Index("ix_pred_device_ts", "device_code", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_code: Mapped[str] = mapped_column(String(48), index=True)
    region_code: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    ts: Mapped[datetime] = mapped_column(DateTime, index=True)
    horizon_hours: Mapped[int] = mapped_column(Integer, default=48)
    probability: Mapped[float] = mapped_column(Float)
    level: Mapped[int] = mapped_column(Integer)
    model_version: Mapped[str] = mapped_column(String(32), default="v1")
    top_factors: Mapped[list | None] = mapped_column(JSON, default=list)
    features: Mapped[dict | None] = mapped_column(JSON, default=dict)


class ModelConfig(Base):
    """模型与告警参数配置（支持版本化与灰度切换）。"""

    __tablename__ = "model_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(48), index=True)
    version: Mapped[str] = mapped_column(String(32))
    active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    params: Mapped[dict | None] = mapped_column(JSON, default=dict)
    metrics: Mapped[dict | None] = mapped_column(JSON, default=dict)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(48), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class MangroveStat(Base):
    """红树林遥感监测统计（按区域 + 期次）。"""

    __tablename__ = "mangrove_stat"
    __table_args__ = (UniqueConstraint("region_code", "period", name="uq_mangrove_region_period"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    region_code: Mapped[str] = mapped_column(String(32), index=True)
    period: Mapped[str] = mapped_column(String(16), index=True)  # 2026Q1
    area_ha: Mapped[float] = mapped_column(Float)
    change_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    health_score: Mapped[float] = mapped_column(Float)
    health_level: Mapped[str | None] = mapped_column(String(16), nullable=True)
    ndvi_mean: Mapped[float | None] = mapped_column(Float, nullable=True)
    ndwi_mean: Mapped[float | None] = mapped_column(Float, nullable=True)
    data_source: Mapped[str | None] = mapped_column(String(48), default="Sentinel-2")
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class User(Base):
    """平台用户。"""

    __tablename__ = "app_user"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(48))
    password_hash: Mapped[str] = mapped_column(String(160))
    role: Mapped[str] = mapped_column(String(16), default="viewer")  # admin/technician/farmer/viewer
    region_scope: Mapped[str | None] = mapped_column(String(64), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(24), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class NotificationLog(Base):
    """推送记录（站内/短信/微信）。"""

    __tablename__ = "notification_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    alert_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("alert.id", ondelete="CASCADE"), nullable=True)
    channel: Mapped[str] = mapped_column(String(16))  # websocket/sms/wechat/console
    target: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="sent")
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class AuditLog(Base):
    """关键操作审计。"""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(32), nullable=True)
    action: Mapped[str] = mapped_column(String(48))
    target: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSON, default=dict)
    ip: Mapped[str | None] = mapped_column(String(48), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)


class SystemEvent(Base):
    """系统事件（启动、聚合、模型加载、仿真器状态等）。"""

    __tablename__ = "system_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category: Mapped[str] = mapped_column(String(24), index=True)
    level: Mapped[str] = mapped_column(String(16), default="info")
    message: Mapped[str] = mapped_column(Text)
    detail: Mapped[dict | None] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)


class PatrolTask(Base):
    """巡护任务：桌面端派发，移动端领取、到场、拍照识别、提交存证。"""

    __tablename__ = "patrol_task"
    __table_args__ = (Index("ix_patrol_status_created", "status", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_code: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(96))
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    region_code: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    device_code: Mapped[str | None] = mapped_column(String(48), index=True, nullable=True)
    priority: Mapped[str] = mapped_column(String(16), default="normal")  # normal/high/urgent
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending/working/done
    source_alert: Mapped[str | None] = mapped_column(String(40), nullable=True)
    assignee: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # 现场证据（位置、图片指纹、识别结果），由移动端提交
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    location_note: Mapped[str | None] = mapped_column(String(120), nullable=True)
    evidence: Mapped[dict | None] = mapped_column(JSON, default=dict)
    result_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    digest: Mapped[str | None] = mapped_column(String(72), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


def utcnow_func() -> datetime:  # pragma: no cover - 兼容占位
    return func.now()
