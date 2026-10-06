"""边缘设备数据仿真器（硬件联调数据源）。

当浮标尚未下水或现场通信中断时，平台需要有物理合理的数据流来验证
「采集 → 质量校验 → 告警 → 模型预测 → 大屏推送」的完整链路。本模块按
连江海域的真实海洋学规律生成数据，而非随机噪声：

1. 海表温度：年周期（8 月中旬峰值）+ 日周期 + 天气尺度扰动；
2. 盐度：年周期（夏季径流偏低）+ M2 半日潮周期（12.42 h）+ 站位差异；
3. 溶解氧：随温度反向变化的溶解度基线，叠加昼夜光合作用与藻华过饱和；
4. pH：受光合作用与藻华强度的日变化控制；
5. 浊度：潮汐与风浪驱动，河口站位叠加径流脉冲；
6. 叶绿素 a：春季藻华季节因子 + 站位营养指数 + 随机藻华事件。

所有随机过程均使用固定种子，保证同一时间段的数据可复现，便于回归测试。
"""

from __future__ import annotations

import logging
import math
import random
import threading
import time
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import METRICS, settings
from ..data.sites import SITES, SITE_BY_CODE, SiteSpec
from ..database import SessionLocal
from ..models import Device, SensorReading
from .aggregates import validate_reading
from .alerts import AlertEngine
from .notifier import bus

logger = logging.getLogger("deepblue.simulator")

M2_PERIOD_HOURS = 12.42  # 半日潮主要分潮
SPRING_BLOOM_DOY = 140  # 4—6 月为福建沿海赤潮高发期，取 5 月中旬为中心
BLOOM_SPREAD_DOY = 46


# ---------------------------------------------------------------------------
# 单站物理模型
# ---------------------------------------------------------------------------
class SiteModel:
    """按站位参数生成物理合理的环境量。"""

    def __init__(self, site: SiteSpec, seed: int | None = None) -> None:
        self.site = site
        self.rng = random.Random(seed if seed is not None else hash(site.code) & 0xFFFF)
        self.phase = self.rng.random()
        self._temp_noise = 0.0
        self._wind_noise = 0.3
        self.rain_factor = 1.0

    # -- 随机游走项：模拟天气尺度扰动 ---------------------------------
    def _walk(self, current: float, sigma: float, limit: float) -> float:
        current = current * 0.94 + self.rng.gauss(0.0, sigma)
        return max(-limit, min(limit, current))

    def step_weather(self) -> None:
        self._temp_noise = self._walk(self._temp_noise, 0.28, 2.6)
        self._wind_noise = self._walk(self._wind_noise, 0.16, 1.6)
        self.rain_factor = self._walk(self.rain_factor - 1.0, 0.05, 0.9) + 1.0

    # -- 潮位（0—1，0 为低平潮，1 为高平潮） --------------------------
    def tide(self, ts: datetime) -> float:
        hours = (ts - datetime(2026, 1, 1)).total_seconds() / 3600.0
        ang = 2 * math.pi * (hours / M2_PERIOD_HOURS + self.phase)
        return 0.5 * (1.0 - math.cos(ang))

    # -- 季节性 -------------------------------------------------------
    def _season(self, ts: datetime, peak_doy: float, amp: float, mean: float) -> float:
        doy = ts.timetuple().tm_yday
        return mean + amp * math.cos(2 * math.pi * (doy - peak_doy) / 365.25)

    def sample(self, ts: datetime, bloom: float = 0.0) -> dict:
        """输出某一时刻的六项水质参数。"""

        site = self.site
        tide = self.tide(ts)
        hour = ts.hour + ts.minute / 60.0
        daylight = max(0.0, math.sin(math.pi * (hour - 6.0) / 12.0)) if 6.0 <= hour <= 18.0 else 0.0

        # 水温：年周期峰值在 8 月中旬（doy 227）
        temp = self._season(ts, 227, site.temp_amp, site.temp_base)
        temp += 0.55 * math.sin(math.pi * (hour - 9.0) / 12.0) + self._temp_noise
        temp += bloom * 0.9  # 藻华期间表层增温
        temp = max(-2.0, min(35.0, temp))

        # 盐度：夏季径流偏低；潮汐使河口站位盐度显著起伏
        salinity = self._season(ts, 130, site.salinity_amp, site.salinity_base)
        salinity -= (self.rain_factor - 1.0) * site.salinity_amp * 1.4
        salinity += (tide - 0.5) * site.salinity_amp * 0.85
        salinity = max(0.0, min(40.0, salinity))

        # 溶解氧：温度越高溶解度越低；藻华期白天过饱和、夜间亏缺
        do_sat = 14.62 - 0.3898 * temp + 0.0055 * temp * temp
        dox = site.do_base + (do_sat - 9.0) * 0.55
        dox += daylight * 0.55 * (1.0 + bloom * 2.4)
        dox -= (1.0 - daylight) * 0.30 * (0.4 + bloom)
        dox += (tide - 0.5) * 0.16
        dox = max(0.0, min(20.0, dox))

        # pH：光合作用消耗 CO2 使 pH 升高
        ph = 8.08 + daylight * 0.09 + bloom * (0.28 + 0.14 * daylight)
        ph -= (self.rain_factor - 1.0) * 0.10
        ph = max(5.0, min(10.0, ph))

        # 浊度：潮汐再悬浮 + 风浪 + 降雨冲刷
        turbidity = site.turbidity_base
        turbidity += site.turbidity_amp * (0.55 * abs(math.sin(math.pi * tide)) + 0.35 * self._wind_noise)
        turbidity += (self.rain_factor - 1.0) * site.turbidity_amp * 1.2
        turbidity = max(0.0, min(1000.0, turbidity))

        # 叶绿素 a：季节因子 + 营养本底 + 藻华事件
        bloom_season = math.exp(-((ts.timetuple().tm_yday - SPRING_BLOOM_DOY) ** 2) / (2 * BLOOM_SPREAD_DOY ** 2))
        chl = site.chl_base * (1.0 + 0.85 * bloom_season * (0.5 + site.nutrient_index))
        chl += site.chl_base * 0.28 * math.sin(2 * math.pi * (hour / 24.0) + self.phase)
        chl *= 1.0 + bloom * (5.0 + 6.0 * site.nutrient_index)
        chl = max(0.0, min(200.0, chl))

        return {
            "water_temp": round(temp, 2),
            "ph": round(ph, 2),
            "dissolved_oxygen": round(dox, 2),
            "turbidity": round(turbidity, 2),
            "chlorophyll_a": round(chl, 2),
            "salinity": round(salinity, 2),
        }


# ---------------------------------------------------------------------------
# 序列生成
# ---------------------------------------------------------------------------
def simulate_series(
    site: SiteSpec,
    start: datetime,
    end: datetime,
    step_minutes: int = 10,
    seed: int | None = None,
) -> list[dict]:
    """生成 [start, end) 区间的连续观测序列（不含藻华事件注入）。"""

    model = SiteModel(site, seed=seed)
    step = timedelta(minutes=step_minutes)
    rows: list[dict] = []
    ts = start
    while ts < end:
        model.step_weather()
        row = model.sample(ts)
        row["ts"] = ts
        rows.append(row)
        ts += step
    return rows


def inject_bloom_events(rows: list[dict], site: SiteSpec, rng: random.Random | None = None) -> list[dict]:
    """在序列中注入符合季节规律的藻华过程。

    福建沿海赤潮高发期为 4—6 月（业务高发监测期 3—10 月），因此事件强度
    随季节权重抽样，事件持续 3—9 天，叶绿素 a 呈快升缓降形态。
    """

    rng = rng or random.Random(hash(site.code) ^ 0x5EED)
    if not rows:
        return rows
    n_events = 1 if site.nutrient_index > 0.55 else (1 if rng.random() < 0.45 else 0)
    for _ in range(n_events):
        weight = [1.0 + 6.0 * math.exp(-((r["ts"].timetuple().tm_yday - SPRING_BLOOM_DOY) ** 2)
                                       / (2 * BLOOM_SPREAD_DOY ** 2)) for r in rows]
        total = sum(weight)
        pick = rng.random() * total
        acc = 0.0
        start_idx = 0
        for i, w in enumerate(weight):
            acc += w
            if acc >= pick:
                start_idx = i
                break
        duration = rng.randint(12, 26)
        peak = 0.55 + 0.45 * site.nutrient_index + rng.random() * 0.25
        for offset in range(duration):
            idx = start_idx + offset
            if idx >= len(rows):
                break
            progress = offset / max(1, duration - 1)
            shape = math.sin(math.pi * progress) ** 0.8
            rows[idx]["chlorophyll_a"] = round(
                min(200.0, rows[idx]["chlorophyll_a"] * (1.0 + peak * shape * 6.0)), 2
            )
            rows[idx]["ph"] = round(min(10.0, rows[idx]["ph"] + peak * shape * 0.55), 2)
            rows[idx]["dissolved_oxygen"] = round(
                min(20.0, rows[idx]["dissolved_oxygen"] + peak * shape * 2.1), 2
            )
            rows[idx]["turbidity"] = round(rows[idx]["turbidity"] * (1.0 + 0.25 * shape), 2)
    return rows


# ---------------------------------------------------------------------------
# 历史回填
# ---------------------------------------------------------------------------
def backfill_history(db: Session, days: int | None = None, step_minutes: int = 10) -> dict[str, int]:
    """为所有浮标站点回填历史数据，返回写入统计。"""

    days = days or settings.simulator_history_days
    end = datetime.now().replace(second=0, microsecond=0)
    start = end - timedelta(days=days)
    created = 0
    skipped = 0

    devices = db.execute(
        select(Device).where(Device.type == "buoy").order_by(Device.device_code)
    ).scalars().all()

    for device in devices:
        site = SITE_BY_CODE.get(device.device_code) or _match_site(device.device_code)
        if site is None:
            continue
        exists = db.execute(
            select(SensorReading.id)
            .where(SensorReading.device_code == device.device_code)
            .where(SensorReading.ts >= start)
            .limit(1)
        ).scalar_one_or_none()
        if exists is not None:
            skipped += 1
            continue

        rows = simulate_series(site, start, end, step_minutes=step_minutes, seed=hash(device.device_code) & 0xFFFF)
        rows = inject_bloom_events(rows, site)
        battery = 100.0
        for row in rows:
            battery = max(38.0, min(100.0, battery - 0.004 + (0.02 if 8 <= row["ts"].hour <= 16 else -0.01)))
            quality = validate_reading(db, device, row)
            db.add(
                SensorReading(
                    device_code=device.device_code,
                    ts=row["ts"],
                    water_temp=row["water_temp"],
                    ph=row["ph"],
                    dissolved_oxygen=row["dissolved_oxygen"],
                    turbidity=row["turbidity"],
                    chlorophyll_a=row["chlorophyll_a"],
                    salinity=row["salinity"],
                    quality_flag=quality,
                    battery=round(battery, 1),
                    signal=-(58 + int(14 * abs(math.sin(row["ts"].timestamp() / 5400)))),
                    source="sim",
                )
            )
            created += 1
        device.last_seen = end
        device.status = "online"
        device.battery = round(battery, 1)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
    return {"devices": len(devices), "readings": created, "skipped": skipped}


def _match_site(device_code: str) -> SiteSpec | None:
    """按设备编码前缀匹配站点（摄像头/网关共用浮标站点的环境参数）。"""

    for site in SITES:
        if device_code.startswith(site.code):
            return site
    return None


# ---------------------------------------------------------------------------
# 实时数据流线程
# ---------------------------------------------------------------------------
class SimulatorWorker(threading.Thread):
    """按固定间隔写入实时数据，并触发告警与 WebSocket 推送。"""

    def __init__(self, interval: int | None = None) -> None:
        super().__init__(name="deepblue-simulator", daemon=True)
        self.interval = interval or settings.simulator_interval_seconds
        self._stop = threading.Event()
        self.models: dict[str, SiteModel] = {}
        self.ticks = 0

    def stop(self) -> None:
        self._stop.set()

    def _model_for(self, device_code: str, site: SiteSpec) -> SiteModel:
        if device_code not in self.models:
            self.models[device_code] = SiteModel(site, seed=hash(device_code) & 0xFFFF)
        return self.models[device_code]

    def run(self) -> None:  # pragma: no cover - 后台线程
        db = SessionLocal()
        try:
            while not self._stop.is_set():
                try:
                    self._tick(db)
                except Exception as exc:
                    logger.exception("仿真器单次采样失败: %s", exc)
                    db.rollback()
                self._stop.wait(self.interval)
        finally:
            db.close()

    def _tick(self, db: Session) -> None:
        now = datetime.now().replace(microsecond=0)
        devices = db.execute(select(Device).where(Device.type == "buoy")).scalars().all()
        engine = AlertEngine(db)
        payloads: list[dict] = []

        for device in devices:
            site = SITE_BY_CODE.get(device.device_code) or _match_site(device.device_code)
            if site is None:
                continue
            model = self._model_for(device.device_code, site)
            model.step_weather()
            values = model.sample(now, bloom=_bloom_intensity(site, now, self.ticks))
            quality = validate_reading(db, device, values)
            reading = SensorReading(
                device_code=device.device_code,
                ts=now,
                quality_flag=quality,
                battery=device.battery,
                signal=device.signal,
                source="sim",
                **values,
            )
            db.add(reading)

            # 太阳能浮标电源：白天回充、夜间放电，长期维持在 45%~100% 之间。
            # 同一时刻各设备必须呈现个体差异，避免台账出现一模一样的电量/信号。
            charging = 8 <= now.hour <= 16
            level = (device.battery if device.battery is not None else 94.0) + (0.004 if charging else -0.006)
            device.battery = round(min(100.0, max(45.0, level)), 1)
            device.solar_voltage = round((13.9 if charging else 12.1) + (device.id % 5) * 0.06, 2)
            device.signal = -(
                57 + (device.id * 5) % 15 + int(4 * abs(math.sin(now.timestamp() / 3600.0 + device.id)))
            )
            device.last_seen = now - timedelta(seconds=(device.id * 7) % 45)
            device.status = "online"
            db.flush()

            alerts = engine.evaluate_reading(device, reading)
            payloads.append(
                {
                    "device_code": device.device_code,
                    "device_name": device.name,
                    "region_code": device.region_code,
                    "ts": now.isoformat(timespec="seconds"),
                    "values": values,
                    "quality_flag": quality,
                    "alerts": len(alerts),
                }
            )

        db.commit()
        self.ticks += 1
        bus.broadcast(
            "reading",
            {"ts": now.isoformat(timespec="seconds"), "items": payloads, "source": "sim"},
        )


def _bloom_intensity(site: SiteSpec, ts: datetime, tick: int) -> float:
    """实时藻华强度：按季节权重与站位营养指数做低频扰动。"""

    season = math.exp(-((ts.timetuple().tm_yday - SPRING_BLOOM_DOY) ** 2) / (2 * BLOOM_SPREAD_DOY ** 2))
    if season < 0.28:
        return 0.0
    wave = 0.5 + 0.5 * math.sin(tick / 47.0 + site.nutrient_index * 6.28)
    intensity = season * site.nutrient_index * wave * 1.35
    return round(max(0.0, min(1.0, intensity - 0.16)), 3)


def metric_snapshot(values: dict) -> dict:
    """把仿真输出转成带单位与标签的快照，供接口直接返回。"""

    return {
        key: {
            "value": values.get(key),
            "label": METRICS[key]["label"],
            "unit": METRICS[key]["unit"],
        }
        for key in METRICS
    }


__all__ = [
    "SiteModel",
    "SimulatorWorker",
    "backfill_history",
    "inject_bloom_events",
    "metric_snapshot",
    "simulate_series",
]
