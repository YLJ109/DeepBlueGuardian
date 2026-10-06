"""巡护任务接口：桌面端派发、移动端执行、结果存证与报告。

设计口径：
- 桌面端负责调度（派单、看板、报告），移动端负责现场（领取、到场、拍照识别、提交）。
- 提交时把「图片 SHA-256 + 识别结果 + 经纬度 + 时间」一起写入 `evidence` 并计算整体
  摘要 `digest`，形成不可事后改写的存证指纹；写操作全部记入审计日志。
- 识别结果由检测模型真实输出，未识别到目标时如实记录空结果，不做任何补全。
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import func, select

from ..deps import CurrentUser, DbSession, TechnicianUser
from ..models import Alert, Device, PatrolTask, Region, User, AuditLog
from ..schemas import PatrolClaim, PatrolSubmit, PatrolTaskCreate
from ..services import vision as vision_service

logger = logging.getLogger("deepblue.patrol")

router = APIRouter(prefix="/patrol", tags=["巡护任务"])

STATUS_LABEL = {"pending": "待领取", "working": "进行中", "done": "已完成"}
PRIORITY_LABEL = {"normal": "常规", "high": "优先", "urgent": "紧急"}


def _next_code(db: DbSession) -> str:
    today = datetime.now().strftime("%Y%m%d")
    prefix = f"PT{today}"
    row = db.execute(
        select(func.count()).select_from(PatrolTask).where(PatrolTask.task_code.like(f"{prefix}%"))
    ).scalar_one()
    return f"{prefix}{int(row) + 1:03d}"


def _region_name(db: DbSession, code: str | None) -> str | None:
    if not code:
        return None
    region = db.execute(select(Region).where(Region.code == code)).scalar_one_or_none()
    return region.name if region else None


def _device_name(db: DbSession, code: str | None) -> str | None:
    if not code:
        return None
    device = db.execute(select(Device).where(Device.device_code == code)).scalar_one_or_none()
    return device.name if device else None


def _serialize(db: DbSession, task: PatrolTask, *, with_evidence: bool = False) -> dict:
    data = {
        "task_code": task.task_code,
        "title": task.title,
        "detail": task.detail,
        "region_code": task.region_code,
        "region_name": _region_name(db, task.region_code),
        "device_code": task.device_code,
        "device_name": _device_name(db, task.device_code),
        "priority": task.priority,
        "priority_label": PRIORITY_LABEL.get(task.priority, task.priority),
        "status": task.status,
        "status_label": STATUS_LABEL.get(task.status, task.status),
        "assignee": task.assignee,
        "created_by": task.created_by,
        "source_alert": task.source_alert,
        "due_at": task.due_at.isoformat(timespec="seconds") if task.due_at else None,
        "created_at": task.created_at.isoformat(timespec="seconds") if task.created_at else None,
        "started_at": task.started_at.isoformat(timespec="seconds") if task.started_at else None,
        "finished_at": task.finished_at.isoformat(timespec="seconds") if task.finished_at else None,
        "location_note": task.location_note,
        "latitude": task.latitude,
        "longitude": task.longitude,
        "result_note": task.result_note,
        "digest": task.digest,
        "detection_count": len((task.evidence or {}).get("detections") or []),
    }
    if with_evidence:
        data["evidence"] = task.evidence or {}
    return data


@router.get("/tasks", summary="巡护任务列表")
def list_tasks(
    db: DbSession,
    status: str | None = Query(default=None, description="pending/working/done"),
    region_code: str | None = Query(default=None),
    assignee: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=300),
) -> dict:
    stmt = select(PatrolTask).order_by(PatrolTask.created_at.desc()).limit(limit)
    if status:
        stmt = stmt.where(PatrolTask.status == status)
    if region_code:
        stmt = stmt.where(PatrolTask.region_code == region_code)
    if assignee:
        stmt = stmt.where(PatrolTask.assignee == assignee)
    rows = db.execute(stmt).scalars().all()
    return {"code": 0, "data": [_serialize(db, t) for t in rows]}


@router.post("/tasks", summary="派发巡护任务（技术员及以上）")
def create_task(payload: PatrolTaskCreate, request: Request, db: DbSession, user: TechnicianUser) -> dict:
    if payload.device_code:
        device = db.execute(select(Device).where(Device.device_code == payload.device_code)).scalar_one_or_none()
        if device is None:
            raise HTTPException(status_code=404, detail="关联设备不存在")
    task = PatrolTask(
        task_code=_next_code(db),
        title=payload.title,
        detail=payload.detail,
        region_code=payload.region_code,
        device_code=payload.device_code,
        priority=payload.priority,
        assignee=payload.assignee,
        created_by=user.username,
        source_alert=payload.source_alert,
        due_at=datetime.now() + timedelta(hours=payload.due_hours),
        evidence={},
    )
    db.add(task)
    db.add(
        AuditLog(
            username=user.username,
            action="patrol.create",
            target=task.task_code,
            detail={"title": task.title, "region": task.region_code, "priority": task.priority},
            ip=request.client.host if request.client else None,
        )
    )
    db.commit()
    db.refresh(task)
    return {"code": 0, "data": _serialize(db, task)}


@router.get("/tasks/{task_code}", summary="任务详情")
def task_detail(task_code: str, db: DbSession) -> dict:
    task = db.execute(select(PatrolTask).where(PatrolTask.task_code == task_code)).scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"code": 0, "data": _serialize(db, task, with_evidence=True)}


@router.post("/tasks/{task_code}/claim", summary="领取任务")
def claim_task(task_code: str, payload: PatrolClaim, request: Request, db: DbSession, user: CurrentUser) -> dict:
    task = db.execute(select(PatrolTask).where(PatrolTask.task_code == task_code)).scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.status == "done":
        raise HTTPException(status_code=400, detail="任务已完成，无法再次领取")
    task.status = "working"
    task.assignee = payload.assignee or user.username
    task.started_at = task.started_at or datetime.now()
    db.add(
        AuditLog(
            username=user.username,
            action="patrol.claim",
            target=task.task_code,
            detail={"assignee": task.assignee},
            ip=request.client.host if request.client else None,
        )
    )
    db.commit()
    db.refresh(task)
    return {"code": 0, "data": _serialize(db, task)}


@router.post("/tasks/{task_code}/submit", summary="提交现场记录（含存证）")
def submit_task(
    task_code: str,
    payload: PatrolSubmit,
    request: Request,
    db: DbSession,
    user: CurrentUser,
) -> dict:
    task = db.execute(select(PatrolTask).where(PatrolTask.task_code == task_code)).scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.status == "done":
        raise HTTPException(status_code=400, detail="任务已提交，如需修改请重新派单")

    evidence = payload.evidence.model_dump() if payload.evidence else {}
    now = datetime.now()
    canonical = {
        "task_code": task.task_code,
        "operator": user.username,
        "submitted_at": now.isoformat(timespec="seconds"),
        "location": [payload.latitude, payload.longitude, payload.location_note],
        "result_note": payload.result_note,
        "evidence": evidence,
    }
    digest = hashlib.sha256(
        json.dumps(canonical, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()

    task.status = "done"
    task.assignee = task.assignee or user.username
    task.started_at = task.started_at or now
    task.finished_at = now
    task.latitude = payload.latitude
    task.longitude = payload.longitude
    task.location_note = payload.location_note
    task.evidence = evidence
    task.result_note = payload.result_note
    task.digest = digest

    alert_info = None
    if payload.create_alert and task.device_code:
        focus = next(
            (d for d in (evidence.get("detections") or []) if d.get("focus")),
            None,
        )
        if focus:
            device = db.execute(select(Device).where(Device.device_code == task.device_code)).scalar_one_or_none()
            if device is not None:
                try:
                    result = vision_service.create_alert(
                        db,
                        device,
                        label=focus.get("name") or "debris_other",
                        confidence=float(focus.get("confidence") or 0.0),
                        note=f"移动端巡护 {task.task_code} 现场识别上报",
                        operator=user.username,
                        ip=request.client.host if request.client else None,
                        extra={"patrol": task.task_code},
                    )
                    alert_info = result.get("alert")
                except vision_service.VisionError as exc:
                    logger.warning("巡护转告警失败：%s", exc)

    db.add(
        AuditLog(
            username=user.username,
            action="patrol.submit",
            target=task.task_code,
            detail={"digest": digest, "detections": len(evidence.get("detections") or []), "alert": bool(alert_info)},
            ip=request.client.host if request.client else None,
        )
    )
    db.commit()
    db.refresh(task)
    data = _serialize(db, task, with_evidence=True)
    data["alert"] = alert_info
    return {"code": 0, "data": data}


@router.get("/summary", summary="巡护任务统计")
def summary(db: DbSession) -> dict:
    rows = db.execute(select(PatrolTask)).scalars().all()
    total = len(rows)
    pending = sum(1 for t in rows if t.status == "pending")
    working = sum(1 for t in rows if t.status == "working")
    done = sum(1 for t in rows if t.status == "done")
    durations = [
        (t.finished_at - t.started_at).total_seconds() / 60
        for t in rows
        if t.finished_at and t.started_at
    ]
    detections = sum(len((t.evidence or {}).get("detections") or []) for t in rows)
    return {
        "code": 0,
        "data": {
            "total": total,
            "pending": pending,
            "working": working,
            "done": done,
            "done_rate": round(done / total * 100, 1) if total else 0.0,
            "avg_minutes": round(sum(durations) / len(durations), 1) if durations else None,
            "detections": detections,
            "open": pending + working,
        },
    }


@router.get("/report", summary="巡检报告数据（按时间窗聚合）")
def report(db: DbSession, hours: int = Query(default=24, ge=1, le=720)) -> dict:
    since = datetime.now() - timedelta(hours=hours)
    tasks = db.execute(
        select(PatrolTask).where(PatrolTask.created_at >= since).order_by(PatrolTask.created_at.desc())
    ).scalars().all()
    alerts = db.execute(
        select(Alert).where(Alert.created_at >= since).order_by(Alert.created_at.desc()).limit(200)
    ).scalars().all()

    done = [t for t in tasks if t.status == "done"]
    detections: list[dict] = []
    for task in done:
        for item in (task.evidence or {}).get("detections") or []:
            if item.get("focus"):
                detections.append(
                    {
                        "task_code": task.task_code,
                        "region_name": _region_name(db, task.region_code),
                        "label": item.get("label") or item.get("name"),
                        "confidence": item.get("confidence"),
                        "finished_at": task.finished_at.isoformat(timespec="seconds") if task.finished_at else None,
                    }
                )

    by_level: dict[str, int] = {}
    for alert in alerts:
        by_level[alert.level] = by_level.get(alert.level, 0) + 1

    operators = sorted({t.assignee for t in done if t.assignee})
    return {
        "code": 0,
        "data": {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "window_hours": hours,
            "since": since.isoformat(timespec="seconds"),
            "tasks": {
                "total": len(tasks),
                "done": len(done),
                "open": len(tasks) - len(done),
                "items": [_serialize(db, t) for t in tasks],
            },
            "alerts": {"total": len(alerts), "by_level": by_level},
            "detections": {"total": len(detections), "items": detections},
            "operators": operators,
            "evidence_chain": [
                {"task_code": t.task_code, "digest": t.digest, "finished_at": t.finished_at.isoformat(timespec="seconds")}
                for t in done
                if t.digest
            ],
        },
    }
