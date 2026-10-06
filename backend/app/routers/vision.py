"""视频 AI 识别接口。

鉴权策略与平台其他模块一致：状态、点位、样本与单帧识别对匿名开放，
便于大屏与演示环境直接调用；视频逐帧巡视与告警转化会占用 GPU 或写入业务数据，
要求技术员及以上角色，并写入审计日志。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from ..deps import AdminUser, DbSession, OptionalUser, TechnicianUser
from ..models import Device
from ..schemas import (
    VisionAlertRequest,
    VisionDetectRequest,
    VisionModelSwitchRequest,
    VisionScanRequest,
)
from ..services import vision as vision_service

logger = logging.getLogger("deepblue.vision.api")

router = APIRouter(prefix="/vision", tags=["视频AI识别"])


def _ensure_node() -> None:
    """节点离线时尝试拉起一次，仍不可用则给出可操作的错误提示。"""

    if vision_service.health() is not None:
        return
    if not vision_service.ensure_service(wait_seconds=60.0):
        raise HTTPException(
            status_code=503,
            detail=(
                "视觉推理节点未就绪，请确认深度学习环境可用，"
                "或查看 backend/data/vision_node.log 排查启动失败原因"
            ),
        )


def _device(db: DbSession, code: str | None) -> Device | None:
    if not code:
        return None
    return db.execute(select(Device).where(Device.device_code == code)).scalar_one_or_none()


def _device_brief(device: Device | None) -> dict | None:
    if device is None:
        return None
    return {
        "device_code": device.device_code,
        "name": device.name,
        "region_code": device.region_code,
        "site": (device.config or {}).get("site"),
        "status": device.status,
    }


@router.get("/status", summary="推理节点状态与可用点位、样本")
def status(db: DbSession) -> dict:
    return {"code": 0, "data": vision_service.status_payload(db)}


@router.get("/cameras", summary="可接入的视频点位（摄像头设备）")
def cameras(db: DbSession) -> dict:
    return {"code": 0, "data": vision_service.camera_devices(db)}


@router.get("/samples", summary="内置演示样本影像清单")
def samples() -> dict:
    return {"code": 0, "data": vision_service.samples()}


@router.post("/detect", summary="对样本影像或上传图片执行一次识别")
def detect(payload: VisionDetectRequest, db: DbSession) -> dict:
    _ensure_node()
    device = _device(db, payload.device_code)
    try:
        if payload.sample:
            result = vision_service.detect_sample(payload.sample, conf=payload.conf)
        elif payload.image_base64:
            result = vision_service.detect_base64(payload.image_base64, conf=payload.conf)
        else:
            raise HTTPException(status_code=400, detail="请选择样本影像或上传一张图片")
    except vision_service.VisionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    result["device"] = _device_brief(device)
    if device is not None:
        result["source"] = result.get("source") or device.device_code
    return {"code": 0, "data": result}


@router.post("/scan", summary="对本地录像执行逐帧巡视并汇总")
def scan(payload: VisionScanRequest, db: DbSession, user: TechnicianUser) -> dict:
    _ensure_node()
    device = _device(db, payload.device_code)
    try:
        result = vision_service.scan_video(
            payload.video_path,
            sample_fps=payload.sample_fps,
            max_frames=payload.max_frames,
            conf=payload.conf,
        )
    except vision_service.VisionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    result["device"] = _device_brief(device)
    return {"code": 0, "data": result}


@router.post("/alert", summary="将识别结果转为平台告警")
def create_alert(payload: VisionAlertRequest, request: Request, db: DbSession, user: TechnicianUser) -> dict:
    device = _device(db, payload.device_code)
    if device is None:
        raise HTTPException(status_code=404, detail="点位不存在")
    extra = {"sample": payload.sample} if payload.sample else {}
    try:
        result = vision_service.create_alert(
            db,
            device,
            label=payload.label,
            confidence=payload.confidence,
            note=payload.note,
            operator=user.username,
            ip=request.client.host if request.client else None,
            extra=extra,
        )
    except vision_service.VisionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"code": 0, **result}


@router.get("/models", summary="可切换的识别权重")
def models() -> dict:
    return {"code": 0, "data": vision_service.list_models()}


@router.post("/models/switch", summary="切换识别权重并重启推理节点")
def switch_model(payload: VisionModelSwitchRequest, user: AdminUser) -> dict:
    try:
        result = vision_service.switch_model(payload.name)
    except vision_service.VisionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"code": 0, **result}
