"""实时事件总线：WebSocket 广播 + 多通道推送适配。

设计要点：仿真器/聚合器等后台线程可以同步调用 `broadcast`，
由事件总线把消息投递到主事件循环中的 WebSocket 连接。
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any

import httpx
from fastapi import WebSocket

from ..config import settings

logger = logging.getLogger("deepblue.notifier")


class ConnectionManager:
    """WebSocket 连接池。"""

    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()
        self._stats = {"connected": 0, "sent": 0, "dropped": 0}

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections.add(websocket)
            self._stats["connected"] = len(self._connections)

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._connections.discard(websocket)
            self._stats["connected"] = len(self._connections)

    async def broadcast(self, event: dict[str, Any]) -> None:
        if not self._connections:
            return
        payload = json.dumps(event, ensure_ascii=False, default=str)
        dead: list[WebSocket] = []
        for ws in list(self._connections):
            try:
                await ws.send_text(payload)
                self._stats["sent"] += 1
            except Exception:
                self._stats["dropped"] += 1
                dead.append(ws)
        for ws in dead:
            await self.disconnect(ws)

    @property
    def stats(self) -> dict[str, int]:
        return dict(self._stats, connected=len(self._connections))


class EventBus:
    """线程安全的事件总线。"""

    def __init__(self) -> None:
        self.manager = ConnectionManager()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.recent: list[dict[str, Any]] = []
        self.max_recent = 200

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    def broadcast(self, event_type: str, data: dict[str, Any]) -> None:
        """可从前台或后台线程调用。"""

        event = {"type": event_type, "ts": datetime.now().isoformat(timespec="seconds"), "data": data}
        self.recent.append(event)
        if len(self.recent) > self.max_recent:
            del self.recent[: len(self.recent) - self.max_recent]
        if self.loop is None or self.loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(self.manager.broadcast(event), self.loop)
        except Exception as exc:  # pragma: no cover
            logger.debug("广播失败: %s", exc)


bus = EventBus()


def push_webhook(url: str, payload: dict[str, Any], channel: str) -> bool:
    """向外部网关（短信/企业微信）推送，未配置时直接返回 False。"""

    if not url:
        return False
    try:
        with httpx.Client(timeout=6.0) as client:
            resp = client.post(url, json=payload)
            resp.raise_for_status()
        logger.info("[%s] 推送成功 alert=%s", channel, payload.get("alert_code"))
        return True
    except Exception as exc:  # pragma: no cover
        logger.warning("[%s] 推送失败: %s", channel, exc)
        return False


def notify_alert(alert_payload: dict[str, Any]) -> list[dict[str, Any]]:
    """多通道告警推送，返回每个通道的结果。"""

    results: list[dict[str, Any]] = []
    bus.broadcast("alert", alert_payload)
    results.append({"channel": "websocket", "status": "sent"})

    sms_ok = push_webhook(settings.sms_webhook, alert_payload, "sms")
    results.append({"channel": "sms", "status": "sent" if sms_ok else "skipped"})

    wechat_ok = push_webhook(settings.wechat_webhook, alert_payload, "wechat")
    results.append({"channel": "wechat", "status": "sent" if wechat_ok else "skipped"})

    logger.warning("[告警] %s | %s", alert_payload.get("title"), alert_payload.get("content"))
    return results
