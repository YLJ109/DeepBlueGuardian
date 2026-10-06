"""全局配置。

所有配置项均可通过环境变量覆盖，便于部署到不同环境。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent  # backend/
PROJECT_DIR = BASE_DIR.parent
DATA_DIR = Path(os.getenv("DEEPBLUE_DATA_DIR", BASE_DIR / "data"))
ARTIFACT_DIR = Path(os.getenv("DEEPBLUE_ARTIFACT_DIR", BASE_DIR / "app" / "ml" / "artifacts"))

DATA_DIR.mkdir(parents=True, exist_ok=True)
ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)


def _env_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    """运行参数。"""

    app_name: str = "深蓝智护 · 连江海洋生态智能监测平台"
    api_prefix: str = "/api/v1"
    version: str = "1.0.0"
    timezone: str = "Asia/Shanghai"

    # 数据库
    db_path: Path = field(default_factory=lambda: DATA_DIR / "deepblue.db")

    # 鉴权
    jwt_secret: str = os.getenv("DEEPBLUE_JWT_SECRET", "deepblue-lianjiang-dev-secret-change-me")
    jwt_ttl_seconds: int = int(os.getenv("DEEPBLUE_JWT_TTL", str(12 * 3600)))
    device_secret: str = os.getenv("DEEPBLUE_DEVICE_SECRET", "deepblue-device-key")

    # 仿真器（无真实浮标时驱动全链路演示）
    simulator_enabled: bool = _env_bool("DEEPBLUE_SIMULATOR", True)
    simulator_interval_seconds: int = int(os.getenv("DEEPBLUE_SIM_INTERVAL", "5"))
    simulator_history_days: int = int(os.getenv("DEEPBLUE_SIM_HISTORY_DAYS", "14"))

    # 聚合任务
    aggregator_enabled: bool = _env_bool("DEEPBLUE_AGGREGATOR", True)
    aggregator_interval_seconds: int = int(os.getenv("DEEPBLUE_AGG_INTERVAL", "60"))

    # 告警
    alert_suppress_minutes: int = int(os.getenv("DEEPBLUE_ALERT_SUPPRESS", "30"))

    # 海洋生态智能体（大模型 + 工具调用）
    # 默认接入智谱 AI 开放平台，可通过环境变量指向任意 OpenAI 兼容服务
    zhipu_api_key: str = os.getenv(
        "ZHIPU_API_KEY", "4c6318760d4b412983a34b6b7481260c.LelkLlczdjXYtGQi"
    )
    zhipu_base_url: str = os.getenv("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
    zhipu_model: str = os.getenv("ZHIPU_MODEL", "glm-4-flash")
    agent_enabled: bool = _env_bool("DEEPBLUE_AGENT", True)
    agent_max_rounds: int = int(os.getenv("DEEPBLUE_AGENT_MAX_ROUNDS", "4"))
    agent_timeout_seconds: float = float(os.getenv("DEEPBLUE_AGENT_TIMEOUT", "45"))

    # 视觉识别推理节点（独立进程，运行在带 CUDA 的深度学习环境）
    # 主后端只做代理与业务编排，模型前向推理全部发生在该节点内。
    vision_enabled: bool = _env_bool("DEEPBLUE_VISION", True)
    vision_base_url: str = os.getenv("DEEPBLUE_VISION_URL", "http://127.0.0.1:8001")
    vision_autostart: bool = _env_bool("DEEPBLUE_VISION_AUTOSTART", True)
    vision_python: str = os.getenv(
        "DEEPBLUE_VISION_PYTHON",
        r"C:\Users\Administrator\.conda\envs\pytorch_cuda_env\python.exe",
    )
    vision_model: str = os.getenv(
        "DEEPBLUE_VISION_MODEL", str(DATA_DIR / "models" / "marine_litter_yolo11s.pt")
    )
    vision_device: str = os.getenv("DEEPBLUE_VISION_DEVICE", "auto")
    vision_conf: float = float(os.getenv("DEEPBLUE_VISION_CONF", "0.35"))
    vision_timeout_seconds: float = float(os.getenv("DEEPBLUE_VISION_TIMEOUT", "120"))
    vision_samples_dir: Path = field(default_factory=lambda: DATA_DIR / "vision_samples")

    # 推送（可对接真实短信/微信网关）
    sms_webhook: str = os.getenv("DEEPBLUE_SMS_WEBHOOK", "")
    wechat_webhook: str = os.getenv("DEEPBLUE_WECHAT_WEBHOOK", "")

    # 前端静态资源
    frontend_dir: Path = field(default_factory=lambda: PROJECT_DIR / "frontend")

    @property
    def database_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.db_path}" if False else f"sqlite:///{self.db_path}"


settings = Settings()

# ---- 监测指标元数据（接口、前端、模型共用同一份口径） ----
METRICS: dict[str, dict] = {
    "water_temp": {"label": "水温", "unit": "℃", "range": (-2.0, 35.0), "decimals": 2},
    "ph": {"label": "pH", "unit": "", "range": (5.0, 10.0), "decimals": 2},
    "dissolved_oxygen": {"label": "溶解氧", "unit": "mg/L", "range": (0.0, 20.0), "decimals": 2},
    "turbidity": {"label": "浊度", "unit": "NTU", "range": (0.0, 1000.0), "decimals": 2},
    "chlorophyll_a": {"label": "叶绿素a", "unit": "μg/L", "range": (0.0, 200.0), "decimals": 2},
    "salinity": {"label": "盐度", "unit": "PSU", "range": (0.0, 40.0), "decimals": 2},
}

# 赤潮风险等级（与福建省海洋预报台业务口径保持一致：叶绿素a 为关键指标）
RISK_LEVELS: dict[int, dict] = {
    1: {"label": "Ⅰ级", "name": "低风险", "color": "#33e1ff", "advice": "常规监测，保持每日数据巡检"},
    2: {"label": "Ⅱ级", "name": "关注", "color": "#ffd166", "advice": "加密采样至 10 分钟，关注叶绿素a与水温变化"},
    3: {"label": "Ⅲ级", "name": "中高风险", "color": "#ff9f43", "advice": "发布预警，启动应急巡检并通知养殖户"},
    4: {"label": "Ⅳ级", "name": "高风险", "color": "#ff4d6d", "advice": "立即预警，建议提前布设应急监测与处置力量"},
}

# 海水水质告警阈值（依据《海水水质标准》GB 3097 一类/二类限值与养殖水质经验值）
ALERT_THRESHOLDS: dict[str, list[dict]] = {
    "dissolved_oxygen": [
        {"level": "critical", "op": "<", "value": 3.0, "label": "溶解氧低于 3.0 mg/L，养殖生物面临缺氧风险"},
        {"level": "warning", "op": "<", "value": 5.0, "label": "溶解氧低于 5.0 mg/L，进入警戒区间"},
    ],
    "chlorophyll_a": [
        {"level": "critical", "op": ">", "value": 20.0, "label": "叶绿素a超过 20 μg/L，达到赤潮重点关注水平"},
        {"level": "warning", "op": ">", "value": 10.0, "label": "叶绿素a超过 10 μg/L，需加密监测"},
    ],
    "water_temp": [
        {"level": "warning", "op": ">", "value": 28.0, "label": "水温超过 28 ℃，利于部分赤潮藻种繁殖"},
        {"level": "warning", "op": "<", "value": 10.0, "label": "水温低于 10 ℃，注意养殖生物应激"},
    ],
    "ph": [
        {"level": "warning", "op": ">", "value": 8.6, "label": "pH 高于 8.6，与藻类大量繁殖相关"},
        {"level": "warning", "op": "<", "value": 7.6, "label": "pH 低于 7.6，水体酸化风险"},
    ],
    "turbidity": [
        {"level": "warning", "op": ">", "value": 50.0, "label": "浊度超过 50 NTU，疑似陆源输入或底泥扰动"},
    ],
    "salinity": [
        {"level": "warning", "op": "<", "value": 22.0, "label": "盐度低于 22 PSU，注意径流淡水输入影响"},
    ],
}

# 趋势告警：1 小时内变化率阈值
TREND_RULES: list[dict] = [
    {"metric": "dissolved_oxygen", "window_hours": 1, "delta": -1.5, "level": "warning",
     "label": "溶解氧 1 小时内下降超过 1.5 mg/L"},
    {"metric": "water_temp", "window_hours": 1, "delta": 2.0, "level": "warning",
     "label": "水温 1 小时内上升超过 2.0 ℃"},
    {"metric": "water_temp", "window_hours": 1, "delta": -2.5, "level": "warning",
     "label": "水温 1 小时内下降超过 2.5 ℃"},
    {"metric": "chlorophyll_a", "window_hours": 6, "delta": 3.0, "level": "warning",
     "label": "叶绿素a 6 小时内上升超过 3.0 μg/L"},
]

settings.vision_samples_dir.mkdir(parents=True, exist_ok=True)


# ---- 视觉识别关注类别（与 vision_service/infer_server.py 保持同一口径）----
VISION_FOCUS: dict[str, dict[str, str]] = {
    "person": {
        "label": "人员",
        "category": "人员活动",
        "level": "critical",
        "title": "水域人员活动识别",
        "advice": "识别到人员出现在监测水域画面内，建议立即核查是否为违规作业或落水求助。",
    },
    "boat": {
        "label": "船只",
        "category": "船只活动",
        "level": "warning",
        "title": "船只进入监测水域",
        "advice": "识别到船只进入监测水域，建议核查船名、作业许可与是否进入禁养或航道区域。",
    },
    "bottle": {
        "label": "瓶状漂浮物",
        "category": "水面漂浮物",
        "level": "warning",
        "title": "水面疑似漂浮垃圾",
        "advice": "识别到瓶状漂浮物，建议安排保洁船只打捞并追溯陆源排放。",
    },
    "surfboard": {
        "label": "冲浪板",
        "category": "水面活动器具",
        "level": "warning",
        "title": "水面活动器具识别",
        "advice": "识别到水面活动器具，建议核查是否在养殖区或航道内活动。",
    },
    "frisbee": {
        "label": "圆盘状漂浮物",
        "category": "水面漂浮物",
        "level": "info",
        "title": "水面圆盘状漂浮物",
        "advice": "识别到圆盘状漂浮物，建议人工复核是否为养殖浮球或垃圾。",
    },
    "airplane": {
        "label": "低空飞行器",
        "category": "低空目标",
        "level": "info",
        "title": "低空飞行器活动",
        "advice": "识别到低空飞行器，建议核查是否为巡检无人机作业。",
    },
    "bird": {
        "label": "海鸟",
        "category": "生态目标",
        "level": "info",
        "title": "海鸟活动",
        "advice": "识别到海鸟活动，可作为湿地生态状况的辅助观察记录。",
    },
    # 以下类别来自海洋垃圾检测权重（连渔智脑 TideSignal 项目训练）
    "plastic": {
        "label": "塑料垃圾",
        "category": "海漂垃圾",
        "level": "warning",
        "title": "水面塑料垃圾识别",
        "advice": "识别到塑料垃圾，建议安排打捞并追溯陆源与航运排放。",
    },
    "foam": {
        "label": "泡沫塑料",
        "category": "海漂垃圾",
        "level": "warning",
        "title": "泡沫塑料垃圾识别",
        "advice": "识别到泡沫塑料垃圾，易破碎扩散，建议优先清理。",
    },
    "fishing_gear": {
        "label": "废弃渔具",
        "category": "海漂垃圾",
        "level": "warning",
        "title": "废弃渔具识别",
        "advice": "识别到废弃渔具，存在缠绕养殖设施与海洋生物的风险，建议尽快回收。",
    },
    "metal_wood": {
        "label": "金属木质垃圾",
        "category": "海漂垃圾",
        "level": "info",
        "title": "金属木质漂浮物识别",
        "advice": "识别到金属或木质漂浮物，建议复核是否影响航道与养殖设施。",
    },
    "debris_other": {
        "label": "其他海漂垃圾",
        "category": "海漂垃圾",
        "level": "info",
        "title": "其他海漂垃圾识别",
        "advice": "识别到其他类型海漂垃圾，建议人工复核并纳入打捞计划。",
    },
    "vessel": {
        "label": "船只",
        "category": "船只活动",
        "level": "warning",
        "title": "船只进入监测水域",
        "advice": "识别到船只进入监测水域，建议核查船名、作业许可与是否进入禁养或航道区域。",
    },
    "buoy": {
        "label": "养殖浮标",
        "category": "养殖设施",
        "level": "info",
        "title": "养殖浮标识别",
        "advice": "识别到养殖浮标，可用于核对养殖设施分布与漂移情况。",
    },
    "cage": {
        "label": "养殖网箱",
        "category": "养殖设施",
        "level": "info",
        "title": "养殖网箱识别",
        "advice": "识别到养殖网箱，可用于核对养殖区范围与设施完好情况。",
    },
}
