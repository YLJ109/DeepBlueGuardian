"""赤潮预测接口：单站预测、全域预测、风险分布、历史预测回放、模型信息。"""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException
from sqlalchemy import desc, select

from ..config import RISK_LEVELS
from ..data.sites import REGION_BY_CODE
from ..deps import DbSession, TechnicianUser
from ..ml.red_tide import artifact_exists, get_model, model_meta
from ..models import Device, PredictionLog
from ..services.alerts import AlertEngine
from ..services.predictor import predict_all, predict_device, region_risk_summary

router = APIRouter(prefix="/predict", tags=["赤潮预测"])


@router.get("/model", summary="当前模型信息与评估指标")
def model_info() -> dict:
    model = get_model()
    meta = model_meta()
    return {
        "code": 0,
        "data": {
            "version": model.version,
            "loaded": model.loaded,
            "artifact_exists": artifact_exists(),
            "mode": "tree-ensemble" if model.loaded else "rule-based-fallback",
            "thresholds": list(model.thresholds),
            "risk_levels": [
                {"level": k, **v} for k, v in RISK_LEVELS.items()
            ],
            "metrics": meta.get("metrics", {}),
            "trained_at": meta.get("trained_at"),
            "algorithm": meta.get("algorithm"),
            "labeling": meta.get("labeling", {}),
            "training": {
                **(meta.get("training") or {}),
                "window_hours": meta.get("window_hours"),
                "horizon_hours": meta.get("horizon_hours"),
            },
            "features": meta.get("features", {}),
            "confusion_matrix": meta.get("confusion_matrix"),
            "per_class": meta.get("per_class", []),
        },
    }


@router.get("/overview", summary="全域赤潮风险概览")
def overview(db: DbSession, hours: int = 48) -> dict:
    summary = region_risk_summary(db, hours=hours)
    since = datetime.now() - timedelta(hours=hours)
    latest = db.execute(
        select(PredictionLog).where(PredictionLog.ts >= since).order_by(desc(PredictionLog.ts)).limit(200)
    ).scalars().all()
    seen: set[str] = set()
    devices: list[dict] = []
    for row in latest:
        if row.device_code in seen:
            continue
        seen.add(row.device_code)
        device = db.execute(select(Device).where(Device.device_code == row.device_code)).scalar_one_or_none()
        region = REGION_BY_CODE.get(row.region_code or "")
        devices.append(
            {
                "device_code": row.device_code,
                "device_name": device.name if device else row.device_code,
                "region_code": row.region_code,
                "region_name": region.name if region else None,
                "world_x": device.world_x if device else None,
                "world_z": device.world_z if device else None,
                "ts": row.ts.isoformat(timespec="seconds"),
                "probability": row.probability,
                "level": row.level,
                "level_label": RISK_LEVELS[row.level]["label"],
                "level_name": RISK_LEVELS[row.level]["name"],
                "color": RISK_LEVELS[row.level]["color"],
                "top_factors": row.top_factors or [],
                "basis": row.features or {},
            }
        )
    devices.sort(key=lambda d: (d["level"], d["probability"]), reverse=True)
    return {"code": 0, "summary": summary, "devices": devices}


@router.post("/run", summary="执行一次全域预测（技术员及以上）")
def run_all(
    db: DbSession,
    _: TechnicianUser,
    horizon_hours: int = 48,
) -> dict:
    results = predict_all(db, horizon_hours=horizon_hours, persist=True)
    return {"code": 0, "message": f"完成 {len(results)} 个点位的预测", "data": results}


@router.get("/device/{device_code}", summary="单设备实时预测")
def predict_one(db: DbSession, device_code: str, horizon_hours: int = 48, raise_alert: bool = False) -> dict:
    device = db.execute(select(Device).where(Device.device_code == device_code)).scalar_one_or_none()
    if device is None:
        raise HTTPException(status_code=404, detail="设备不存在")
    result = predict_device(db, device, horizon_hours=horizon_hours, persist=True, raise_alert=raise_alert)
    if "error" in result:
        raise HTTPException(status_code=422, detail=result["error"])
    return {"code": 0, "data": result}


@router.get("/history/{device_code}", summary="单设备预测历史")
def prediction_history(db: DbSession, device_code: str, hours: int = 168, limit: int = 500) -> dict:
    since = datetime.now() - timedelta(hours=max(1, min(hours, 24 * 90)))
    rows = db.execute(
        select(PredictionLog)
        .where(PredictionLog.device_code == device_code)
        .where(PredictionLog.ts >= since)
        .order_by(PredictionLog.ts)
        .limit(min(limit, 2000))
    ).scalars().all()
    return {
        "code": 0,
        "device_code": device_code,
        "timestamps": [r.ts.isoformat(timespec="seconds") for r in rows],
        "probability": [round(r.probability, 4) for r in rows],
        "level": [r.level for r in rows],
        "points": [
            {
                "ts": r.ts.isoformat(timespec="seconds"),
                "probability": r.probability,
                "level": r.level,
                "level_label": RISK_LEVELS[r.level]["label"],
                "color": RISK_LEVELS[r.level]["color"],
                "top_factors": r.top_factors or [],
            }
            for r in rows
        ],
    }


@router.post("/alert/{device_code}", summary="按当前预测直接生成模型告警")
def raise_alert(db: DbSession, device_code: str, _: TechnicianUser) -> dict:
    device = db.execute(select(Device).where(Device.device_code == device_code)).scalar_one_or_none()
    if device is None:
        raise HTTPException(status_code=404, detail="设备不存在")
    result = predict_device(db, device, persist=True, raise_alert=False)
    if "error" in result:
        raise HTTPException(status_code=422, detail=result["error"])
    alert = AlertEngine(db).raise_model_alert(device, result)
    return {
        "code": 0,
        "message": "已生成模型告警" if alert else "当前风险等级未达到告警阈值",
        "prediction": result,
        "alert": alert.alert_code if alert else None,
    }
