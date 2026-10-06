"""AI 模型模块：赤潮风险分级预测 + 红树林遥感分割。"""

from .red_tide import RedTideModel, get_model
from .mangrove import MangroveAnalyzer

__all__ = ["RedTideModel", "get_model", "MangroveAnalyzer"]
