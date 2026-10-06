"""Pydantic 数据契约（请求 / 响应模型）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# 通用
# ---------------------------------------------------------------------------
class ApiResponse(BaseModel):
    code: int = 0
    message: str = "success"
    data: Any = None
    timestamp: datetime = Field(default_factory=datetime.now)


# ---------------------------------------------------------------------------
# 鉴权
# ---------------------------------------------------------------------------
class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: "UserOut"


class UserOut(ORMModel):
    id: int
    username: str
    display_name: str
    role: str
    region_scope: str | None = None
    phone: str | None = None
    active: bool = True
    last_login: datetime | None = None


# ---------------------------------------------------------------------------
# 设备
# ---------------------------------------------------------------------------
class DeviceBase(BaseModel):
    name: str
    type: Literal["buoy", "camera", "gateway", "weather", "uav"]
    region_code: str | None = None
    longitude: float | None = None
    latitude: float | None = None
    world_x: float | None = None
    world_z: float | None = None
    depth_m: float | None = 0.5
    sample_interval: int = 300
    firmware: str | None = "v1.0.0"
    config: dict[str, Any] | None = None


class DeviceCreate(DeviceBase):
    device_code: str


class DeviceUpdate(BaseModel):
    name: str | None = None
    region_code: str | None = None
    status: str | None = None
    sample_interval: int | None = None
    firmware: str | None = None
    depth_m: float | None = None
    config: dict[str, Any] | None = None
    longitude: float | None = None
    latitude: float | None = None


class DeviceOut(ORMModel):
    id: int
    device_code: str
    name: str
    type: str
    region_code: str | None
    longitude: float | None
    latitude: float | None
    world_x: float | None
    world_z: float | None
    status: str
    battery: float | None
    signal: int | None
    solar_voltage: float | None
    firmware: str | None
    sample_interval: int
    depth_m: float | None
    config: dict[str, Any] | None
    last_seen: datetime | None
    installed_at: datetime | None


class DeviceCommand(BaseModel):
    command: Literal["set_interval", "reboot", "calibrate", "sync_time", "upgrade_firmware"]
    payload: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# 数据接入
# ---------------------------------------------------------------------------
class ReadingIn(BaseModel):
    device_code: str
    ts: datetime
    water_temp: float | None = None
    ph: float | None = None
    dissolved_oxygen: float | None = None
    turbidity: float | None = None
    chlorophyll_a: float | None = None
    salinity: float | None = None
    battery: float | None = None
    signal: int | None = None
    source: str = "edge"


class BatchIngestRequest(BaseModel):
    items: list[ReadingIn]
    signature: str | None = None
    ts: int | None = None
    nonce: str | None = None


class ReadingOut(ORMModel):
    id: int
    device_code: str
    ts: datetime
    water_temp: float | None
    ph: float | None
    dissolved_oxygen: float | None
    turbidity: float | None
    chlorophyll_a: float | None
    salinity: float | None
    quality_flag: int
    battery: float | None
    signal: int | None


class HeartbeatIn(BaseModel):
    device_code: str
    battery: float | None = None
    signal: int | None = None
    solar_voltage: float | None = None
    firmware: str | None = None
    status: str = "online"


# ---------------------------------------------------------------------------
# 告警
# ---------------------------------------------------------------------------
class AlertOut(ORMModel):
    id: int
    alert_code: str
    device_code: str | None
    region_code: str | None
    level: str
    type: str
    title: str
    content: str
    metric: str | None
    value: float | None
    threshold: float | None
    status: str
    handler: str | None
    handle_note: str | None
    suppressed_count: int
    extra: dict[str, Any] | None
    created_at: datetime
    handled_at: datetime | None


class AlertHandle(BaseModel):
    status: Literal["handling", "resolved", "ignored"] = "resolved"
    note: str | None = None


# ---------------------------------------------------------------------------
# 预测
# ---------------------------------------------------------------------------
class RedTidePrediction(BaseModel):
    device_code: str | None
    region_code: str | None
    region_name: str | None = None
    ts: datetime
    horizon_hours: int
    probability: float
    level: int
    level_label: str
    level_name: str
    color: str
    advice: str
    model_version: str
    top_factors: list[dict[str, Any]] = Field(default_factory=list)
    basis: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# 红树林
# ---------------------------------------------------------------------------
class MangroveStatOut(ORMModel):
    id: int
    region_code: str
    period: str
    area_ha: float
    change_pct: float | None
    health_score: float
    health_level: str | None
    ndvi_mean: float | None
    ndwi_mean: float | None
    data_source: str | None
    remark: str | None


# ---------------------------------------------------------------------------
# 模型配置
# ---------------------------------------------------------------------------
class ModelConfigOut(ORMModel):
    id: int
    name: str
    version: str
    active: bool
    params: dict[str, Any] | None
    metrics: dict[str, Any] | None
    description: str | None
    updated_by: str | None
    updated_at: datetime


class ModelConfigUpdate(BaseModel):
    params: dict[str, Any] | None = None
    active: bool | None = None
    description: str | None = None


# ---------------------------------------------------------------------------
# 大屏总览
# ---------------------------------------------------------------------------
class DashboardOverview(BaseModel):
    generated_at: datetime
    summary: dict[str, Any]
    latest: list[dict[str, Any]]
    regions: list[dict[str, Any]]
    alerts: list[dict[str, Any]]
    risk: dict[str, Any]
    mangrove: dict[str, Any]
    eco_share: list[dict[str, Any]]
    trend: dict[str, Any]


# ---------------------------------------------------------------------------
# 海洋生态智能体
# ---------------------------------------------------------------------------
class AgentChatRequest(BaseModel):
    """智能体问答请求：question 为当前问题，history 为可选的多轮上下文（仅 user/assistant 角色）。"""

    question: str = Field(min_length=1, max_length=500)
    history: list[dict[str, str]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 视觉识别（视频 AI 巡视）
# ---------------------------------------------------------------------------
class VisionDetectRequest(BaseModel):
    """单帧识别请求：sample 与 image_base64 二选一。"""

    device_code: str | None = None
    sample: str | None = Field(default=None, max_length=200)
    image_base64: str | None = None
    conf: float | None = Field(default=None, ge=0.05, le=0.95)


class VisionScanRequest(BaseModel):
    """视频逐帧巡视请求：对本地录像按采样帧率执行识别并汇总。"""

    device_code: str | None = None
    video_path: str = Field(min_length=1, max_length=500)
    sample_fps: float = Field(default=1.0, ge=0.1, le=10.0)
    max_frames: int = Field(default=120, ge=1, le=600)
    conf: float | None = Field(default=None, ge=0.05, le=0.95)


class VisionAlertRequest(BaseModel):
    """将人工确认的识别结果转为平台告警。"""

    device_code: str = Field(min_length=1, max_length=48)
    label: str = Field(min_length=1, max_length=40)
    confidence: float = Field(ge=0.0, le=1.0)
    note: str | None = Field(default=None, max_length=300)
    sample: str | None = Field(default=None, max_length=200)


class VisionModelSwitchRequest(BaseModel):
    """切换推理节点使用的权重文件（例如换成自训练的海洋场景模型）。"""

    name: str = Field(min_length=1, max_length=120)


# ---------------------------------------------------------------------------
# 巡护任务（桌面派发 / 移动执行）
# ---------------------------------------------------------------------------
class PatrolTaskCreate(BaseModel):
    """派发一条巡护任务。"""

    title: str = Field(min_length=1, max_length=96)
    detail: str | None = Field(default=None, max_length=1000)
    region_code: str | None = Field(default=None, max_length=32)
    device_code: str | None = Field(default=None, max_length=48)
    priority: Literal["normal", "high", "urgent"] = "normal"
    assignee: str | None = Field(default=None, max_length=32)
    due_hours: int = Field(default=24, ge=1, le=168)
    source_alert: str | None = Field(default=None, max_length=40)


class PatrolEvidence(BaseModel):
    """现场证据：图片指纹与识别结果，全部来自真实推理输出。"""

    image_sha256: str | None = Field(default=None, max_length=72)
    image_name: str | None = Field(default=None, max_length=120)
    detections: list[dict[str, Any]] = Field(default_factory=list)
    model: str | None = Field(default=None, max_length=80)
    captured_at: str | None = Field(default=None, max_length=40)


class PatrolSubmit(BaseModel):
    """移动端提交现场记录。"""

    result_note: str | None = Field(default=None, max_length=1000)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    location_note: str | None = Field(default=None, max_length=120)
    evidence: PatrolEvidence | None = None
    create_alert: bool = False


class PatrolClaim(BaseModel):
    """领取任务，可指定执行人（默认当前账号）。"""

    assignee: str | None = Field(default=None, max_length=32)


TokenResponse.model_rebuild()
