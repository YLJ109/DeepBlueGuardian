"""海洋生态智能体自检脚本。

直接调用服务层（不经过 HTTP），验证三件事：
1. 平台数据工具能否取到真实数据；
2. 大模型能否正确选择工具并给出有依据的结论；
3. 写操作与权限拦截是否符合预期。

用法：
    python tools/agent_selftest.py                 # 跑默认问题集
    python tools/agent_selftest.py -q "现在有没有风险点位？"
    python tools/agent_selftest.py --tools-only    # 只跑工具，不调用大模型
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

from app.database import SessionLocal  # noqa: E402
from app.services import agent  # noqa: E402

DEFAULT_QUESTIONS = [
    "现在平台整体情况怎么样？",
    "黄岐湾中部浮标最新的水质如何，有没有指标越限？",
    "未来 48 小时赤潮风险最高的点位在哪里？",
    "最近 24 小时有哪些告警还没处理？",
    "马鼻镇浮曦村红树林最近一期的面积和健康度如何？",
]


class _Anonymous:
    username = None
    role = "viewer"


def run_tools(db) -> None:
    print("=" * 72)
    print("工具自检（直接调用服务层）")
    print("=" * 72)
    probe_calls = [
        ("platform_overview", {}),
        ("list_devices", {"device_type": "buoy"}),
        ("get_water_quality", {"device": "黄岐湾", "hours": 24}),
        ("get_metric_trend", {"device": "黄岐湾", "metric": "chlorophyll_a", "hours": 48}),
        ("list_alerts", {"hours": 24}),
        ("red_tide_risk", {"hours": 48}),
        ("mangrove_status", {}),
    ]
    for name, args in probe_calls:
        result, focus, ok = agent._execute_tool(db, name, json.dumps(args, ensure_ascii=False), _Anonymous())
        label = agent.TOOLS[name].label
        status = "OK " if ok else "ERR"
        detail = result.get("error") if "error" in result else agent._summarize(label, result)
        print(f"[{status}] {label:<10} {name:<20} -> {detail}  focus={len(focus)}")

    denied, _, ok = agent._execute_tool(db, "run_inspection", "{}", _Anonymous())
    print(f"[{'OK ' if not ok else 'ERR'}] 权限拦截    run_inspection       -> {denied.get('error')}")


def run_questions(db, questions: list[str]) -> int:
    failures = 0
    for index, question in enumerate(questions, start=1):
        print()
        print("=" * 72)
        print(f"第 {index} 问：{question}")
        print("=" * 72)
        result = agent.chat(db, question, _Anonymous())
        for step in result.get("tools", []):
            flag = "OK " if step.get("ok") else "ERR"
            print(f"  [{flag}] {step['label']:<10} {step.get('arguments')}")
        print("-" * 72)
        print(result.get("answer", "").strip())
        focus = [f.get("device_code") or f.get("region_code") for f in result.get("focus", [])]
        meta = result.get("model"), result.get("rounds"), result.get("usage", {}).get("total_tokens")
        print("-" * 72)
        print(f"模型={meta[0]} 轮次={meta[1]} tokens={meta[2]} 降级={result.get('degraded')} 高亮={focus}")
        if result.get("degraded"):
            failures += 1
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="海洋生态智能体自检")
    parser.add_argument("-q", "--question", help="只跑单个问题")
    parser.add_argument("--tools-only", action="store_true", help="只验证工具取数，不调用大模型")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        run_tools(db)
        if args.tools_only:
            return 0
        questions = [args.question] if args.question else DEFAULT_QUESTIONS
        degraded = run_questions(db, questions)
        print()
        print(f"完成：{len(questions)} 个问题，降级 {degraded} 个")
        return 1 if degraded else 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
