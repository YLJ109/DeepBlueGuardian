"""红树林遥感分割与健康度评估。

流程：多光谱影像 → 水体/植被指数 → 阈值分割 → 形态学后处理
      → 面积统计 → 健康度评分 → 两期变化检测。

支持真实 Sentinel-2 / 无人机多光谱数据（传入波段矩阵），
也内置合成场景生成器，便于在没有卫星数据时验证全流程。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np
from scipy import ndimage


@dataclass
class MangroveScene:
    """多光谱场景。

    bands 包含：blue / green / red / red_edge / nir / swir，取值 0—1（已做反射率归一化）。
    """

    bands: dict[str, np.ndarray]
    gsd_meters: float = 10.0
    name: str = "scene"
    reference_mask: np.ndarray | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def shape(self) -> tuple[int, int]:
        return self.bands["red"].shape


@dataclass
class SegmentationResult:
    mask: np.ndarray
    indices: dict[str, np.ndarray]
    area_ha: float
    pixel_count: int
    health_score: float
    health_level: str
    stats: dict[str, Any]


def _nd(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    denom = a + b
    denom = np.where(np.abs(denom) < 1e-6, 1e-6, denom)
    return (a - b) / denom


class MangroveAnalyzer:
    """红树林分割与健康度评估器。"""

    # 阈值基于福建省主要红树植物（秋茄、桐花树、白骨壤）在 Sentinel-2 上的
    # 典型光谱特征经验区间，实际部署时可用现场标注样本重新标定。
    NDVI_THRESHOLD = 0.28
    NDWI_THRESHOLD = -0.10
    MNDWI_THRESHOLD = 0.05
    MIN_PATCH_PIXELS = 12

    def __init__(self, gsd_meters: float = 10.0) -> None:
        self.gsd_meters = gsd_meters

    # ------------------------------------------------------------------
    # 指数计算
    # ------------------------------------------------------------------
    def compute_indices(self, scene: MangroveScene) -> dict[str, np.ndarray]:
        b = scene.bands
        nir = b["nir"]
        red = b["red"]
        green = b["green"]
        red_edge = b.get("red_edge", red)
        swir = b.get("swir", green)

        ndvi = _nd(nir, red)
        ndwi = _nd(green, nir)
        mndwi = _nd(green, swir)
        # 红树林专用指数：近红外与红边协同，抑制陆生植被与水体
        cmri = _nd(nir - red, nir + red_edge + 1e-6)
        return {"ndvi": ndvi, "ndwi": ndwi, "mndwi": mndwi, "cmri": cmri}

    # ------------------------------------------------------------------
    # 分割
    # ------------------------------------------------------------------
    def segment(self, scene: MangroveScene, min_patch_pixels: int | None = None) -> SegmentationResult:
        idx = self.compute_indices(scene)
        ndvi, ndwi, mndwi, cmri = idx["ndvi"], idx["ndwi"], idx["mndwi"], idx["cmri"]

        water = (ndwi > self.NDWI_THRESHOLD) | (mndwi > self.MNDWI_THRESHOLD)
        vegetation = ndvi > self.NDVI_THRESHOLD
        candidate = vegetation & (~water)
        # 红树林偏向潮间带：紧邻水体且位于水体边缘带上
        near_water = ndimage.binary_dilation(water, iterations=6)
        mask = candidate & near_water & (cmri > np.percentile(cmri, 55))

        mask = ndimage.binary_opening(mask, iterations=1)
        mask = ndimage.binary_closing(mask, iterations=2)
        min_pixels = min_patch_pixels if min_patch_pixels is not None else self.MIN_PATCH_PIXELS
        mask = self._remove_small_patches(mask, min_pixels)

        pixel_area_m2 = self.gsd_meters * self.gsd_meters
        pixel_count = int(mask.sum())
        area_ha = pixel_count * pixel_area_m2 / 10_000.0

        health_score, stats = self._health(mask, idx, scene)
        level = self.health_level(health_score)
        return SegmentationResult(
            mask=mask,
            indices=idx,
            area_ha=round(area_ha, 4),
            pixel_count=pixel_count,
            health_score=round(health_score, 2),
            health_level=level,
            stats=stats,
        )

    @staticmethod
    def _remove_small_patches(mask: np.ndarray, min_pixels: int) -> np.ndarray:
        labeled, count = ndimage.label(mask)
        if count == 0:
            return mask
        sizes = ndimage.sum(mask, labeled, range(1, count + 1))
        keep = {i + 1 for i, size in enumerate(sizes) if size >= min_pixels}
        return np.isin(labeled, list(keep)) if keep else np.zeros_like(mask, dtype=bool)

    def _health(self, mask: np.ndarray, indices: dict[str, np.ndarray], scene: MangroveScene) -> tuple[float, dict[str, Any]]:
        if not mask.any():
            return 0.0, {"ndvi_mean": 0.0, "ndvi_std": 0.0, "edge_ratio": 0.0}
        ndvi = indices["ndvi"][mask]
        ndvi_mean = float(np.mean(ndvi))
        ndvi_std = float(np.std(ndvi))

        # 边缘破碎度：破碎度越高，退化风险越大
        eroded = ndimage.binary_erosion(mask, iterations=1)
        edge_ratio = float((mask.sum() - eroded.sum()) / max(mask.sum(), 1))

        # 归一化到 0—100：长势（0—70）+ 均一度（0—20）+ 连通性（0—10）
        vigor = np.clip((ndvi_mean - 0.20) / 0.55, 0, 1) * 70
        uniformity = np.clip(1 - ndvi_std / 0.25, 0, 1) * 20
        connectivity = np.clip(1 - edge_ratio / 0.35, 0, 1) * 10
        score = float(np.clip(vigor + uniformity + connectivity, 0, 100))
        stats = {
            "ndvi_mean": round(ndvi_mean, 4),
            "ndvi_std": round(ndvi_std, 4),
            "edge_ratio": round(edge_ratio, 4),
            "vigor": round(float(vigor), 2),
            "uniformity": round(float(uniformity), 2),
            "connectivity": round(float(connectivity), 2),
        }
        return score, stats

    @staticmethod
    def health_level(score: float) -> str:
        if score >= 80:
            return "优"
        if score >= 65:
            return "良"
        if score >= 50:
            return "中"
        return "差"

    # ------------------------------------------------------------------
    # 变化检测
    # ------------------------------------------------------------------
    def change_detection(self, before: SegmentationResult, after: SegmentationResult) -> dict[str, Any]:
        before_mask = before.mask
        after_mask = after.mask
        if before_mask.shape != after_mask.shape:
            raise ValueError("两期影像尺寸不一致，请先重采样到同一网格")
        gained = after_mask & (~before_mask)
        lost = before_mask & (~after_mask)
        pixel_area_ha = self.gsd_meters * self.gsd_meters / 10_000.0
        return {
            "before_area_ha": round(before.area_ha, 4),
            "after_area_ha": round(after.area_ha, 4),
            "change_ha": round(after.area_ha - before.area_ha, 4),
            "change_pct": round((after.area_ha - before.area_ha) / max(before.area_ha, 1e-6) * 100, 2),
            "gain_ha": round(float(gained.sum()) * pixel_area_ha, 4),
            "loss_ha": round(float(lost.sum()) * pixel_area_ha, 4),
            "health_change": round(after.health_score - before.health_score, 2),
            "gain_mask": gained,
            "loss_mask": lost,
        }

    @staticmethod
    def health_series(base_score: float, periods: int = 6, seed: int = 42) -> list[dict[str, Any]]:
        """生成连续期次的健康度序列（真实部署时由历史影像逐期计算）。"""

        rng = np.random.default_rng(seed)
        out: list[dict[str, Any]] = []
        value = base_score - 6
        for i in range(periods):
            value = float(np.clip(value + rng.normal(1.2, 0.8), 40, 95))
            out.append({"period": f"P{i + 1}", "score": round(value, 1)})
        return out


# ---------------------------------------------------------------------------
# 合成场景：用于无真实卫星数据时的全流程验证与演示
# ---------------------------------------------------------------------------
def generate_scene(
    size: tuple[int, int] = (256, 256),
    gsd_meters: float = 10.0,
    seed: int = 7,
    bloom: float = 0.0,
    name: str = "synthetic",
) -> MangroveScene:
    """生成包含陆地和潮间带红树林的合成多光谱影像。

    `bloom` 控制植被长势（0 = 正常，1 = 生长旺盛），用于验证变化检测。
    """

    rng = np.random.default_rng(seed)
    h, w = size
    yy, xx = np.mgrid[0:h, 0:w]
    coast = 0.42 + 0.11 * np.sin(xx / 22.0) + 0.06 * np.cos(xx / 9.0)
    land = (yy / h) < coast
    intertidal = land & ((yy / h) > coast - 0.16)

    mangrove = np.zeros((h, w), dtype=bool)
    if name == "synthetic-after":
        blobs = [(0.30, 0.30, 26), (0.60, 0.26, 20), (0.44, 0.36, 30), (0.72, 0.38, 18)]
    else:
        blobs = [(0.30, 0.30, 24), (0.60, 0.26, 17), (0.44, 0.36, 27), (0.72, 0.38, 13)]
    for cx, cy, r in blobs:
        mangrove |= ((xx - cx * w) ** 2 + (yy - cy * h) ** 2) < (r + bloom * 4) ** 2
    mangrove &= intertidal
    mangrove = ndimage.binary_closing(mangrove, iterations=2)

    water = ~land
    soil = land & (~mangrove)
    urban = soil & (np.random.random((h, w)) < 0.06)

    def band(water_v, soil_v, veg_v, mangrove_v, noise=0.012):
        arr = np.full((h, w), soil_v, dtype=float)
        arr[water] = water_v
        arr[mangrove] = mangrove_v * (0.9 + bloom * 0.25)
        arr[urban] = soil_v * 0.75
        arr = arr + rng.normal(0, noise, (h, w))
        return np.clip(arr, 0.0, 1.0)

    bands = {
        "blue": band(0.09, 0.12, 0.06, 0.05),
        "green": band(0.11, 0.17, 0.10, 0.07),
        "red": band(0.06, 0.22, 0.07, 0.06),
        "red_edge": band(0.05, 0.26, 0.22, 0.11),
        "nir": band(0.03, 0.30, 0.46, 0.30),
        "swir": band(0.02, 0.34, 0.22, 0.12),
    }
    meta = {"generated_at": datetime.now().isoformat(timespec="seconds"), "scene": name}
    return MangroveScene(bands=bands, gsd_meters=gsd_meters, name=name, reference_mask=mangrove, meta=meta)


def masks_to_png_uri(mask: np.ndarray, color: tuple[int, int, int] = (41, 231, 160)) -> str:
    """把掩膜转成 data URI（前端直接叠加显示，无需额外依赖）。"""

    from base64 import b64encode
    from io import BytesIO

    from PIL import Image

    rgba = np.zeros((*mask.shape, 4), dtype=np.uint8)
    rgba[mask] = [color[0], color[1], color[2], 200]
    img = Image.fromarray(rgba, mode="RGBA")
    buf = BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + b64encode(buf.getvalue()).decode("ascii")
