"""模型与规则配置接口：查看、更新、激活版本、重载模型、系统事件。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from ..config import ALERT_THRESHOLDS, METRICS, RISK_LEVELS, TREND_RULES, settings
from ..deps import AdminUser, DbSession
from ..ml.red_tide import model_meta, reload_model
from ..models import AuditLog, ModelConfig, SystemEvent
from ..schemas import ModelConfigOut, ModelConfigUpdate

router = APIRouter(prefix="/models", tags=["模型配置"])


@router.get("", summary="模型与规则配置列表")
def list_configs(db: DbSession) -> dict:
    rows = db.execute(select(ModelConfig).order_by(ModelConfig.name, ModelConfig.version)).scalars().all()
    return {"code": 0, "data": [ModelConfigOut.model_validate(r).model_dump(mode="json") for r in rows]}


@router.get("/rules", summary="当前生效的告警规则与风险分级口径")
def rules() -> dict:
    return {
        "code": 0,
        "data": {
            "thresholds": [
                {
                    "metric": metric,
                    "metric_label": METRICS[metric]["label"],
                    "unit": METRICS[metric]["unit"],
                    "conditions": conditions,
                }
                for metric, conditions in ALERT_THRESHOLDS.items()
            ],
            "trends": [
                {**rule, "metric_label": METRICS[rule["metric"]]["label"], "unit": METRICS[rule["metric"]]["unit"]}
                for rule in TREND_RULES
            ],
            "risk_levels": [{"level": k, **v} for k, v in RISK_LEVELS.items()],
            "suppress_minutes": settings.alert_suppress_minutes,
            "offline_seconds": 1800,
        },
    }


@router.get("/meta", summary="赤潮模型评测指标")
def meta() -> dict:
    return {"code": 0, "data": model_meta()}


@router.patch("/{config_id}", summary="更新模型配置（管理员）")
def update_config(
    config_id: int,
    payload: ModelConfigUpdate,
    db: DbSession,
    user: AdminUser,
) -> dict:
    config = db.get(ModelConfig, config_id)
    if config is None:
        raise HTTPException(status_code=404, detail="配置不存在")

    if payload.params is not None:
        merged: dict[str, Any] = {**(config.params or {}), **payload.params}
        config.params = merged
        if config.name == "red_tide" and "thresholds" in merged:
            thresholds = merged["thresholds"]
            if not (isinstance(thresholds, list) and len(thresholds) == 3):
                raise HTTPException(status_code=400, detail="thresholds 必须为长度为 3 的数组")
            if not all(0 < float(t) < 1 for t in thresholds) or not (
                float(thresholds[0]) < float(thresholds[1]) < float(thresholds[2])
            ):
                raise HTTPException(status_code=400, detail="阈值需满足 0 < t1 < t2 < t3 < 1")
    if payload.description is not None:
        config.description = payload.description
    if payload.active is not None:
        if payload.active:
            for other in db.execute(
                select(ModelConfig).where(ModelConfig.name == config.name).where(ModelConfig.id != config.id)
            ).scalars().all():
                other.active = False
        config.active = payload.active

    config.updated_by = user.username
    db.add(
        AuditLog(
            username=user.username,
            action="update_model_config",
            target=f"{config.name}:{config.version}",
            detail=payload.model_dump(exclude_none=True),
        )
    )
    db.add(
        SystemEvent(
            category="model_config",
            level="info",
            message=f"更新配置 {config.name}:{config.version}",
            detail={"operator": user.username, "params": config.params},
        )
    )
    db.commit()
    db.refresh(config)
    return {"code": 0, "data": ModelConfigOut.model_validate(config).model_dump(mode="json")}


@router.post("/reload", summary="重新加载赤潮模型（管理员）")
def reload(db: DbSession, user: AdminUser, version: str = "v1") -> dict:
    model = reload_model(version)
    db.add(
        SystemEvent(
            category="model",
            level="info",
            message=f"重载模型 red_tide:{version}",
            detail={"operator": user.username, "loaded": model.loaded, "mode": "tree" if model.loaded else "fallback"},
        )
    )
    db.commit()
    return {
        "code": 0,
        "message": "模型已重载" if model.loaded else "未找到模型文件，当前使用规则降级模式",
        "loaded": model.loaded,
        "version": model.version,
    }


@router.get("/events", summary="系统事件流")
def events(db: DbSession, category: str | None = None, limit: int = 100) -> dict:
    stmt = select(SystemEvent)
    if category:
        stmt = stmt.where(SystemEvent.category == category)
    rows = db.execute(stmt.order_by(SystemEvent.id.desc()).limit(min(limit, 500))).scalars().all()
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
