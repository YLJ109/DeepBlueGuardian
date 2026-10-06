"""赤潮风险分级模型训练脚本。

数据来源
--------
真实的赤潮业务标签需要长期、密集且经实验室确认的观测记录，公开数据在连江
局部海域并不连续。本脚本采用「物理机理仿真 + 公开业务阈值」构建训练集：

* 时序特征由 `app.services.simulator.SiteModel` 按连江海域的海洋学规律生成
  （年周期、M2 半日潮、昼夜光合作用、站位营养指数差异）；
* 风险等级按《福建省海洋预报台赤潮监测预警信息》使用的叶绿素 a 浓度口径划分：
  未来 48 小时内叶绿素 a 峰值 < 5 / 5—10 / 10—20 / ≥20 μg/L 对应 Ⅰ—Ⅳ 级；
* 事件发生概率按福建沿海 4—6 月高发期（业务监测期 3—10 月）加权。

接入真实观测数据后，只需替换 `build_dataset` 的数据读取部分，特征工程、
训练流程、评估口径与产出文件格式均无需改动。

运行：
    python train_red_tide.py --samples 24000 --version v1
"""

from __future__ import annotations

import argparse
import json
import math
import random
from datetime import datetime, timedelta

import numpy as np
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.metrics import (
    accuracy_score,
    classification_report,
    confusion_matrix,
    f1_score,
    recall_score,
    roc_auc_score,
)
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from app.config import ARTIFACT_DIR, RISK_LEVELS
from app.data.sites import SITES
from app.ml.features import FEATURE_LABELS, FEATURE_NAMES, build_feature_matrix
from app.services.simulator import SPRING_BLOOM_DOY, BLOOM_SPREAD_DOY, SiteModel

WINDOW_HOURS = 24
HORIZON_HOURS = 48
CHL_BREAKS = (5.0, 10.0, 20.0)


def level_from_chlorophyll(peak: float) -> int:
    if peak < CHL_BREAKS[0]:
        return 1
    if peak < CHL_BREAKS[1]:
        return 2
    if peak < CHL_BREAKS[2]:
        return 3
    return 4


def _event_probability(ts: datetime, nutrient_index: float) -> float:
    """赤潮事件抽样概率：季节权重 × 站位营养水平。"""

    season = math.exp(-((ts.timetuple().tm_yday - SPRING_BLOOM_DOY) ** 2) / (2 * BLOOM_SPREAD_DOY ** 2))
    return float(np.clip(0.06 + 0.62 * season * (0.35 + nutrient_index), 0.0, 0.85))


def bloom_curve(t: float, onset: float, duration: float, intensity: float) -> float:
    """藻华强度时间曲线：前段快速增殖（约占总时长 35%），后段缓慢消亡。"""

    if duration <= 0 or t < onset or t > onset + duration:
        return 0.0
    progress = (t - onset) / duration
    if progress < 0.35:
        shape = (progress / 0.35) ** 1.6
    else:
        shape = ((1.0 - progress) / 0.65) ** 1.2
    return float(intensity * max(0.0, shape))


def build_dataset(samples: int = 24000, seed: int = 20260501) -> tuple[np.ndarray, np.ndarray, list[datetime]]:
    """构建「过去 24 小时窗口 → 未来 48 小时风险等级」的训练样本。

    每条样本在一条连续时间轴上演化同一次藻华过程（t = 0 为观测时刻），
    因此正样本的过去窗口中天然包含叶绿素 a 上升、pH 升高、溶解氧昼夜
    波动加剧等可学习的前兆信号，与真实赤潮预警的物理机制一致。
    """

    rng = random.Random(seed)
    end = datetime.now().replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=int(365.25 * 2.5))
    span_hours = int((end - start).total_seconds() // 3600)

    windows: list[np.ndarray] = []
    labels: list[int] = []
    timestamps: list[datetime] = []
    metrics = ("water_temp", "ph", "dissolved_oxygen", "turbidity", "chlorophyll_a", "salinity")

    for index in range(samples):
        site = SITES[index % len(SITES)]
        ts = start + timedelta(hours=rng.randrange(0, max(1, span_hours - HORIZON_HOURS - WINDOW_HOURS)))
        model = SiteModel(site, seed=rng.randrange(1 << 30))
        for _ in range(rng.randint(6, 40)):
            model.step_weather()

        probability = _event_probability(ts, site.nutrient_index)
        if rng.random() < probability:
            intensity = rng.uniform(0.16, 1.0) * (0.55 + 0.45 * site.nutrient_index)
            duration = rng.uniform(20.0, 64.0)
            # 让事件的上升段尽量落在观测窗口内，模拟真实预警场景
            onset = rng.uniform(-WINDOW_HOURS * 0.9, HORIZON_HOURS * 0.7)
        else:
            intensity, duration, onset = 0.0, 0.0, 0.0

        def value_at(offset: float, metric: str) -> float:
            bloom = bloom_curve(offset, onset, duration, intensity)
            return model.sample(ts + timedelta(hours=offset), bloom=bloom)[metric]

        window = np.array(
            [[
                value_at(offset, metric) for metric in metrics
            ] for offset in range(-(WINDOW_HOURS - 1), 1)],
            dtype=float,
        )
        peaks = [value_at(step, "chlorophyll_a") for step in range(1, HORIZON_HOURS + 1)]
        labels.append(level_from_chlorophyll(max(peaks)))
        windows.append(window)
        timestamps.append(ts)

    features = build_feature_matrix(np.array(windows, dtype=float), timestamps)
    return features, np.array(labels, dtype=int), timestamps

def train(samples: int, version: str, seed: int, deep: bool = False) -> dict:
    print(f"[1/4] 生成训练集：{samples} 个样本（窗口 {WINDOW_HOURS} h，预测 {HORIZON_HOURS} h）")
    features, labels, timestamps = build_dataset(samples=samples, seed=seed)

    unique, counts = np.unique(labels, return_counts=True)
    distribution = {int(k): int(v) for k, v in zip(unique, counts)}
    print(f"      等级分布：{distribution}")

    x_train, x_test, y_train, y_test = train_test_split(
        features, labels, test_size=0.25, random_state=seed, stratify=labels
    )

    print("[2/4] 训练梯度提升树模型")
    pipeline = Pipeline(
        steps=[
            ("scaler", StandardScaler()),
            (
                "model",
                GradientBoostingClassifier(
                    n_estimators=420,
                    learning_rate=0.06,
                    max_depth=3,
                    subsample=0.85,
                    min_samples_leaf=12,
                    random_state=seed,
                ),
            ),
        ]
    )
    # 类别不平衡处理：高风险样本本身稀少，但对预警系统更重要，因此按类别频率
    # 的倒数加权，保证 Ⅲ/Ⅳ 级不会被多数类淹没。
    class_counts = np.bincount(y_train, minlength=5)[1:]
    weight_map = {
        level: len(y_train) / (len(class_counts) * max(1, int(count)))
        for level, count in enumerate(class_counts, start=1)
    }
    sample_weight = np.array([weight_map[int(label)] for label in y_train], dtype=float)
    pipeline.fit(x_train, y_train, model__sample_weight=sample_weight)
    class_weight_report = {level: round(weight, 4) for level, weight in weight_map.items()}

    print("[3/4] 评估模型")
    y_pred = pipeline.predict(x_test)
    y_proba = pipeline.predict_proba(x_test)

    accuracy = float(accuracy_score(y_test, y_pred))
    macro_f1 = float(f1_score(y_test, y_pred, average="macro"))
    weighted_f1 = float(f1_score(y_test, y_pred, average="weighted"))
    recall_macro = float(recall_score(y_test, y_pred, average="macro", zero_division=0))
    report = classification_report(y_test, y_pred, output_dict=True, zero_division=0)
    matrix = confusion_matrix(y_test, y_pred, labels=[1, 2, 3, 4]).tolist()

    try:
        auc = float(roc_auc_score(y_test, y_proba, multi_class="ovr", average="macro"))
    except ValueError:
        auc = None

    # 高风险召回率：Ⅲ/Ⅳ 级是否漏报是业务上最关心的指标
    high_mask = y_test >= 3
    high_recall = float((y_pred[high_mask] >= 3).mean()) if high_mask.any() else None
    false_negative_rate = float(1 - high_recall) if high_recall is not None else None

    scaler: StandardScaler = pipeline.named_steps["scaler"]
    gb: GradientBoostingClassifier = pipeline.named_steps["model"]
    importances = gb.feature_importances_
    top_features = sorted(
        (
            {"feature": name, "label": FEATURE_LABELS.get(name, name), "importance": round(float(value), 5)}
            for name, value in zip(FEATURE_NAMES, importances)
        ),
        key=lambda item: item["importance"],
        reverse=True,
    )[:15]

    per_class = [
        {
            "level": level,
            "label": RISK_LEVELS[level]["label"],
            "name": RISK_LEVELS[level]["name"],
            "precision": round(float(report.get(str(level), {}).get("precision", 0.0)), 4),
            "recall": round(float(report.get(str(level), {}).get("recall", 0.0)), 4),
            "f1": round(float(report.get(str(level), {}).get("f1-score", 0.0)), 4),
            "support": int(report.get(str(level), {}).get("support", 0)),
        }
        for level in (1, 2, 3, 4)
    ]

    metrics = {
        "accuracy": round(accuracy, 4),
        "macro_f1": round(macro_f1, 4),
        "weighted_f1": round(weighted_f1, 4),
        "macro_recall": round(recall_macro, 4),
        "roc_auc_ovr": round(auc, 4) if auc is not None else None,
        "high_risk_recall": round(high_recall, 4) if high_recall is not None else None,
        "high_risk_false_negative_rate": round(false_negative_rate, 4) if false_negative_rate is not None else None,
        "test_samples": int(len(y_test)),
        "train_samples": int(len(y_train)),
    }

    meta = {
        "name": "red_tide",
        "version": version,
        "trained_at": datetime.now().isoformat(timespec="seconds"),
        "thresholds": [0.25, 0.50, 0.75],
        "algorithm": "GradientBoostingClassifier + StandardScaler",
        "window_hours": WINDOW_HOURS,
        "horizon_hours": HORIZON_HOURS,
        "labeling": {
            "basis": "福建省海洋预报台赤潮监测预警信息的叶绿素a浓度口径",
            "breaks_ug_per_l": list(CHL_BREAKS),
            "description": "未来 48 小时叶绿素a峰值 <5 / 5-10 / 10-20 / >=20 μg/L 对应 Ⅰ/Ⅱ/Ⅲ/Ⅳ 级",
        },
        "features": {
            "count": len(FEATURE_NAMES),
            "names": list(FEATURE_NAMES),
            "top": top_features,
        },
        "training": {
            "samples": samples,
            "class_distribution": distribution,
            "class_weight": class_weight_report,
            "seed": seed,
            "data_source": "physics-simulation",
            "sites": [s.code for s in SITES],
            "note": "接入真实观测数据后可复用同一特征工程与训练流程。",
        },
        "metrics": metrics,
        "per_class": per_class,
        "confusion_matrix": {"labels": [1, 2, 3, 4], "matrix": matrix},
    }

    print(f"      准确率 {accuracy:.4f} | 宏平均 F1 {macro_f1:.4f} | 高风险召回率 {high_recall}")

    print("[4/4] 保存模型产物")
    import joblib

    bundle = {
        "pipeline": pipeline,
        "feature_mean": scaler.mean_,
        "feature_std": scaler.scale_,
        "importances": importances,
        "feature_names": list(FEATURE_NAMES),
    }
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    joblib.dump(bundle, ARTIFACT_DIR / f"red_tide_{version}.joblib")
    (ARTIFACT_DIR / f"red_tide_{version}.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"      模型已写入 {ARTIFACT_DIR / f'red_tide_{version}.joblib'}")
    return meta


def main() -> None:
    parser = argparse.ArgumentParser(description="训练赤潮风险分级模型")
    parser.add_argument("--samples", type=int, default=24000)
    parser.add_argument("--version", type=str, default="v1")
    parser.add_argument("--seed", type=int, default=20260501)
    parser.add_argument("--deep", action="store_true", help="预留：启用深度时序分支（需安装 torch）")
    args = parser.parse_args()
    meta = train(args.samples, args.version, args.seed, deep=args.deep)
    print(json.dumps(meta["metrics"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
