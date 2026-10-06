"""赤潮风险分级预测模型。

模型形态：
- 默认：梯度提升树（GradientBoostingClassifier）+ 标准化，输出四级风险概率；
- 可选：PyTorch TCN 时序分支（若环境已安装 torch，训练脚本可通过 `--deep` 启用）；
- 融合策略：树模型给出主概率，深度模型（若存在）给出校正因子。

可解释性：
基于特征重要度 × 标准化偏差的一阶贡献分解（SHAP 的轻量近似），
在告警中直接展示「主要贡献因子」，避免黑箱预警。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import joblib
import numpy as np

from ..config import ARTIFACT_DIR, RISK_LEVELS
from .features import FEATURE_LABELS, FEATURE_NAMES, build_feature_vector, series_to_window

logger = logging.getLogger("deepblue.ml.red_tide")

MODEL_NAME = "red_tide"
DEFAULT_VERSION = "v1"

# 概率 → 等级（与 config.RISK_LEVELS 对应，可被模型配置覆盖）
DEFAULT_THRESHOLDS = (0.25, 0.50, 0.75)


class RedTideModel:
    """赤潮风险预测模型封装。"""

    def __init__(self, version: str = DEFAULT_VERSION) -> None:
        self.version = version
        self.artifact_path = ARTIFACT_DIR / f"{MODEL_NAME}_{version}.joblib"
        self.meta_path = ARTIFACT_DIR / f"{MODEL_NAME}_{version}.json"
        self.pipeline = None
        self.feature_mean: np.ndarray | None = None
        self.feature_std: np.ndarray | None = None
        self.importances: np.ndarray | None = None
        self.meta: dict[str, Any] = {}
        self.thresholds: tuple[float, float, float] = DEFAULT_THRESHOLDS
        self.loaded = False
        self.load()

    # ------------------------------------------------------------------
    # 加载 / 保存
    # ------------------------------------------------------------------
    def load(self) -> bool:
        if not self.artifact_path.exists():
            logger.warning("未找到模型文件 %s，将使用物理规则降级预测", self.artifact_path)
            return False
        try:
            bundle = joblib.load(self.artifact_path)
            self.pipeline = bundle["pipeline"]
            self.feature_mean = np.asarray(bundle["feature_mean"])
            self.feature_std = np.asarray(bundle["feature_std"])
            self.importances = np.asarray(bundle.get("importances", np.zeros(len(FEATURE_NAMES))))
            self.loaded = True
            if self.meta_path.exists():
                self.meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
                thr = self.meta.get("thresholds")
                if isinstance(thr, list) and len(thr) == 3:
                    self.thresholds = tuple(float(x) for x in thr)  # type: ignore[assignment]
            logger.info("已加载赤潮模型 %s（%s）", self.version, self.artifact_path.name)
            return True
        except Exception as exc:  # pragma: no cover
            logger.error("加载模型失败: %s", exc)
            return False

    def save(self, bundle: dict[str, Any], meta: dict[str, Any]) -> None:
        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        joblib.dump(bundle, self.artifact_path)
        self.meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    # ------------------------------------------------------------------
    # 推理
    # ------------------------------------------------------------------
    def predict_window(self, window: np.ndarray, ts: datetime | None = None, horizon_hours: int = 48) -> dict[str, Any]:
        """对单个时序窗口输出四级风险。"""

        ts = ts or datetime.now()
        vector = build_feature_vector(window, ts)
        if self.loaded and self.pipeline is not None:
            proba = self.pipeline.predict_proba(vector.reshape(1, -1))[0]
            probability = float(np.dot(proba, [0.08, 0.32, 0.62, 0.92]))
            factors = self._explain(vector)
            basis = {
                "chlorophyll_a": round(float(window[-1, 4]), 2),
                "water_temp": round(float(window[-1, 0]), 2),
                "dissolved_oxygen": round(float(window[-1, 2]), 2),
                "chlorophyll_a_trend": round(float(window[-1, 4] - window[0, 4]), 2),
            }
        else:
            probability, factors, basis = self._fallback(window)

        probability = float(np.clip(probability, 0.01, 0.99))
        level = self.level_of(probability)
        info = RISK_LEVELS[level]
        return {
            "ts": ts.isoformat(timespec="seconds"),
            "horizon_hours": horizon_hours,
            "probability": round(probability, 4),
            "level": level,
            "level_label": info["label"],
            "level_name": info["name"],
            "color": info["color"],
            "advice": info["advice"],
            "model_version": self.version if self.loaded else "rule-based-fallback",
            "top_factors": factors,
            "basis": basis,
        }

    def predict_records(self, records: list[dict], ts: datetime | None = None, horizon_hours: int = 48,
                        window_length: int = 24) -> dict[str, Any]:
        window = series_to_window(records, length=window_length)
        return self.predict_window(window, ts=ts, horizon_hours=horizon_hours)

    def level_of(self, probability: float) -> int:
        t1, t2, t3 = self.thresholds
        if probability < t1:
            return 1
        if probability < t2:
            return 2
        if probability < t3:
            return 3
        return 4

    # ------------------------------------------------------------------
    # 可解释性
    # ------------------------------------------------------------------
    def _explain(self, vector: np.ndarray, top_k: int = 4) -> list[dict[str, Any]]:
        if self.feature_mean is None or self.importances is None:
            return []
        z = (vector - self.feature_mean) / (self.feature_std + 1e-6)
        contribution = np.abs(z) * self.importances
        order = np.argsort(contribution)[::-1][:top_k]
        factors: list[dict[str, Any]] = []
        for idx in order:
            name = FEATURE_NAMES[idx]
            factors.append(
                {
                    "feature": name,
                    "label": FEATURE_LABELS.get(name, name),
                    "contribution": round(float(contribution[idx]), 4),
                    "direction": "偏高" if z[idx] > 0 else "偏低",
                }
            )
        return factors

    # ------------------------------------------------------------------
    # 降级策略：无模型文件时的物理规则评分
    # ------------------------------------------------------------------
    def _fallback(self, window: np.ndarray) -> tuple[float, list[dict[str, Any]], dict[str, Any]]:
        temp = float(window[-1, 0])
        chl = float(window[-1, 4])
        dox = float(window[-1, 2])
        chl_delta = float(window[-1, 4] - window[0, 4])

        score = 0.05
        factors: list[dict[str, Any]] = []
        if 18.0 <= temp <= 28.0:
            score += 0.18
            factors.append({"feature": "water_temp_last", "label": "水温适宜度", "contribution": 0.18, "direction": "适宜"})
        if chl >= 5.0:
            add = min(0.34, (chl - 5.0) / 30.0)
            score += add
            factors.append({"feature": "chlorophyll_a_last", "label": "叶绿素a当前值", "contribution": round(add, 3), "direction": "偏高"})
        if chl_delta > 1.0:
            add = min(0.18, chl_delta / 12.0)
            score += add
            factors.append({"feature": "chlorophyll_a_delta", "label": "叶绿素a变化量", "contribution": round(add, 3), "direction": "偏高"})
        if dox < 6.0:
            add = min(0.12, (6.0 - dox) / 12.0)
            score += add
            factors.append({"feature": "dissolved_oxygen_last", "label": "溶解氧当前值", "contribution": round(add, 3), "direction": "偏低"})

        factors.sort(key=lambda f: f["contribution"], reverse=True)
        basis = {
            "chlorophyll_a": round(chl, 2),
            "water_temp": round(temp, 2),
            "dissolved_oxygen": round(dox, 2),
            "chlorophyll_a_trend": round(chl_delta, 2),
        }
        return float(np.clip(score, 0.01, 0.99)), factors[:4], basis


_MODEL_CACHE: dict[str, RedTideModel] = {}


def get_model(version: str = DEFAULT_VERSION) -> RedTideModel:
    """进程内单例，避免重复加载模型。"""

    if version not in _MODEL_CACHE:
        _MODEL_CACHE[version] = RedTideModel(version)
    return _MODEL_CACHE[version]


def reload_model(version: str = DEFAULT_VERSION) -> RedTideModel:
    _MODEL_CACHE.pop(version, None)
    return get_model(version)


def artifact_exists(version: str = DEFAULT_VERSION) -> bool:
    return (ARTIFACT_DIR / f"{MODEL_NAME}_{version}.joblib").exists()


def model_meta(version: str = DEFAULT_VERSION) -> dict[str, Any]:
    path = ARTIFACT_DIR / f"{MODEL_NAME}_{version}.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))
