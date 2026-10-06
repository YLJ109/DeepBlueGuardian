"""数据接入接口：单条/批量上报、设备签名校验、质量校验、告警触发。"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from ..deps import DbSession
from ..models import Device, SensorReading, SystemEvent
from ..schemas import BatchIngestRequest, ReadingIn, ReadingOut
from ..security import verify_device_signature
from ..services.aggregates import validate_reading
from ..services.alerts import AlertEngine
from ..services.notifier import bus

logger = logging.getLogger("deepblue.ingest")

router = APIRouter(prefix="/ingest", tags=["数据接入"])

METRIC_FIELDS = ("water_temp", "ph", "dissolved_oxygen", "turbidity", "chlorophyll_a", "salinity")


def _store(db, device: Device, item: ReadingIn) -> tuple[SensorReading, int]:
    values = {field: getattr(item, field) for field in METRIC_FIELDS}
    quality = validate_reading(db, device, values)

    existing = db.execute(
        select(SensorReading)
        .where(SensorReading.device_code == item.device_code)
        .where(SensorReading.ts == item.ts)
    ).scalar_one_or_none()

    if existing is not None:
        for field, value in values.items():
            if value is not None:
                setattr(existing, field, value)
        existing.quality_flag = quality
        existing.battery = item.battery
        existing.signal = item.signal
        existing.source = item.source
        existing.received_at = datetime.now().replace(microsecond=0)
        reading = existing
    else:
        reading = SensorReading(
            device_code=item.device_code,
            ts=item.ts,
            quality_flag=quality,
            battery=item.battery,
            signal=item.signal,
            source=item.source,
            **values,
        )
        db.add(reading)

    device.last_seen = item.ts
    device.status = "online"
    if item.battery is not None:
        device.battery = item.battery
    if item.signal is not None:
        device.signal = item.signal
    db.flush()
    return reading, quality


@router.post("/reading", summary="单条观测数据上报")
def ingest_reading(payload: ReadingIn, db: DbSession) -> dict:
    device = db.execute(select(Device).where(Device.device_code == payload.device_code)).scalar_one_or_none()
    if device is None:
        raise HTTPException(status_code=404, detail=f"设备 {payload.device_code} 未注册")
    if payload.ts > datetime.now() + timedelta(minutes=10):
        raise HTTPException(status_code=400, detail="上报时间超出允许的时钟偏移范围")

    reading, quality = _store(db, device, payload)
    alerts = AlertEngine(db).evaluate_reading(device, reading)
    db.commit()
    db.refresh(reading)

    bus.broadcast(
        "reading",
        {
            "ts": reading.ts.isoformat(timespec="seconds"),
            "source": reading.source,
            "items": [
                {
                    "device_code": reading.device_code,
                    "device_name": device.name,
                    "region_code": device.region_code,
                    "ts": reading.ts.isoformat(timespec="seconds"),
                    "values": {field: getattr(reading, field) for field in METRIC_FIELDS},
                    "quality_flag": quality,
                    "alerts": len(alerts),
                }
            ],
        },
    )
    return {
        "code": 0,
        "message": "已接收",
        "quality_flag": quality,
        "alerts_created": len(alerts),
        "data": ReadingOut.model_validate(reading).model_dump(mode="json"),
    }


@router.post("/batch", summary="批量观测数据上报（支持设备签名）")
def ingest_batch(payload: BatchIngestRequest, db: DbSession) -> dict:
    if not payload.items:
        raise HTTPException(status_code=400, detail="items 不能为空")
    if len(payload.items) > 2000:
        raise HTTPException(status_code=413, detail="单批次最多 2000 条")

    if payload.signature:
        code = payload.items[0].device_code
        if not verify_device_signature(code, payload.ts or 0, payload.nonce or "", payload.signature):
            raise HTTPException(status_code=401, detail="设备签名校验失败")

    devices = {
        d.device_code: d
        for d in db.execute(
            select(Device).where(Device.device_code.in_({i.device_code for i in payload.items}))
        ).scalars().all()
    }
    missing = sorted({i.device_code for i in payload.items} - set(devices))
    if missing:
        raise HTTPException(status_code=404, detail=f"以下设备未注册：{', '.join(missing[:5])}")

    engine = AlertEngine(db)
    accepted = 0
    invalid = 0
    alert_count = 0
    latest: dict[str, dict] = {}

    for item in payload.items:
        device = devices[item.device_code]
        reading, quality = _store(db, device, item)
        if quality == 2:
            invalid += 1
        else:
            accepted += 1
            alert_count += len(engine.evaluate_reading(device, reading))
        latest[item.device_code] = {
            "device_code": item.device_code,
            "device_name": device.name,
            "region_code": device.region_code,
            "ts": item.ts.isoformat(timespec="seconds"),
            "values": {field: getattr(reading, field) for field in METRIC_FIELDS},
            "quality_flag": quality,
        }

    db.add(
        SystemEvent(
            category="ingest",
            level="info",
            message=f"批量接入 {len(payload.items)} 条观测数据",
            detail={"devices": len(devices), "invalid": invalid, "alerts": alert_count},
        )
    )
    db.commit()

    bus.broadcast(
        "reading",
        {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "source": payload.items[0].source,
            "items": list(latest.values()),
        },
    )
    return {
        "code": 0,
        "message": "批量接收完成",
        "received": len(payload.items),
        "accepted": accepted,
        "invalid": invalid,
        "alerts_created": alert_count,
    }


@router.post("/simulate", summary="触发一次仿真采样（无硬件时的链路自检）")
def trigger_simulation(db: DbSession, ticks: int = 1) -> dict:
    from ..services.simulator import SimulatorWorker

    worker = SimulatorWorker()
    for _ in range(max(1, min(ticks, 60))):
        worker._tick(db)
    return {"code": 0, "message": f"已触发 {ticks} 次仿真采样", "ticks": worker.ticks}
