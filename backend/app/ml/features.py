"""特征工程：把水质时序窗口转为模型输入向量。

输入：长度 L 的多变量序列（水温、pH、溶解氧、浊度、叶绿素a、盐度）
输出：固定长度的特征向量（统计特征 + 变化率特征 + 交叉特征 + 时间特征）
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import numpy as np

# 与 config.METRICS 保持一致
METRIC_ORDER: tuple[str, ...] = (
    "water_temp",
    "ph",
    "dissolved_oxygen",
    "turbidity",
    "chlorophyll_a",
    "salinity",
)

# 赤潮发生的先验适宜区间（依据福建沿海赤潮多发生于 4—6 月、水温 18—28 ℃ 等业务经验）
OPTIMAL_TEMP = (18.0, 28.0)
OPTIMAL_SALINITY = (24.0, 33.0)
BLOOM_CHL_THRESHOLD = 10.0


@dataclass(frozen=True)
class FeatureSpec:
    """特征名称与切片索引。"""

    names: tuple[str, ...]

    def __len__(self) -> int:  # pragma: no cover
        return len(self.names)


FEATURE_NAMES: tuple[str, ...] = tuple(
    [f"{m}_{suffix}" for m in METRIC_ORDER for suffix in ("mean", "std", "last", "delta", "min", "max")]
    + [
        "temp_optimal_score",
        "salinity_optimal_score",
        "chl_growth_rate",
        "chl_temp_interaction",
        "do_temp_ratio",
        "do_deficit",
        "ph_deviation",
        "turbidity_chl_ratio",
        "hour_sin",
        "hour_cos",
        "doy_sin",
        "doy_cos",
        "bloom_window_flag",
    ]
)

FEATURE_SPEC = FeatureSpec(FEATURE_NAMES)


# 中文名称，用于前端可解释性展示
FEATURE_LABELS: dict[str, str] = {
    "chlorophyll_a_mean": "叶绿素a均值",
    "chlorophyll_a_std": "叶绿素a波动",
    "chlorophyll_a_last": "叶绿素a当前值",
    "chlorophyll_a_delta": "叶绿素a变化量",
    "chlorophyll_a_min": "叶绿素a最低值",
    "chlorophyll_a_max": "叶绿素a峰值",
    "water_temp_mean": "水温均值",
    "water_temp_std": "水温波动",
    "water_temp_last": "水温当前值",
    "water_temp_delta": "水温变化量",
    "water_temp_min": "水温最低值",
    "water_temp_max": "水温最高值",
    "dissolved_oxygen_mean": "溶解氧均值",
    "dissolved_oxygen_std": "溶解氧波动",
    "dissolved_oxygen_last": "溶解氧当前值",
    "dissolved_oxygen_delta": "溶解氧变化量",
    "dissolved_oxygen_min": "溶解氧最低值",
    "dissolved_oxygen_max": "溶解氧最高值",
    "ph_mean": "pH 均值",
    "ph_std": "pH 波动",
    "ph_last": "pH 当前值",
    "ph_delta": "pH 变化量",
    "ph_min": "pH 最低值",
    "ph_max": "pH 最高值",
    "turbidity_mean": "浊度均值",
    "turbidity_std": "浊度波动",
    "turbidity_last": "浊度当前值",
    "turbidity_delta": "浊度变化量",
    "turbidity_min": "浊度最低值",
    "turbidity_max": "浊度峰值",
    "salinity_mean": "盐度均值",
    "salinity_std": "盐度波动",
    "salinity_last": "盐度当前值",
    "salinity_delta": "盐度变化量",
    "salinity_min": "盐度最低值",
    "salinity_max": "盐度最高值",
    "temp_optimal_score": "水温适宜度",
    "salinity_optimal_score": "盐度适宜度",
    "chl_growth_rate": "叶绿素a增长速率",
    "chl_temp_interaction": "叶绿素a×水温耦合",
    "do_temp_ratio": "溶解氧/水温比",
    "do_deficit": "溶解氧亏缺",
    "ph_deviation": "pH 偏离度",
    "turbidity_chl_ratio": "浊度/叶绿素a比",
    "hour_sin": "昼夜节律（sin）",
    "hour_cos": "昼夜节律（cos）",
    "doy_sin": "季节节律（sin）",
    "doy_cos": "季节节律（cos）",
    "bloom_window_flag": "赤潮高发期标志",
}


def _band_score(value: float, low: float, high: float, soft: float = 3.0) -> float:
    """越靠近区间中心得分越高（0—1）。"""

    if low <= value <= high:
        center = (low + high) / 2
        half = (high - low) / 2
        return float(np.clip(1.0 - abs(value - center) / (half + 1e-6) * 0.35, 0.0, 1.0))
    distance = low - value if value < low else value - high
    return float(np.clip(1.0 - distance / soft, 0.0, 1.0))


def build_feature_vector(window: np.ndarray, ts: datetime | None = None) -> np.ndarray:
    """单个样本：`window` 形状为 (L, 6)。"""

    ts = ts or datetime.now()
    window = np.asarray(window, dtype=float)
    if window.ndim != 2 or window.shape[1] != len(METRIC_ORDER):
        raise ValueError(f"window 形状应为 (L, {len(METRIC_ORDER)})，实际为 {window.shape}")

    values: list[float] = []
    for idx in range(len(METRIC_ORDER)):
        series = window[:, idx]
        values.extend(
            [
                float(np.nanmean(series)),
                float(np.nanstd(series)),
                float(series[-1]),
                float(series[-1] - series[0]),
                float(np.nanmin(series)),
                float(np.nanmax(series)),
            ]
        )

    temp = window[:, 0]
    dox = window[:, 2]
    turb = window[:, 3]
    chl = window[:, 4]
    salt = window[:, 5]

    chl_slope = float(np.polyfit(np.arange(len(chl)), chl, 1)[0]) if len(chl) > 1 else 0.0
    values.extend(
        [
            _band_score(float(temp[-1]), *OPTIMAL_TEMP),
            _band_score(float(salt[-1]), *OPTIMAL_SALINITY),
            chl_slope * len(chl),
            float(chl[-1]) * float(temp[-1]) / 100.0,
            float(dox[-1]) / (float(temp[-1]) + 1e-6),
            max(0.0, 8.0 - float(dox[-1])),
            abs(float(window[-1, 1]) - 8.1),
            float(turb[-1]) / (float(chl[-1]) + 1e-6),
            float(np.sin(2 * np.pi * ts.hour / 24)),
            float(np.cos(2 * np.pi * ts.hour / 24)),
            float(np.sin(2 * np.pi * ts.timetuple().tm_yday / 365)),
            float(np.cos(2 * np.pi * ts.timetuple().tm_yday / 365)),
            1.0 if ts.month in (3, 4, 5, 6, 7, 8, 9, 10) else 0.0,
        ]
    )
    return np.asarray(values, dtype=float)


def build_feature_matrix(windows: np.ndarray, timestamps: list[datetime] | None = None) -> np.ndarray:
    """批量样本：`windows` 形状为 (N, L, 6)。"""

    windows = np.asarray(windows, dtype=float)
    timestamps = timestamps or [datetime.now()] * len(windows)
    return np.vstack([build_feature_vector(w, t) for w, t in zip(windows, timestamps)])


def series_to_window(records: list[dict], length: int = 24) -> np.ndarray:
    """从数据库记录列表构造固定长度窗口（不足时前向填充）。"""

    if not records:
        raise ValueError("records 不能为空")
    matrix = np.array([[float(r.get(m) or 0.0) for m in METRIC_ORDER] for r in records], dtype=float)
    if len(matrix) >= length:
        return matrix[-length:]
    pad = np.repeat(matrix[:1], length - len(matrix), axis=0)
    return np.vstack([pad, matrix])
