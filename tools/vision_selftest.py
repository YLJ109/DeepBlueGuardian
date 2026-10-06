"""视频 AI 识别自检脚本。

不打开浏览器，直接验证三段链路：
1. 推理节点是否就绪：权重、推理设备、类别与累计耗时；
2. 样本识别：逐张跑内置演示影像，打印真实检出目标与置信度；
3. 平台链路：点位、视频巡视接口，以及（可选）识别结果转告警。

用法：
    python tools/vision_selftest.py                    # 走平台接口（需后端已启动）
    python tools/vision_selftest.py --node-only        # 只检查推理节点，不依赖后端
    python tools/vision_selftest.py --alert            # 追加验证识别转告警闭环
    python tools/vision_selftest.py --video D:\\clip.mp4   # 追加一次录像巡视

说明：识别结果全部来自节点对输入画面的真实前向推理，脚本只做透传与统计，
不加权、不补全；--alert 会向平台写入一条真实告警记录，默认不执行。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:8000").rstrip("/")
NODE_URL = os.getenv("DEEPBLUE_VISION_URL", "http://127.0.0.1:8001").rstrip("/")
LOGIN = {"username": "admin", "password": "deepblue@2026"}

FAILURES = 0


def _request(method: str, url: str, payload: Any = None, token: str | None = None, timeout: float = 180.0) -> dict:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"HTTP {exc.code} {url} -> {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"无法连接 {url} -> {exc.reason}") from exc


def _check(label: str, ok: bool, detail: str = "") -> None:
    global FAILURES
    if not ok:
        FAILURES += 1
    print(f"[{'OK ' if ok else 'ERR'}] {label:<22} {detail}")


def check_node() -> dict:
    print("=" * 78)
    print("推理节点（直接访问节点接口）")
    print("=" * 78)
    info = _request("GET", f"{NODE_URL}/health", timeout=10.0)
    _check("节点状态", bool(info.get("ready")), f"ready={info.get('ready')}")
    _check("权重", bool(info.get("model")), str(info.get("model")))
    device = f"{info.get('device')} / {info.get('device_name')}"
    _check("推理设备", info.get("device") not in (None, "cpu"), device)
    classes = _request("GET", f"{NODE_URL}/classes", timeout=10.0)
    names = [str(n) for n in (classes.get("names") or {}).values()]
    _check("模型类别", bool(names), f"{len(names)} 类：{', '.join(names)}")
    focus = classes.get("focus") or []
    _check("关注策略", bool(focus), f"{len(focus)} 类关注目标（含中文标签与处置建议）")
    print(f"      累计请求 {info.get('requests')} 次 / 目标 {info.get('objects')} 个 / 平均 {info.get('avg_ms')} ms")
    return info


def check_platform(video: str | None, sample_fps: float, alert: bool, max_frames: int) -> None:
    print()
    print("=" * 78)
    print("平台接口（后端 /api/v1/vision）")
    print("=" * 78)

    token = None
    try:
        login = _request("POST", f"{BASE_URL}/api/v1/auth/login", LOGIN, timeout=15.0)
        token = login.get("access_token")
        _check("登录", bool(token), f"账号 {LOGIN['username']}")
    except RuntimeError as exc:
        _check("登录", False, str(exc))

    status = _request("GET", f"{BASE_URL}/api/v1/vision/status", token=token, timeout=20.0).get("data") or {}
    _check("节点在线", bool(status.get("online")), f"endpoint={status.get('endpoint')}")
    _check("当前权重", bool(status.get("model")), str(status.get("model")))
    _check("视频点位", bool(status.get("cameras")), f"{len(status.get('cameras') or [])} 个摄像头点位")
    print(f"      算法说明：{status.get('algorithm')}")

    samples = _request("GET", f"{BASE_URL}/api/v1/vision/samples", token=token, timeout=20.0).get("data") or []
    _check("样本清单", bool(samples), f"{len(samples)} 张")

    detected = 0
    for item in samples:
        name = item.get("name")
        try:
            result = _request(
                "POST",
                f"{BASE_URL}/api/v1/vision/detect",
                {"sample": name},
                token=token,
                timeout=120.0,
            ).get("data") or {}
        except RuntimeError as exc:
            _check(str(name), False, str(exc))
            continue
        hits = result.get("focus_hits") or []
        if hits:
            detected += 1
        summary = result.get("focus_summary") or "无结果"
        _check(str(name), True, f"{len(result.get('detections') or [])} 个检出 / {summary}")
    _check("样本检出率", detected == len(samples), f"{detected}/{len(samples)} 张识别到关注目标")

    cameras = status.get("cameras") or []
    if video:
        payload = {
            "device_code": (cameras[0] or {}).get("device_code") if cameras else None,
            "video_path": video,
            "sample_fps": sample_fps,
            "max_frames": max_frames,
        }
        try:
            scan = _request("POST", f"{BASE_URL}/api/v1/vision/scan", payload, token=token, timeout=900.0).get("data") or {}
            video_info = scan.get("video") or {}
            stat = scan.get("scan") or {}
            _check(
                "录像巡视",
                True,
                f"{stat.get('scanned_frames')} 帧 / {round(float(stat.get('wall_ms') or 0))} ms / "
                f"{stat.get('total_objects')} 个目标 / 源时长 {video_info.get('duration_s')} s",
            )
        except RuntimeError as exc:
            _check("录像巡视", False, str(exc))

    if alert:
        if not cameras:
            _check("识别转告警", False, "无可用摄像头点位，无法验证闭环")
            return
        focus = status.get("focus_classes") or []
        pick = next((f for f in focus if f.get("category") == "海漂垃圾"), focus[0] if focus else None)
        if pick is None:
            _check("识别转告警", False, "节点未返回可用类别")
            return
        payload = {
            "device_code": cameras[0]["device_code"],
            "label": pick.get("name"),
            "confidence": 0.9,
            "note": "视觉自检脚本生成的验证告警，可在后台直接处置",
        }
        try:
            result = _request("POST", f"{BASE_URL}/api/v1/vision/alert", payload, token=token, timeout=60.0)
            alert_info = result.get("alert") or {}
            _check(
                "识别转告警",
                bool(result.get("created")),
                f"{alert_info.get('alert_code')} {alert_info.get('title')} ｜ {result.get('message')}",
            )
        except RuntimeError as exc:
            _check("识别转告警", False, str(exc))


def main() -> int:
    parser = argparse.ArgumentParser(description="视频 AI 识别自检")
    parser.add_argument("--node-only", action="store_true", help="只检查推理节点，不访问后端")
    parser.add_argument("--video", help="追加一次录像巡视的本地视频路径")
    parser.add_argument("--sample-fps", type=float, default=1.0, help="录像巡视采样帧率（默认 1.0）")
    parser.add_argument("--max-frames", type=int, default=120, help="录像巡视最大采样帧数")
    parser.add_argument("--alert", action="store_true", help="追加验证识别结果转告警（会写入一条真实告警）")
    args = parser.parse_args()

    try:
        check_node()
    except RuntimeError as exc:
        _check("推理节点", False, str(exc))
        print("\n提示：后端启动时会按 DEEPBLUE_VISION_PYTHON 指定解释器自动拉起节点，")
        print("      也可手动执行 python vision_service/infer_server.py --host 127.0.0.1 --port 8001")
        return 1

    if not args.node_only:
        check_platform(args.video, args.sample_fps, args.alert, args.max_frames)

    print()
    print(f"完成：失败项 {FAILURES} 个")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
