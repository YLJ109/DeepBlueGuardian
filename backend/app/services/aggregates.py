"""数据质量与聚合。

- `validate_reading`：量程校验、突变检测，输出质量标记
- `rebuild_aggregates`：生成 1m / 1h / 1d 三级聚合数据
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..config import METRICS
from ..models import Device, MetricAggregate, SensorReading

logger = logging.getLogger("deepblue.aggregates")

_OUT_OF_RANGE = 2
_SUSPECT = 1


def validate_reading(db: Session, device: Device, payload: dict) -> int:
    """返回质量标记：0 正常、1 可疑、2 无效。"""

    for metric, meta in METRICS.items():
        value = payload.get(metric)
        if value is None:
            continue
        low, high = meta["range"]
        if not (low <= value <= high):
            return _OUT_OF_RANGE

    last = db.execute(
        select(SensorReading)
        .where(SensorReading.device_code == device.device_code)
        .where(SensorReading.quality_flag == 0)
        .order_by(SensorReading.ts.desc())
        .limit(1)
    ).scalar_one_or_none()
    if last is not None:
        for metric, meta in METRICS.items():
            cur, prev = payload.get(metric), getattr(last, metric, None)
            if cur is None or prev is None:
                continue
            span = meta["range"][1] - meta["range"][0]
            if abs(cur - prev) > span * 0.35:
                return _SUSPECT
    return 0


def _bucket(dt: datetime, granularity: str) -> datetime:
    if granularity == "1m":
        return dt.replace(second=0, microsecond=0)
    if granularity == "1h":
        return dt.replace(minute=0, second=0, microsecond=0)
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def rebuild_aggregates(
    db: Session,
    since: datetime | None = None,
    granularities: tuple[str, ...] = ("1m", "1h", "1d"),
) -> dict[str, int]:
    """重算指定时间之后的聚合数据，返回每个粒度的记录数。"""

    since = since or (datetime.now() - timedelta(days=2))
    result: dict[str, int] = {}
    metrics = list(METRICS.keys())

    for granularity in granularities:
        db.execute(
            delete(MetricAggregate)
            .where(MetricAggregate.granularity == granularity)
            .where(MetricAggregate.bucket >= _bucket(since, granularity))
        )
        db.commit()

        rows = db.execute(
            select(SensorReading).where(SensorReading.ts >= since).where(SensorReading.quality_flag != 2)
        ).scalars().all()

        buckets: dict[tuple[str, str, datetime], list[float]] = {}
        for reading in rows:
            key_ts = _bucket(reading.ts, granularity)
            for metric in metrics:
                value = getattr(reading, metric, None)
                if value is None:
                    continue
                buckets.setdefault((reading.device_code, metric, key_ts), []).append(float(value))

        for (device_code, metric, bucket_ts), values in buckets.items():
            db.add(
                MetricAggregate(
                    device_code=device_code,
                    metric=metric,
                    granularity=granularity,
                    bucket=bucket_ts,
                    avg_value=sum(values) / len(values),
                    min_value=min(values),
                    max_value=max(values),
                    sample_count=len(values),
                )
            )
        db.commit()
        result[granularity] = len(buckets)
        logger.info("聚合完成 granularity=%s records=%s", granularity, len(buckets))
    return result


def cleanup_old_data(db: Session, keep_days: int = 180) -> int:
    """清理超期的原始数据（聚合数据保留）。"""

    cutoff = datetime.now() - timedelta(days=keep_days)
    deleted = db.execute(delete(SensorReading).where(SensorReading.ts < cutoff)).rowcount
    db.commit()
    return int(deleted or 0)


def data_volume(db: Session) -> dict[str, int]:
    return {
        "readings": int(db.execute(select(func.count()).select_from(SensorReading)).scalar() or 0),
        "aggregates": int(db.execute(select(func.count()).select_from(MetricAggregate)).scalar() or 0),
    }
