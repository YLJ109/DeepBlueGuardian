"""初始化数据：连江本地区域、设备、红树林基线、账号与模型配置。

执行：
    python -m app.seed             # 首次初始化（已存在则跳过）
    python -m app.seed --reset     # 清空后重建
    python -m app.seed --history   # 额外回填历史观测数据
"""

from __future__ import annotations

import argparse
import logging
from datetime import datetime

from sqlalchemy import delete, select

from .config import settings
from .data.sites import MANGROVE_BASELINE, REGIONS, SITES, devices_for_site
from .database import Base, SessionLocal, engine, init_db
from .models import (
    Alert,
    Device,
    MangroveStat,
    MetricAggregate,
    ModelConfig,
    NotificationLog,
    PredictionLog,
    Region,
    SensorReading,
    SystemEvent,
    User,
)
from .security import hash_password

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("deepblue.seed")

DEFAULT_USERS = (
    ("admin", "平台管理员", "admin", "deepblue@2026", "13800000001", None),
    ("operator", "连江运维工程师", "technician", "ocean@2026", "13800000002", None),
    ("farmer", "黄岐牧场养殖户", "farmer", "farm@2026", "13800000003", "LJ-HQ"),
    ("viewer", "生态监测访客", "viewer", "view@2026", None, None),
)

MODEL_CONFIGS = (
    {
        "name": "red_tide",
        "version": "v1",
        "active": True,
        "description": "赤潮风险四级分级模型，输入 24 点水质时序窗口，预测未来 48 小时风险。",
        "params": {
            "horizon_hours": 48,
            "window_length": 24,
            "thresholds": [0.25, 0.50, 0.75],
            "feature_count": 49,
            "algorithm": "GradientBoostingClassifier",
        },
    },
    {
        "name": "mangrove_segmentation",
        "version": "v1",
        "active": True,
        "description": "红树林遥感指数分割模型，基于 NDVI/NDWI/MNDWI/CMRI 的多指数决策融合。",
        "params": {
            "ndvi_threshold": 0.28,
            "ndwi_threshold": 0.02,
            "min_patch_ha": 0.4,
            "health_weights": {"ndvi": 0.5, "cmri": 0.3, "fragmentation": 0.2},
        },
    },
    {
        "name": "alert_rules",
        "version": "v1",
        "active": True,
        "description": "水质阈值与趋势告警规则集，阈值依据《海水水质标准》GB 3097 与养殖水质经验值。",
        "params": {
            "suppress_minutes": settings.alert_suppress_minutes,
            "offline_seconds": 1800,
            "trend_window_hours": 1,
        },
    },
)


def seed_reference(db) -> dict[str, int]:
    """写入区域、设备、红树林基线、模型配置。"""

    created = {"regions": 0, "devices": 0, "mangrove": 0, "model_configs": 0}

    for spec in REGIONS:
        if db.execute(select(Region).where(Region.code == spec.code)).scalar_one_or_none():
            continue
        wx, wz = spec.world
        db.add(
            Region(
                code=spec.code,
                name=spec.name,
                parent_code=spec.parent_code,
                center_lon=spec.longitude,
                center_lat=spec.latitude,
                world_x=wx,
                world_z=wz,
                kind=spec.kind,
                remark=spec.remark,
            )
        )
        created["regions"] += 1

    for site in SITES:
        for item in devices_for_site(site):
            code = item["device_code"]
            if db.execute(select(Device).where(Device.device_code == code)).scalar_one_or_none():
                continue
            from .data.sites import to_world

            wx, wz = to_world(item["longitude"], item["latitude"])
            # 同一批浮标需呈现个体差异：电量 / 信号 / 采样间隔 / 太阳能板电压各不相同
            code_seed = sum(ord(ch) for ch in code)
            is_buoy = item["type"] == "buoy"
            db.add(
                Device(
                    device_code=code,
                    name=item["name"],
                    type=item["type"],
                    region_code=item["region_code"],
                    longitude=item["longitude"],
                    latitude=item["latitude"],
                    world_x=wx,
                    world_z=wz,
                    depth_m=item.get("depth_m"),
                    sample_interval=item.get("sample_interval") or (300, 600, 900)[code_seed % 3],
                    firmware="v1.4.2" if is_buoy else "v2.1.0",
                    status="online",
                    battery=round(86.0 + (code_seed % 12) * 0.9, 1) if is_buoy else None,
                    signal=-(57 + code_seed % 15) if is_buoy else None,
                    solar_voltage=round(12.9 + (code_seed % 6) * 0.08, 2) if is_buoy else None,
                    installed_at=datetime(2026, 3, 18, 9, 30, 0),
                    config={
                        "site": site.name,
                        "depth_m": site.depth_m,
                        "summary": site.summary,
                        "sensors": ["水温", "pH", "溶解氧", "浊度", "叶绿素a", "盐度"]
                        if item["type"] == "buoy"
                        else [],
                        "power": "20W 单晶硅 + 12V/40Ah 磷酸铁锂",
                        "comm": "LoRa SX1262 + 4G Cat.1",
                    },
                )
            )
            created["devices"] += 1

    for row in MANGROVE_BASELINE:
        exists = db.execute(
            select(MangroveStat)
            .where(MangroveStat.region_code == row["region_code"])
            .where(MangroveStat.period == row["period"])
        ).scalar_one_or_none()
        if exists:
            continue
        db.add(MangroveStat(**row))
        created["mangrove"] += 1

    for item in MODEL_CONFIGS:
        exists = db.execute(
            select(ModelConfig).where(ModelConfig.name == item["name"]).where(ModelConfig.version == item["version"])
        ).scalar_one_or_none()
        if exists:
            continue
        db.add(ModelConfig(**item, updated_by="system"))
        created["model_configs"] += 1

    db.commit()
    return created


def seed_users(db) -> int:
    created = 0
    for username, display, role, password, phone, scope in DEFAULT_USERS:
        if db.execute(select(User).where(User.username == username)).scalar_one_or_none():
            continue
        db.add(
            User(
                username=username,
                display_name=display,
                role=role,
                phone=phone,
                region_scope=scope,
                password_hash=hash_password(password),
            )
        )
        created += 1
    db.commit()
    return created


def reset_all(db) -> None:
    """按外键依赖顺序清空业务表（保留表结构）。"""

    for model in (
        NotificationLog,
        Alert,
        PredictionLog,
        MetricAggregate,
        SensorReading,
        MangroveStat,
        ModelConfig,
        SystemEvent,
        Device,
        Region,
        User,
    ):
        db.execute(delete(model))
    db.commit()


def run(reset: bool = False, history: bool = False) -> None:
    init_db()
    db = SessionLocal()
    try:
        if reset:
            logger.info("清空既有业务数据")
            reset_all(db)
        refs = seed_reference(db)
        users = seed_users(db)
        logger.info("基础数据写入完成：%s，新增账号 %s 个", refs, users)

        if history:
            from .services.aggregates import rebuild_aggregates
            from .services.simulator import backfill_history

            stats = backfill_history(db, days=settings.simulator_history_days)
            logger.info("历史观测回填：%s", stats)
            agg = rebuild_aggregates(db)
            logger.info("聚合数据重建：%s", agg)

        db.add(
            SystemEvent(
                category="seed",
                level="info",
                message="平台基础数据初始化完成",
                detail={"reference": refs, "users": users, "history": history},
            )
        )
        db.commit()
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="深蓝智护平台数据初始化")
    parser.add_argument("--reset", action="store_true", help="清空业务表后重建")
    parser.add_argument("--history", action="store_true", help="同时回填历史观测数据与聚合")
    args = parser.parse_args()
    run(reset=args.reset, history=args.history)


if __name__ == "__main__":
    main()
