"""海洋生态智能体接口：状态、工具清单、问答（普通 / 流式）。

鉴权策略：匿名与观察者可使用全部只读问答能力，便于大屏随时演示；
涉及写操作的工具（执行巡检、生成处置草稿）由服务层按角色拦截，并写入 AuditLog。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from ..config import settings
from ..database import SessionLocal
from ..deps import DbSession, OptionalUser
from ..schemas import AgentChatRequest
from ..services import agent as agent_service

logger = logging.getLogger("deepblue.agent.api")

router = APIRouter(prefix="/agent", tags=["海洋生态智能体"])

EXAMPLES = [
    "现在整体情况怎么样？有哪些需要重点关注的风险？",
    "黄岐湾中部浮标最新的水质如何，有没有指标越限？",
    "最近 24 小时有哪些告警，哪些还没处置？",
    "未来 48 小时赤潮风险最高的点位在哪里？",
    "马鼻镇浮曦村红树林最近一期的面积和健康度如何？",
    "针对当前最高风险的站点，给我一份处置建议草稿。",
]


@router.get("/status", summary="智能体运行状态与工具清单")
def status(db: DbSession) -> dict:
    return {"code": 0, "data": agent_service.status_payload(db)}


@router.get("/examples", summary="推荐问法示例")
def examples() -> dict:
    return {"code": 0, "data": EXAMPLES}


@router.post("/chat", summary="海洋生态智能体问答")
def chat(payload: AgentChatRequest, db: DbSession, user: OptionalUser) -> dict:
    if not settings.agent_enabled:
        raise HTTPException(status_code=503, detail="智能体功能未启用")
    result = agent_service.chat(db, payload.question.strip(), user, payload.history)
    return {"code": 0, "data": result}


@router.post("/chat/stream", summary="海洋生态智能体流式问答（SSE）")
def chat_stream(payload: AgentChatRequest, user: OptionalUser) -> StreamingResponse:
    if not settings.agent_enabled:
        raise HTTPException(status_code=503, detail="智能体功能未启用")

    question = payload.question.strip()
    history = payload.history

    def event_stream() -> Iterator[str]:
        # 流式响应期间 FastAPI 会先回收请求级依赖，因此这里独立管理会话生命周期
        db = SessionLocal()
        try:
            for event in agent_service.chat_stream(db, question, user, history):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception:  # pragma: no cover - 保证前端始终收到终止事件
            logger.exception("智能体流式问答异常")
            yield f"data: {json.dumps({'type': 'error', 'message': '智能体服务异常，请稍后重试'}, ensure_ascii=False)}\n\n"
        finally:
            db.close()
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream; charset=utf-8",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
