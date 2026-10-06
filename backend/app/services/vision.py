"""视觉识别服务：代理 GPU 推理节点，并把识别结果接入告警闭环。

职责边界
--------
本模块不执行任何模型推理，只做三件事：

1. 代理调用独立的视觉推理节点（``vision_service/infer_server.py``），
   必要时在后端启动时把该节点作为子进程拉起，避免演示时忘记开第二个窗口；
2. 把节点返回的通用类别结果，按平台口径翻译成「船只 / 人员 / 漂浮物 / 海鸟」
   等业务语义，并叠加关注等级与处置建议；
3. 将确认后的识别结果写入平台告警表，纳入既有的通知、处置与审计链路。

所有检测数值均来自节点对真实输入画面的前向推理，本层不做任何补值或推测。
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import PROJECT_DIR, VISION_FOCUS, settings
from ..models import AuditLog, Device
from .alerts import AlertEngine

logger = logging.getLogger("deepblue.vision")

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
MANIFEST_NAME = "samples.json"

_PROCESS: subprocess.Popen | None = None
_LOG_HANDLE = None
_LAST_ERROR: str | None = None


class VisionError(RuntimeError):
    """视觉节点不可用或返回业务错误。"""


# ---------------------------------------------------------------------------
# 节点生命周期
# ---------------------------------------------------------------------------
def _port() -> int:
    parsed = urlparse(settings.vision_base_url)
    return parsed.port or 8001


def health(timeout: float = 5.0) -> dict[str, Any] | None:
    """探测节点状态，未启动或异常时返回 None。"""

    try:
        with httpx.Client(base_url=settings.vision_base_url, timeout=timeout) as client:
            resp = client.get("/health")
            resp.raise_for_status()
            payload = resp.json()
            return payload if payload.get("ok") else None
    except Exception as exc:  # pragma: no cover - 探测失败属正常分支
        logger.debug("视觉节点探测失败：%s", exc)
        return None


def _spawn(model: str | None = None) -> subprocess.Popen:
    script = PROJECT_DIR / "vision_service" / "infer_server.py"
    if not Path(settings.vision_python).exists():
        raise VisionError(f"推理环境解释器不存在：{settings.vision_python}")
    if not script.exists():
        raise VisionError(f"推理节点脚本不存在：{script}")

    model_path = Path(model or settings.vision_model)
    if not model_path.exists():
        raise VisionError(f"权重文件不存在：{model_path}")

    log_path = settings.db_path.parent / "vision_node.log"
    handle = open(log_path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115 - 子进程存活期间保持打开
    cmd = [
        settings.vision_python,
        str(script),
        "--model",
        str(model_path),
        "--device",
        settings.vision_device,
        "--host",
        urlparse(settings.vision_base_url).hostname or "127.0.0.1",
        "--port",
        str(_port()),
    ]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    logger.info("启动视觉推理节点：%s", " ".join(cmd))
    handle.write(f"\n--- {datetime.now():%Y-%m-%d %H:%M:%S} 启动推理节点 ---\n")
    return subprocess.Popen(
        cmd,
        cwd=str(PROJECT_DIR),
        stdout=handle,
        stderr=subprocess.STDOUT,
        creationflags=creationflags,
    )


def ensure_service(wait_seconds: float = 120.0) -> bool:
    """确保推理节点可用：已在线则直接复用，否则按配置拉起并等待就绪。"""

    global _PROCESS, _LOG_HANDLE, _LAST_ERROR

    if not settings.vision_enabled:
        return False
    if health() is not None:
        logger.info("视觉推理节点已在线：%s", settings.vision_base_url)
        return True
    if not settings.vision_autostart:
        _LAST_ERROR = "推理节点未运行，且已禁用自动拉起"
        return False

    try:
        _PROCESS = _spawn()
    except Exception as exc:
        _LAST_ERROR = str(exc)
        logger.error("视觉推理节点启动失败：%s", exc)
        return False

    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if _PROCESS.poll() is not None:
            _LAST_ERROR = f"推理节点进程提前退出，返回码 {_PROCESS.returncode}，详见 data/vision_node.log"
            logger.error(_LAST_ERROR)
            return False
        if health(timeout=2.0) is not None:
            logger.info("视觉推理节点就绪（耗时约 %.1f 秒）", wait_seconds - (deadline - time.time()))
            _LAST_ERROR = None
            return True
        time.sleep(1.5)

    _LAST_ERROR = f"等待 {wait_seconds:.0f} 秒仍未就绪，详见 data/vision_node.log"
    logger.error(_LAST_ERROR)
    return False


def shutdown() -> None:
    """仅在由本进程拉起时关闭节点，避免误杀用户手动启动的服务。"""

    global _PROCESS
    if _PROCESS is None:
        return
    try:
        if _PROCESS.poll() is None:
            _PROCESS.terminate()
            try:
                _PROCESS.wait(timeout=8)
            except subprocess.TimeoutExpired:
                _PROCESS.kill()
    except Exception as exc:  # pragma: no cover - 退出阶段尽力而为
        logger.debug("关闭推理节点异常：%s", exc)
    finally:
        _PROCESS = None


# ---------------------------------------------------------------------------
# 调用节点
# ---------------------------------------------------------------------------
def _post(path: str, *, content: bytes | None = None, json_body: dict | None = None,
          params: dict | None = None, timeout: float | None = None) -> dict[str, Any]:
    if not settings.vision_enabled:
        raise VisionError("视觉识别功能已在配置中关闭")
    try:
        with httpx.Client(base_url=settings.vision_base_url, timeout=timeout or settings.vision_timeout_seconds) as client:
            resp = client.post(path, content=content, json=json_body, params=params)
    except httpx.HTTPError as exc:
        raise VisionError(f"无法连接视觉推理节点（{settings.vision_base_url}）：{exc}") from exc

    if resp.status_code >= 400:
        detail = ""
        try:
            detail = resp.json().get("error", "")
        except Exception:  # pragma: no cover - 非 JSON 错误体
            detail = resp.text[:200]
        raise VisionError(f"推理节点返回错误（{resp.status_code}）：{detail or '未知原因'}")
    return resp.json()


def enrich(result: dict[str, Any]) -> dict[str, Any]:
    """把节点输出的通用类别映射为平台业务语义。"""

    detections = []
    for item in result.get("detections") or []:
        name = item.get("name")
        policy = VISION_FOCUS.get(name)
        enriched = dict(item)
        enriched["label"] = policy["label"] if policy else item.get("label") or name
        enriched["category"] = policy["category"] if policy else "其他目标"
        enriched["level"] = policy["level"] if policy else "info"
        enriched["focus"] = bool(policy)
        detections.append(enriched)
    result["detections"] = detections

    focus_hits = [d for d in detections if d["focus"]]
    result["focus_hits"] = focus_hits
    result["focus_summary"] = _focus_summary(focus_hits)
    result["has_focus"] = bool(focus_hits)
    result["source"] = result.get("source")
    result["captured_at"] = datetime.now().isoformat(timespec="seconds")
    return result


def _focus_summary(hits: list[dict[str, Any]]) -> str:
    if not hits:
        return "本帧未识别到关注目标"
    parts: list[str] = []
    for item in hits:
        parts.append(f"{item['label']}（置信度 {item['confidence'] * 100:.1f}%）")
    return "识别到" + "、".join(parts)


def detect_bytes(data: bytes, conf: float | None = None, source: str | None = None) -> dict[str, Any]:
    if not data:
        raise VisionError("未提供图像数据")
    result = _post(
        "/detect",
        content=data,
        params={"conf": conf if conf is not None else settings.vision_conf},
    )
    result["source"] = source
    return enrich(result)


def detect_base64(payload: str, conf: float | None = None) -> dict[str, Any]:
    raw = payload.split(",", 1)[-1] if payload.startswith("data:") else payload
    try:
        data = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise VisionError("图像 Base64 解码失败，请确认上传的是图片文件") from exc
    return detect_bytes(data, conf=conf, source="upload")


def scan_video(video_path: str, sample_fps: float = 1.0, max_frames: int = 120,
               conf: float | None = None) -> dict[str, Any]:
    result = _post(
        "/scan",
        json_body={
            "video_path": video_path,
            "sample_fps": sample_fps,
            "max_frames": max_frames,
            "conf": conf if conf is not None else settings.vision_conf,
            "preview_frames": 3,
        },
        timeout=max(settings.vision_timeout_seconds, 600.0),
    )
    for item in result.get("classes") or []:
        policy = VISION_FOCUS.get(item.get("name"))
        item["label"] = policy["label"] if policy else item.get("label") or item.get("name")
        item["category"] = policy["category"] if policy else "其他目标"
        item["level"] = policy["level"] if policy else "info"
        item["focus"] = bool(policy)
    result["focus_classes"] = [c for c in result.get("classes") or [] if c["focus"]]
    result["captured_at"] = datetime.now().isoformat(timespec="seconds")
    return result


# ---------------------------------------------------------------------------
# 样本影像
# ---------------------------------------------------------------------------
def _manifest() -> dict[str, dict]:
    path = settings.vision_samples_dir / MANIFEST_NAME
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - 清单损坏时降级为无描述
        logger.warning("样本清单解析失败：%s", exc)
        return {}
    items = raw.get("samples") if isinstance(raw, dict) else raw
    return {str(item["file"]): item for item in (items or []) if item.get("file")}


def samples() -> list[dict[str, Any]]:
    """列出内置演示样本影像（仅登记目录中真实存在的文件）。"""

    directory = settings.vision_samples_dir
    manifest = _manifest()
    out: list[dict[str, Any]] = []
    for path in sorted(directory.iterdir() if directory.exists() else []):
        if not path.is_file() or path.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        meta = manifest.get(path.name, {})
        out.append(
            {
                "name": path.name,
                "title": meta.get("title") or path.stem,
                "scene": meta.get("scene") or "未分类",
                "source": meta.get("source") or "平台内置样本",
                "note": meta.get("note") or "",
                "size_kb": round(path.stat().st_size / 1024, 1),
            }
        )
    return out


def sample_bytes(name: str) -> tuple[bytes, dict[str, Any]]:
    safe = Path(name).name
    path = settings.vision_samples_dir / safe
    if path.parent.resolve() != settings.vision_samples_dir.resolve() or not path.exists():
        raise VisionError(f"样本影像不存在：{name}")
    if path.suffix.lower() not in IMAGE_SUFFIXES:
        raise VisionError("仅支持图片格式的样本影像")
    meta = next((s for s in samples() if s["name"] == safe), {"name": safe, "title": safe})
    return path.read_bytes(), meta


def detect_sample(name: str, conf: float | None = None) -> dict[str, Any]:
    data, meta = sample_bytes(name)
    result = detect_bytes(data, conf=conf, source=f"sample:{meta['name']}")
    result["sample"] = meta
    return result


# ---------------------------------------------------------------------------
# 告警闭环
# ---------------------------------------------------------------------------
def create_alert(
    db: Session,
    device: Device | None,
    *,
    label: str,
    confidence: float,
    note: str | None = None,
    operator: str | None = None,
    ip: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把一次人工确认的识别结果写入告警表，走既有去重、通知与处置流程。"""

    policy = VISION_FOCUS.get(label)
    if policy is None:
        raise VisionError(f"该类别未纳入告警策略：{label}")
    if device is None:
        raise VisionError("请先选择点位后再生成告警")

    engine = AlertEngine(db)
    alert = engine.raise_vision_alert(
        device,
        {
            "label": policy["label"],
            "title": policy["title"],
            "category": policy["category"],
            "level": policy["level"],
            "advice": policy["advice"],
            "confidence": float(confidence),
            "note": note or "",
            "extra": extra or {},
        },
    )
    db.add(
        AuditLog(
            username=operator,
            action="vision.alert.create",
            target=device.device_code,
            detail={"label": label, "confidence": confidence, "note": note},
            ip=ip,
        )
    )
    db.commit()

    return {
        "created": alert is not None,
        "alert": (
            {
                "id": alert.id,
                "alert_code": alert.alert_code,
                "level": alert.level,
                "type": alert.type,
                "title": alert.title,
                "content": alert.content,
                "device_code": alert.device_code,
                "created_at": alert.created_at.isoformat(timespec="seconds"),
            }
            if alert
            else None
        ),
        "message": (
            f"已生成告警 {alert.alert_code}"
            if alert
            else "同一目标在抑制窗口内已有未处置告警，本次仅累加出现次数"
        ),
        "policy": policy,
    }


# ---------------------------------------------------------------------------
# 权重管理
# ---------------------------------------------------------------------------
def list_models() -> list[dict[str, Any]]:
    """列出可切换的本地权重（backend/data/models 下的 .pt 文件）。"""

    directory = settings.db_path.parent / "models"
    out: list[dict[str, Any]] = []
    if directory.exists():
        for path in sorted(directory.glob("*.pt")):
            out.append(
                {
                    "name": path.name,
                    "size_mb": round(path.stat().st_size / 1024 / 1024, 2),
                    "active": Path(settings.vision_model).name == path.name,
                }
            )
    return out


def switch_model(name: str, wait_seconds: float = 90.0) -> dict[str, Any]:
    """用指定权重重启推理节点，仅对由平台拉起的节点生效。"""

    global _PROCESS, _LAST_ERROR

    target = settings.db_path.parent / "models" / Path(name).name
    if not target.exists():
        raise VisionError(f"权重文件不存在：{name}")
    if _PROCESS is None:
        raise VisionError("推理节点由平台外部启动，请重启节点时通过 --model 指定权重")

    logger.info("切换视觉权重：%s", target.name)
    shutdown()
    try:
        _PROCESS = _spawn(str(target))
    except Exception as exc:
        raise VisionError(f"重启推理节点失败：{exc}") from exc

    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if _PROCESS.poll() is not None:
            _LAST_ERROR = f"节点启动后退出，返回码 {_PROCESS.returncode}，详见 data/vision_node.log"
            raise VisionError(_LAST_ERROR)
        info = health(timeout=2.0)
        if info is not None:
            _LAST_ERROR = None
            return {"ok": True, "model": info.get("model"), "device": info.get("device")}
        time.sleep(1.5)
    raise VisionError(f"切换后 {wait_seconds:.0f} 秒内节点仍未就绪")


# ---------------------------------------------------------------------------
# 状态汇总
# ---------------------------------------------------------------------------
def camera_devices(db: Session) -> list[dict[str, Any]]:
    rows = db.execute(
        select(Device).where(Device.type == "camera").order_by(Device.device_code)
    ).scalars().all()
    return [
        {
            "device_code": d.device_code,
            "name": d.name,
            "region_code": d.region_code,
            "site": (d.config or {}).get("site"),
            "status": d.status,
            "longitude": d.longitude,
            "latitude": d.latitude,
            "last_seen": d.last_seen.isoformat(timespec="seconds") if d.last_seen else None,
        }
        for d in rows
    ]


def status_payload(db: Session) -> dict[str, Any]:
    info = health()
    model_name = (info or {}).get("model") or Path(settings.vision_model).name
    if "marine_litter" in model_name:
        algorithm = f"YOLO11s 海漂垃圾目标检测（自训练权重 {model_name}，GPU 前向推理）"
    else:
        algorithm = f"YOLO 目标检测（预训练权重 {model_name}，GPU 前向推理）"
    return {
        "enabled": settings.vision_enabled,
        "online": info is not None,
        "endpoint": settings.vision_base_url,
        "autostart": settings.vision_autostart,
        "python": settings.vision_python,
        "model": model_name,
        "device": (info or {}).get("device"),
        "device_name": (info or {}).get("device_name"),
        "imgsz": (info or {}).get("imgsz"),
        "half": (info or {}).get("half"),
        "started_at": (info or {}).get("started_at"),
        "requests": (info or {}).get("requests", 0),
        "objects": (info or {}).get("objects", 0),
        "avg_ms": (info or {}).get("avg_ms"),
        "max_ms": (info or {}).get("max_ms"),
        "conf_threshold": settings.vision_conf,
        "focus_classes": [
            {"name": key, **value} for key, value in VISION_FOCUS.items()
        ],
        "cameras": camera_devices(db),
        "available_models": list_models(),
        "samples": samples(),
        "last_error": _LAST_ERROR or (info or {}).get("last_error"),
        "algorithm": algorithm,
        "capability_note": (
            "节点对输入画面逐帧执行真实前向推理；未识别到关注目标时如实返回空结果，"
            "不做任何结果补全。自定义场景可通过替换权重文件扩展识别类别。"
        ),
    }
