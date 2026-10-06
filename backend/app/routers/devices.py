"""设备管理接口：台账、状态、心跳、远程指令、设备事件。"""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException
from sqlalchemy import func, select

from ..data.sites import REGION_BY_CODE
from ..deps import AdminUser, DbSession, TechnicianUser
from ..models import AuditLog, Device, SensorReading, SystemEvent
from ..schemas import DeviceCommand, DeviceCreate, DeviceOut, DeviceUpdate, HeartbeatIn

router = APIRouter(prefix="/devices", tags=["设备管理"])


def _device_payload(device: Device) -> dict:
    data = DeviceOut.model_validate(device).model_dump(mode="json")
    region = REGION_BY_CODE.get(device.region_code or "")
    data["region_name"] = region.name if region else None
    site = (device.config or {}).get("site")
    data["site"] = site
    return data


@router.get("", summary="设备列表")
def list_devices(
    db: DbSession,
    type: str | None = None,
    region_code: str | None = None,
    status: str | None = None,
    keyword: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> dict:
    stmt = select(Device)
    if type:
        stmt = stmt.where(Device.type == type)
    if region_code:
        stmt = stmt.where(Device.region_code == region_code)
    if status:
        stmt = stmt.where(Device.status == status)
    if keyword:
        like = f"%{keyword}%"
        stmt = stmt.where(Device.name.like(like) | Device.device_code.like(like))
    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = db.execute(
        stmt.order_by(Device.type, Device.device_code).limit(min(limit, 500)).offset(offset)
    ).scalars().all()
    return {"code": 0, "total": int(total), "data": [_device_payload(d) for d in rows]}


@router.get("/stats", summary="设备状态统计")
def device_stats(db: DbSession) -> dict:
    rows = db.execute(select(Device.type, Device.status, func.count()).group_by(Device.type, Device.status)).all()
    by_type: dict[str, dict[str, int]] = {}
    total = 0
    online = 0
    for device_type, status, count in rows:
        bucket = by_type.setdefault(device_type, {"online": 0, "offline": 0, "fault": 0, "total": 0})
        bucket[status] = bucket.get(status, 0) + count
        bucket["total"] += count
        total += count
        if status == "online":
            online += count
    buoys = by_type.get("buoy", {})
    return {
        "code": 0,
        "data": {
            "total": total,
            "online": online,
            "offline": total - online,
            "online_rate": round(online / total * 100, 2) if total else 0.0,
            "by_type": by_type,
            "buoy_online": buoys.get("online", 0),
            "buoy_total": buoys.get("total", 0),
        },
    }


@router.get("/regions", summary="区域列表与点位")
def list_regions(db: DbSession) -> dict:
    from ..models import Region

    regions = db.execute(select(Region).order_by(Region.code)).scalars().all()
    devices = db.execute(select(Device)).scalars().all()
    counter: dict[str, int] = {}
    for d in devices:
        counter[d.region_code or ""] = counter.get(d.region_code or "", 0) + 1
    return {
        "code": 0,
        "data": [
            {
                "code": r.code,
                "name": r.name,
                "kind": r.kind,
                "longitude": r.center_lon,
                "latitude": r.center_lat,
                "world_x": r.world_x,
                "world_z": r.world_z,
                "parent_code": r.parent_code,
                "remark": r.remark,
                "device_count": counter.get(r.code, 0),
            }
            for r in regions
        ],
    }


@router.get("/{device_code}", summary="设备详情")
def get_device(device_code: str, db: DbSession) -> dict:
    device = db.execute(select(Device).where(Device.device_code == device_code)).scalar_one_or_none()
    if device is None:
        raise HTTPException(status_code=404, detail="设备不存在")
    data = _device_payload(device)
    latest = db.execute(
        select(SensorReading)
        .where(SensorReading.device_code == device_code)
        .order_by(SensorReading.ts.desc())
        .limit(1)
    ).scalar_one_or_none()
    data["latest"] = (
        {
            "ts": latest.ts.isoformat(timespec="seconds"),
            "water_temp": latest.water_temp,
            "ph": latest.ph,
            "dissolved_oxygen": latest.dissolved_oxygen,
            "turbidity": latest.turbidity,
            "chlorophyll_a": latest.chlorophyll_a,
            "salinity": latest.salinity,
            "quality_flag": latest.quality_flag,
        }
        if latest
        else None
    )
    data["reading_count"] = int(
        db.execute(select(func.count()).select_from(SensorReading).where(SensorReading.device_code == device_code)).scalar_one()
    )
    return {"code": 0, "data": data}


@router.post("", summary="新建设备（技术员及以上）")
def create_device(payload: DeviceCreate, db: DbSession, user: TechnicianUser) -> dict:
    if db.execute(select(Device).where(Device.device_code == payload.device_code)).scalar_one_or_none():
        raise HTTPException(status_code=409, detail="设备编码已存在")
    device = Device(**payload.model_dump(), status="offline")
    db.add(device)
    db.add(AuditLog(username=user.username, action="create_device", target=payload.device_code))
    db.commit()
    db.refresh(device)
    return {"code": 0, "data": _device_payload(device)}


@router.patch("/{device_code}", summary="更新设备（技术员及以上）")
def update_device(
    device_code: str,
    payload: DeviceUpdate,
    db: DbSession,
    user: TechnicianUser,
) -> dict:
    device = db.execute(select(Device).where(Device.device_code == device_code)).scalar_one_or_none()
    if device is None:
        raise HTTPException(status_code=404, detail="设备不存在")
    changes = payload.model_dump(exclude_none=True)
    for key, value in changes.items():
        setattr(device, key, value)
    db.add(AuditLog(username=user.username, action="update_device", target=device_code, detail=changes))
    db.commit()
    return {"code": 0, "data": _device_payload(device)}


@router.delete("/{device_code}", summary="删除设备（管理员）")
def delete_device(device_code: str, db: DbSession, user: AdminUser) -> dict:
    device = db.execute(select(Device).where(Device.device_code == device_code)).scalar_one_or_none()
    if device is None:
        raise HTTPException(status_code=404, detail="设备不存在")
    db.delete(device)
    db.add(AuditLog(username=user.username, action="delete_device", target=device_code))
    db.commit()
    return {"code": 0, "message": "设备已删除"}


@router.post("/{device_code}/heartbeat", summary="设备心跳上报（设备侧调用）")
def heartbeat(device_code: str, payload: HeartbeatIn, db: DbSession) -> dict:
    device = db.execute(select(Device).where(Device.device_code == device_code)).scalar_one_or_none()
    if device is None:
        raise HTTPException(status_code=404, detail="设备未注册")
    device.last_seen = datetime.now().replace(microsecond=0)
    device.status = payload.status
    if payload.battery is not None:
        device.battery = payload.battery
    if payload.signal is not None:
        device.signal = payload.signal
    if payload.solar_voltage is not None:
        device.solar_voltage = payload.solar_voltage
    if payload.firmware:
        device.firmware = payload.firmware
    db.commit()
    return {"code": 0, "message": "心跳已记录", "server_time": device.last_seen.isoformat(timespec="seconds")}


@router.post("/{device_code}/command", summary="下发设备指令（技术员及以上）")
def send_command(
    device_code: str,
    payload: DeviceCommand,
    db: DbSession,
    user: TechnicianUser,
) -> dict:
    device = db.execute(select(Device).where(Device.device_code == device_code)).scalar_one_or_none()
    if device is None:
        raise HTTPException(status_code=404, detail="设备不存在")

    applied: dict = {}
    if payload.command == "set_interval":
        seconds = int(payload.payload.get("sample_interval", device.sample_interval))
        seconds = max(30, min(3600, seconds))
        device.sample_interval = seconds
        applied["sample_interval"] = seconds
    elif payload.command == "upgrade_firmware":
        version = str(payload.payload.get("version") or device.firmware or "v1.0.0")
        device.firmware = version
        applied["firmware"] = version
    elif payload.command == "calibrate":
        config = dict(device.config or {})
        config["last_calibration"] = datetime.now().isoformat(timespec="seconds")
        device.config = config
        applied["last_calibration"] = config["last_calibration"]
    elif payload.command == "sync_time":
        applied["server_time"] = datetime.now().isoformat(timespec="seconds")
    elif payload.command == "reboot":
        device.status = "online"
        applied["reboot_at"] = datetime.now().isoformat(timespec="seconds")

    db.add(
        SystemEvent(
            category="device_command",
            level="info",
            message=f"下发指令 {payload.command}",
            detail={"device_code": device_code, "operator": user.username, **applied},
        )
    )
    db.add(
        AuditLog(username=user.username, action="device_command", target=device_code,
                 detail={"command": payload.command, **applied})
    )
    db.commit()
    return {"code": 0, "message": "指令已下发", "command": payload.command, "applied": applied}


@router.get("/{device_code}/events", summary="设备事件与状态时间线")
def device_events(device_code: str, db: DbSession, hours: int = 24) -> dict:
    since = datetime.now() - timedelta(hours=hours)
    events = db.execute(
        select(SystemEvent)
        .where(SystemEvent.category.in_(("device_command", "device_status", "ingest")))
        .where(SystemEvent.created_at >= since)
        .order_by(SystemEvent.id.desc())
        .limit(100)
    ).scalars().all()
    filtered = [e for e in events if (e.detail or {}).get("device_code") == device_code]
    return {
        "code": 0,
        "data": [
            {
                "id": e.id,
                "category": e.category,
                "level": e.level,
                "message": e.message,
                "detail": e.detail,
                "created_at": e.created_at.isoformat(timespec="seconds"),
            }
            for e in filtered
        ],
    }
