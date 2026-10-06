"""深蓝智护 · 视觉推理节点（独立进程）。

运行环境
--------
conda 环境 ``pytorch_cuda_env``（torch + ultralytics + opencv，CUDA 可用）。
该环境没有 fastapi/uvicorn，因此本节点只依赖标准库 ``http.server`` 提供
最小 HTTP 服务，便于用一条命令启动，也便于后续换成边缘盒子上的常驻进程。

启动
----
    python vision_service/infer_server.py \
        --model backend/data/models/marine_litter_yolo11s.pt --host 127.0.0.1 --port 8001

接口
----
    GET  /health    节点状态、模型信息、推理设备与耗时统计
    GET  /classes   模型类别、中文标签与关注类别处置策略
    POST /detect    请求体为图片原始字节（jpg/png），返回检测框、统计与标注图
    POST /scan      请求体为 JSON，对本地视频按采样帧率逐帧识别并汇总事件

说明
----
检测结果全部来自模型对输入画面的真实前向推理，节点不做任何结果编造；
输入画面为空、损坏或无法解码时直接返回错误，由调用方决定如何提示。
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import logging
import os
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# ---- 中文标签与关注类别处置策略（与后端 services/vision.py 保持同一口径）----
LABELS_ZH: dict[str, str] = {
    "person": "人员",
    "boat": "船只",
    "bird": "海鸟",
    "surfboard": "冲浪板",
    "bottle": "瓶状漂浮物",
    "airplane": "低空飞行器",
    "kite": "风筝",
    "umbrella": "伞状物体",
    "frisbee": "圆盘状漂浮物",
    "motorcycle": "摩托艇",
    "car": "车辆",
    "truck": "货车",
    "bus": "客车",
    "bench": "岸基设施",
    "dog": "犬只",
    "cat": "猫",
    "cow": "牛",
    "sheep": "羊",
    # 海洋垃圾检测权重（来自连渔智脑 TideSignal 项目）的类别
    "plastic": "塑料垃圾",
    "foam": "泡沫塑料",
    "fishing_gear": "废弃渔具",
    "metal_wood": "金属木质垃圾",
    "debris_other": "其他海漂垃圾",
    "vessel": "船只",
    "buoy": "养殖浮标",
    "cage": "养殖网箱",
    "fish": "鱼类",
}

FOCUS_POLICY: dict[str, dict[str, str]] = {
    "person": {
        "category": "人员活动",
        "level": "critical",
        "title": "水域人员活动识别",
        "advice": "识别到人员出现在监测水域画面内，建议立即核查是否为违规作业或落水求助。",
    },
    "boat": {
        "category": "船只活动",
        "level": "warning",
        "title": "船只进入监测水域",
        "advice": "识别到船只进入监测水域，建议核查船名、作业许可与是否进入禁养/航道区域。",
    },
    "bottle": {
        "category": "水面漂浮物",
        "level": "warning",
        "title": "水面疑似漂浮垃圾",
        "advice": "识别到瓶状漂浮物，建议安排保洁船只打捞并追溯陆源排放。",
    },
    "surfboard": {
        "category": "水面活动器具",
        "level": "warning",
        "title": "水面活动器具识别",
        "advice": "识别到水面活动器具，建议核查是否在养殖区或航道内活动。",
    },
    "frisbee": {
        "category": "水面漂浮物",
        "level": "info",
        "title": "水面圆盘状漂浮物",
        "advice": "识别到圆盘状漂浮物，建议人工复核是否为养殖浮球或垃圾。",
    },
    "airplane": {
        "category": "低空目标",
        "level": "info",
        "title": "低空飞行器活动",
        "advice": "识别到低空飞行器，建议核查是否为巡检无人机作业。",
    },
    "bird": {
        "category": "生态目标",
        "level": "info",
        "title": "海鸟活动",
        "advice": "识别到海鸟活动，可作为湿地生态状况的辅助观察记录。",
    },
    "plastic": {
        "category": "海漂垃圾",
        "level": "warning",
        "title": "水面塑料垃圾识别",
        "advice": "识别到塑料垃圾，建议安排打捞并追溯陆源与航运排放。",
    },
    "foam": {
        "category": "海漂垃圾",
        "level": "warning",
        "title": "泡沫塑料垃圾识别",
        "advice": "识别到泡沫塑料垃圾，易破碎扩散，建议优先清理。",
    },
    "fishing_gear": {
        "category": "海漂垃圾",
        "level": "warning",
        "title": "废弃渔具识别",
        "advice": "识别到废弃渔具，存在缠绕养殖设施与海洋生物的风险，建议尽快回收。",
    },
    "metal_wood": {
        "category": "海漂垃圾",
        "level": "info",
        "title": "金属木质漂浮物识别",
        "advice": "识别到金属或木质漂浮物，建议复核是否影响航道与养殖设施。",
    },
    "debris_other": {
        "category": "海漂垃圾",
        "level": "info",
        "title": "其他海漂垃圾识别",
        "advice": "识别到其他类型海漂垃圾，建议人工复核并纳入打捞计划。",
    },
    "vessel": {
        "category": "船只活动",
        "level": "warning",
        "title": "船只进入监测水域",
        "advice": "识别到船只进入监测水域，建议核查船名、作业许可与是否进入禁养或航道区域。",
    },
    "buoy": {
        "category": "养殖设施",
        "level": "info",
        "title": "养殖浮标识别",
        "advice": "识别到养殖浮标，可用于核对养殖设施分布与漂移情况。",
    },
    "cage": {
        "category": "养殖设施",
        "level": "info",
        "title": "养殖网箱识别",
        "advice": "识别到养殖网箱，可用于核对养殖区范围与设施完好情况。",
    },
}

logger = logging.getLogger("deepblue.vision.server")

# 海洋监测场景类别白名单：COCO 预训练权重会输出大量与水域无关的类别
# （风筝、球类、餐具等），这些结果在业务上没有意义，直接在节点侧丢弃。
ALLOWED_CLASSES: set[str] = {
    "person",
    "boat",
    "bird",
    "bottle",
    "surfboard",
    "airplane",
    "car",
    "truck",
    "bus",
    "motorcycle",
    # 海洋垃圾检测权重输出的类别
    "plastic",
    "foam",
    "fishing_gear",
    "metal_wood",
    "debris_other",
    "vessel",
    "buoy",
    "cage",
    "fish",
}

STATE: dict[str, Any] = {
    "ready": False,
    "model_path": None,
    "model_name": None,
    "device": None,
    "device_name": None,
    "half": False,
    "imgsz": 640,
    "started_at": None,
    "requests": 0,
    "objects": 0,
    "elapsed_ms_total": 0.0,
    "elapsed_ms_max": 0.0,
    "last_error": None,
    "names": {},
}

MODEL = None
LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# 模型加载与推理
# ---------------------------------------------------------------------------
def load_model(model_path: Path, device: str, imgsz: int, half: bool) -> None:
    """加载权重并做一次预热前向，把耗时的初始化从首次请求里挪到启动阶段。"""

    import torch
    from ultralytics import YOLO

    global MODEL

    if device in ("auto", "", None):
        resolved = "0" if torch.cuda.is_available() else "cpu"
    else:
        resolved = str(device)
    if resolved.isdigit() and not torch.cuda.is_available():
        resolved = "cpu"

    use_half = bool(half) and resolved != "cpu"
    model = YOLO(str(model_path))
    MODEL = model

    STATE.update(
        {
            "model_path": str(model_path),
            "model_name": model_path.name,
            "device": f"cuda:{resolved}" if resolved.isdigit() else resolved,
            "device_name": torch.cuda.get_device_name(int(resolved)) if resolved.isdigit() else "CPU",
            "half": use_half,
            "imgsz": imgsz,
            "names": {str(k): v for k, v in (model.names or {}).items()},
        }
    )

    import numpy as np

    warmup = np.zeros((imgsz, imgsz, 3), dtype="uint8")
    warmup_args: dict[str, Any] = {}
    if use_half:
        warmup_args["half"] = True
    model.predict(warmup, device=resolved, imgsz=imgsz, conf=0.25, verbose=False, **warmup_args)

    STATE["ready"] = True
    STATE["started_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    logger.info(
        "视觉推理节点就绪：模型 %s，设备 %s，输入 %d，半精度 %s",
        model_path.name,
        STATE["device"],
        imgsz,
        use_half,
    )


def _device_arg() -> Any:
    device = STATE.get("device") or "cpu"
    return device.split(":")[-1] if device.startswith("cuda:") else device


def _label_of(name: str) -> str:
    return LABELS_ZH.get(name, name)


def _decode_image(payload: bytes):
    import cv2
    import numpy as np

    if not payload:
        raise ValueError("请求体为空，未收到图像数据")
    buf = np.frombuffer(payload, dtype="uint8")
    image = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("图像解码失败，请确认上传的是 jpg/png 等常见图片格式")
    return image


def _detect_array(image, conf: float, iou: float) -> dict[str, Any]:
    """对单张 BGR 图像执行一次真实前向推理，返回结构化结果。"""

    import numpy as np

    h, w = image.shape[:2]
    started = time.perf_counter()
    with LOCK:
        predict_args: dict[str, Any] = {}
        if STATE["half"]:
            predict_args["half"] = True
        result = MODEL.predict(
            image,
            device=_device_arg(),
            imgsz=STATE["imgsz"],
            conf=conf,
            iou=iou,
            verbose=False,
            **predict_args,
        )[0]
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    boxes = result.boxes
    names = result.names or STATE["names"]
    detections: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    suppressed = 0

    if boxes is not None and len(boxes) > 0:
        xyxy = boxes.xyxy.detach().cpu().numpy()
        confs = boxes.conf.detach().cpu().numpy()
        classes = boxes.cls.detach().cpu().numpy().astype(int)
        for (x1, y1, x2, y2), score, cls_id in zip(xyxy, confs, classes):
            raw_name = str(names.get(cls_id, cls_id)) if isinstance(names, dict) else str(names[cls_id])
            if ALLOWED_CLASSES and raw_name not in ALLOWED_CLASSES:
                suppressed += 1
                continue
            x1c, y1c = float(max(0.0, x1)), float(max(0.0, y1))
            x2c, y2c = float(min(w, x2)), float(min(h, y2))
            box_area = max(0.0, x2c - x1c) * max(0.0, y2c - y1c)
            counts[raw_name] = counts.get(raw_name, 0) + 1
            detections.append(
                {
                    "class_id": int(cls_id),
                    "name": raw_name,
                    "label": _label_of(raw_name),
                    "confidence": round(float(score), 4),
                    "bbox": [round(x1c, 1), round(y1c, 1), round(x2c, 1), round(y2c, 1)],
                    "area_ratio": round(box_area / float(w * h), 5) if w and h else 0.0,
                    "focus": raw_name in FOCUS_POLICY,
                }
            )

    detections.sort(key=lambda d: d["confidence"], reverse=True)

    # 标注图：直接使用 ultralytics 的绘制结果，保证框与模型输出一致
    annotated = None
    try:
        plotted = result.plot(line_width=2, conf=True, labels=True)
        import cv2

        ok, buf = cv2.imencode(".jpg", plotted, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
        if ok:
            annotated = base64.b64encode(buf.tobytes()).decode("ascii")
    except Exception as exc:  # pragma: no cover - 标注失败不影响检测结果
        logger.warning("绘制标注图失败：%s", exc)

    focus_counts = {k: v for k, v in counts.items() if k in FOCUS_POLICY}

    STATE["requests"] += 1
    STATE["objects"] += len(detections)
    STATE["elapsed_ms_total"] += elapsed_ms
    STATE["elapsed_ms_max"] = max(STATE["elapsed_ms_max"], elapsed_ms)

    return {
        "ok": True,
        "image": {"width": int(w), "height": int(h)},
        "elapsed_ms": round(elapsed_ms, 2),
        "inference": {
            "model": STATE["model_name"],
            "device": STATE["device"],
            "device_name": STATE["device_name"],
            "imgsz": STATE["imgsz"],
            "conf_threshold": conf,
            "iou_threshold": iou,
        },
        "counts": counts,
        "suppressed": suppressed,
        "focus_counts": focus_counts,
        "total": len(detections),
        "objects": len(detections),
        "detections": detections,
        "annotated_jpeg_base64": annotated,
    }


# ---------------------------------------------------------------------------
# 视频逐帧扫描
# ---------------------------------------------------------------------------
def scan_video(payload: dict[str, Any]) -> dict[str, Any]:
    import cv2
    import numpy as np

    raw_path = str(payload.get("video_path") or "").strip()
    if not raw_path:
        raise ValueError("缺少 video_path 参数")
    path = Path(raw_path)
    if not path.exists() or not path.is_file():
        raise ValueError(f"视频文件不存在：{raw_path}")

    conf = float(payload.get("conf", 0.35))
    sample_fps = max(0.1, min(10.0, float(payload.get("sample_fps", 1.0))))
    max_frames = max(1, min(1200, int(payload.get("max_frames", 300))))
    preview_limit = max(0, min(6, int(payload.get("preview_frames", 3))))

    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        raise ValueError(f"视频无法打开：{raw_path}")

    src_fps = capture.get(cv2.CAP_PROP_FPS) or 25.0
    frame_total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    step = max(1, int(round(src_fps / sample_fps)))

    frame_index = 0
    scanned = 0
    timeline: list[dict[str, Any]] = []
    class_stats: dict[str, dict[str, Any]] = {}
    previews: list[dict[str, Any]] = []
    total_objects = 0
    started = time.perf_counter()

    while scanned < max_frames:
        ok, frame = capture.read()
        if not ok:
            break
        if frame_index % step == 0:
            scanned += 1
            result = _detect_array(frame, conf=conf, iou=0.45)
            ts = round(frame_index / src_fps, 2) if src_fps else 0.0
            total_objects += result["total"]
            timeline.append(
                {
                    "frame": frame_index,
                    "t": ts,
                    "counts": result["counts"],
                    "total": result["total"],
                    "elapsed_ms": result["elapsed_ms"],
                }
            )
            for name, count in result["counts"].items():
                item = class_stats.setdefault(
                    name,
                    {
                        "name": name,
                        "label": _label_of(name),
                        "focus": name in FOCUS_POLICY,
                        "frames": 0,
                        "max_count": 0,
                        "total_count": 0,
                        "max_confidence": 0.0,
                        "peak_t": None,
                        "peak_frame": None,
                    },
                )
                item["frames"] += 1
                item["total_count"] += count
                if count > item["max_count"]:
                    item["max_count"] = count
                    item["peak_t"] = ts
                    item["peak_frame"] = frame_index
                best = max(
                    (d["confidence"] for d in result["detections"] if d["name"] == name), default=0.0
                )
                item["max_confidence"] = max(item["max_confidence"], best)

            if preview_limit and result["total"] > 0 and result.get("annotated_jpeg_base64"):
                previews.append(
                    {
                        "frame": frame_index,
                        "t": ts,
                        "total": result["total"],
                        "counts": result["counts"],
                        "image": result["annotated_jpeg_base64"],
                    }
                )
                previews.sort(key=lambda p: p["total"], reverse=True)
                previews = previews[:preview_limit]
        frame_index += 1

    capture.release()
    wall_ms = (time.perf_counter() - started) * 1000.0

    ranked = sorted(class_stats.values(), key=lambda it: (it["frames"], it["max_count"]), reverse=True)
    return {
        "ok": True,
        "video": {
            "path": str(path),
            "name": path.name,
            "src_fps": round(src_fps, 3),
            "frame_total": frame_total,
            "duration_s": round(frame_total / src_fps, 2) if src_fps else None,
        },
        "scan": {
            "sample_fps": sample_fps,
            "frame_step": step,
            "scanned_frames": scanned,
            "max_frames": max_frames,
            "conf_threshold": conf,
            "wall_ms": round(wall_ms, 2),
            "avg_ms_per_frame": round(wall_ms / scanned, 2) if scanned else None,
            "total_objects": total_objects,
            "device": STATE["device"],
            "model": STATE["model_name"],
        },
        "classes": ranked,
        "timeline": timeline,
        "previews": previews,
    }


# ---------------------------------------------------------------------------
# HTTP 层
# ---------------------------------------------------------------------------
def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "DeepBlueVision/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003 - 基类签名
        logger.debug("%s - %s", self.address_string(), fmt % args)

    # -- 工具 ----------------------------------------------------------
    def _send(self, status: int, payload: Any) -> None:
        body = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _query(self) -> dict[str, str]:
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(self.path)
        return {k: v[0] for k, v in parse_qs(parsed.query).items()}

    def _route(self) -> str:
        from urllib.parse import urlparse

        return urlparse(self.path).path.rstrip("/") or "/"

    # -- GET -----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - 基类命名
        route = self._route()
        if route in ("/health", "/"):
            avg = STATE["elapsed_ms_total"] / STATE["requests"] if STATE["requests"] else None
            self._send(
                200,
                {
                    "ok": True,
                    "ready": STATE["ready"],
                    "model": STATE["model_name"],
                    "model_path": STATE["model_path"],
                    "device": STATE["device"],
                    "device_name": STATE["device_name"],
                    "half": STATE["half"],
                    "imgsz": STATE["imgsz"],
                    "started_at": STATE["started_at"],
                    "requests": STATE["requests"],
                    "objects": STATE["objects"],
                    "avg_ms": round(avg, 2) if avg else None,
                    "max_ms": round(STATE["elapsed_ms_max"], 2) if STATE["requests"] else None,
                    "last_error": STATE["last_error"],
                    "focus_classes": sorted(FOCUS_POLICY.keys()),
                    "allowed_classes": sorted(ALLOWED_CLASSES),
                },
            )
            return
        if route == "/classes":
            self._send(
                200,
                {
                    "ok": True,
                    "model": STATE["model_name"],
                    "count": len(STATE["names"]),
                    "names": STATE["names"],
                    "labels_zh": LABELS_ZH,
                    "focus": [
                        {"name": key, **value} for key, value in FOCUS_POLICY.items()
                    ],
                },
            )
            return
        self._send(404, {"ok": False, "error": f"未知接口：{route}"})

    # -- POST ----------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802 - 基类命名
        route = self._route()
        try:
            if not STATE["ready"]:
                self._send(503, {"ok": False, "error": "模型尚未就绪"})
                return
            if route == "/detect":
                query = self._query()
                conf = float(query.get("conf", 0.35))
                iou = float(query.get("iou", 0.45))
                image = _decode_image(self._read_body())
                self._send(200, _detect_array(image, conf=conf, iou=iou))
                return
            if route == "/scan":
                body = json.loads(self._read_body().decode("utf-8") or "{}")
                self._send(200, scan_video(body))
                return
            self._send(404, {"ok": False, "error": f"未知接口：{route}"})
        except ValueError as exc:
            STATE["last_error"] = str(exc)
            self._send(400, {"ok": False, "error": str(exc)})
        except Exception as exc:  # pragma: no cover - 兜底，避免节点被单次请求打断
            STATE["last_error"] = f"{type(exc).__name__}: {exc}"
            logger.error("请求处理失败：%s\n%s", exc, traceback.format_exc())
            self._send(500, {"ok": False, "error": f"推理失败：{type(exc).__name__}: {exc}"})


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="深蓝智护视觉推理节点")
    model_dir = Path(__file__).resolve().parent.parent / "backend" / "data" / "models"
    # 优先内置的海漂垃圾自训练权重，缺失时退回 COCO 预训练权重
    default_model = next(
        (p for p in (model_dir / "marine_litter_yolo11s.pt", model_dir / "yolov8n.pt") if p.exists()),
        model_dir / "marine_litter_yolo11s.pt",
    )
    parser.add_argument("--model", default=str(default_model), help="YOLO 权重路径")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    parser.add_argument("--port", type=int, default=8001, help="监听端口")
    parser.add_argument("--device", default="auto", help="推理设备：auto / 0 / cpu")
    parser.add_argument("--imgsz", type=int, default=640, help="推理输入尺寸")
    parser.add_argument("--half", action="store_true", help="启用半精度推理")
    parser.add_argument(
        "--classes",
        default="",
        help="逗号分隔的类别白名单，留空则使用节点内置的海洋场景白名单",
    )
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    global ALLOWED_CLASSES
    if args.classes.strip():
        ALLOWED_CLASSES = {c.strip() for c in args.classes.split(",") if c.strip()}
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:  # pragma: no cover - 非 TTY 场景忽略
        pass

    model_path = Path(args.model)
    if not model_path.exists():
        logger.error("权重文件不存在：%s", model_path)
        return 2

    load_model(model_path, device=args.device, imgsz=args.imgsz, half=args.half)

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    logger.info("视觉推理节点监听 http://%s:%d", args.host, args.port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("收到中断信号，节点退出")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
