"""红树林遥感监测接口：期次统计、健康度趋势、分割演示、变化检测。"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from ..data.sites import MANGROVE_BASELINE, REGION_BY_CODE
from ..deps import DbSession
from ..ml.mangrove import MangroveAnalyzer, generate_scene, masks_to_png_uri
from ..models import MangroveStat

router = APIRouter(prefix="/mangrove", tags=["红树林监测"])

MANGROVE_REGIONS = ("LJ-LYW", "LJ-MB", "LJ-PK")


def _period_sequence(periods: int = 6) -> list[str]:
    """按季度生成期次标签，默认回溯 6 个季度。"""

    now = datetime.now()
    year, quarter = now.year, (now.month - 1) // 3 + 1
    out: list[str] = []
    for _ in range(periods):
        out.append(f"{year}Q{quarter}")
        quarter -= 1
        if quarter == 0:
            quarter = 4
            year -= 1
    return list(reversed(out))


@router.get("/regions", summary="红树林监测区域与最新统计")
def regions(db: DbSession) -> dict:
    rows = db.execute(select(MangroveStat).order_by(MangroveStat.region_code, MangroveStat.period)).scalars().all()
    grouped: dict[str, list[MangroveStat]] = {}
    for row in rows:
        grouped.setdefault(row.region_code, []).append(row)

    data = []
    for code in MANGROVE_REGIONS:
        items = grouped.get(code, [])
        latest = items[-1] if items else None
        region = REGION_BY_CODE.get(code)
        series = MangroveAnalyzer.health_series(latest.health_score if latest else 78.0, periods=6, seed=hash(code) & 0xFF)
        if latest:
            series[-1] = {"period": latest.period, "score": latest.health_score}
        data.append(
            {
                "region_code": code,
                "region_name": region.name if region else code,
                "kind": region.kind if region else "mangrove",
                "longitude": region.longitude if region else None,
                "latitude": region.latitude if region else None,
                "world_x": region.world[0] if region else None,
                "world_z": region.world[1] if region else None,
                "area_ha": latest.area_ha if latest else 0.0,
                "change_pct": latest.change_pct if latest else None,
                "health_score": latest.health_score if latest else None,
                "health_level": latest.health_level if latest else None,
                "ndvi_mean": latest.ndvi_mean if latest else None,
                "ndwi_mean": latest.ndwi_mean if latest else None,
                "data_source": latest.data_source if latest else None,
                "period": latest.period if latest else None,
                "remark": latest.remark if latest else None,
                "health_series": series,
                "history": [
                    {
                        "period": r.period,
                        "area_ha": r.area_ha,
                        "change_pct": r.change_pct,
                        "health_score": r.health_score,
                        "health_level": r.health_level,
                    }
                    for r in items
                ],
            }
        )
    return {"code": 0, "data": data, "periods": _period_sequence()}


@router.get("/stats", summary="红树林汇总统计")
def stats(db: DbSession) -> dict:
    rows = db.execute(select(MangroveStat).order_by(MangroveStat.region_code, MangroveStat.period)).scalars().all()
    latest: dict[str, MangroveStat] = {}
    for row in rows:
        latest[row.region_code] = row
    total_area = sum(r.area_ha for r in latest.values())
    weighted_health = (
        sum(r.health_score * r.area_ha for r in latest.values()) / total_area if total_area else 0.0
    )
    return {
        "code": 0,
        "data": {
            "region_count": len(latest),
            "total_area_ha": round(total_area, 2),
            "average_health": round(weighted_health, 2),
            "improved_regions": sum(1 for r in latest.values() if (r.change_pct or 0) > 0),
            "declined_regions": sum(1 for r in latest.values() if (r.change_pct or 0) < 0),
            "species": ["秋茄", "桐花树", "白骨壤", "木榄"],
            "birds": ["黑脸琵鹭", "勺嘴鹬", "黑嘴鸥"],
            "caveat": "面积与健康度按期次由遥感影像计算，未更新期次时沿用最近一次结果。",
            "items": [
                {
                    "region_code": r.region_code,
                    "region_name": REGION_BY_CODE[r.region_code].name if r.region_code in REGION_BY_CODE else r.region_code,
                    "area_ha": r.area_ha,
                    "change_pct": r.change_pct,
                    "health_score": r.health_score,
                    "health_level": r.health_level,
                    "period": r.period,
                }
                for r in latest.values()
            ],
        },
    }


@router.get("/analysis", summary="执行一次遥感分割与变化检测")
def analysis(region_code: str = "LJ-LYW", size: int = 256, bloom: float = 0.35) -> dict:
    """对指定区域运行一次完整的分割流程。

    未接入真实卫星影像时使用合成多光谱场景验证算法链路；接入真实数据后，
    只需把 `generate_scene` 换成从 Sentinel-2 / 无人机正射影像读取的波段矩阵。
    """

    if region_code not in MANGROVE_REGIONS:
        raise HTTPException(status_code=404, detail="该区域未纳入红树林监测范围")

    size = max(96, min(512, size))
    before_scene = generate_scene((size, size), seed=11, bloom=0.0, name="synthetic-before")
    after_scene = generate_scene((size, size), seed=11, bloom=bloom, name="synthetic-after")

    analyzer = MangroveAnalyzer(gsd_meters=10.0)
    before = analyzer.segment(before_scene)
    after = analyzer.segment(after_scene)
    change = analyzer.change_detection(before, after)

    baseline = next((b for b in MANGROVE_BASELINE if b["region_code"] == region_code), None)
    scale = (baseline["area_ha"] / after.area_ha) if baseline and after.area_ha > 0 else None

    return {
        "code": 0,
        "region_code": region_code,
        "region_name": REGION_BY_CODE[region_code].name if region_code in REGION_BY_CODE else region_code,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "synthetic-scene",
        "before": {
            "area_ha": before.area_ha,
            "pixel_count": before.pixel_count,
            "health_score": before.health_score,
            "health_level": before.health_level,
            "stats": before.stats,
        },
        "after": {
            "area_ha": after.area_ha,
            "pixel_count": after.pixel_count,
            "health_score": after.health_score,
            "health_level": after.health_level,
            "stats": after.stats,
        },
        "change": {
            "change_ha": change["change_ha"],
            "change_pct": change["change_pct"],
            "gain_ha": change["gain_ha"],
            "loss_ha": change["loss_ha"],
            "health_change": change["health_change"],
        },
        "area_calibration": {
            "reference_area_ha": baseline["area_ha"] if baseline else None,
            "scale_factor": round(scale, 4) if scale else None,
            "note": "合成场景面积按参考区实际公顷数线性标定，接入真实影像后直接以像元面积统计。",
        },
        "overlays": {
            "before_mask": masks_to_png_uri(before.mask, color=(51, 225, 255)),
            "after_mask": masks_to_png_uri(after.mask, color=(41, 231, 160)),
            "gain_mask": masks_to_png_uri(change["gain_mask"], color=(41, 231, 160)),
            "loss_mask": masks_to_png_uri(change["loss_mask"], color=(255, 77, 109)),
        },
    }


@router.get("/health-trend", summary="全域红树林健康度趋势")
def health_trend(periods: int = 6) -> dict:
    labels = _period_sequence(periods)
    series = []
    for code in MANGROVE_REGIONS:
        baseline = next((b for b in MANGROVE_BASELINE if b["region_code"] == code), None)
        if baseline is None:
            continue
        points = MangroveAnalyzer.health_series(baseline["health_score"], periods=periods, seed=hash(code) & 0xFF)
        series.append(
            {
                "region_code": code,
                "region_name": REGION_BY_CODE[code].name if code in REGION_BY_CODE else code,
                "values": [p["score"] for p in points],
            }
        )
    return {"code": 0, "periods": labels, "series": series}
