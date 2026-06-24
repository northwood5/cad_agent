# -*- coding: utf-8 -*-
"""
Translate AgentScope 2.x events into the frontend's JSON event protocol.

Extracted from main.py so both the legacy single-agent path and the new
workflow service share one serialiser. When *node_id* / *agent* are supplied
(workflow execution) they are attached to every payload so the frontend can
attribute each reasoning/tool event to the workflow node that produced it.
"""
from __future__ import annotations

from typing import Any

from agentscope.event import (
    TextBlockDeltaEvent,
    TextBlockStartEvent,
    TextBlockEndEvent,
    ThinkingBlockStartEvent,
    ThinkingBlockDeltaEvent,
    ThinkingBlockEndEvent,
    ToolCallStartEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
    ToolResultEndEvent,
    ReplyStartEvent,
    ReplyEndEvent,
    ModelCallStartEvent,
    ModelCallEndEvent,
    ExceedMaxItersEvent,
)


def event_to_json(
    evt: Any,
    call_buf: dict[str, str],
    res_buf: dict[str, str],
    *,
    node_id: str | None = None,
    agent: str | None = None,
) -> dict[str, Any] | None:
    """Convert one AgentScope event to a frontend payload dict (or None to skip)."""
    payload = _convert(evt, call_buf, res_buf)
    if payload is not None and (node_id is not None or agent is not None):
        if node_id is not None:
            payload["node_id"] = node_id
        if agent is not None:
            payload["agent"] = agent
    return payload


def _convert(evt, call_buf, res_buf) -> dict[str, Any] | None:
    if isinstance(evt, (ReplyStartEvent, ReplyEndEvent,
                        ModelCallStartEvent, ModelCallEndEvent)):
        return None

    if isinstance(evt, ThinkingBlockStartEvent):
        return {"type": "thinking_start"}
    if isinstance(evt, ThinkingBlockDeltaEvent):
        return {"type": "thinking_delta", "text": evt.delta}
    if isinstance(evt, ThinkingBlockEndEvent):
        return {"type": "thinking_end"}

    if isinstance(evt, TextBlockStartEvent):
        return {"type": "text_start"}
    if isinstance(evt, TextBlockDeltaEvent):
        return {"type": "text_delta", "text": evt.delta}
    if isinstance(evt, TextBlockEndEvent):
        return {"type": "text_end"}

    if isinstance(evt, ToolCallStartEvent):
        call_buf[evt.tool_call_id] = ""
        return {"type": "tool_call_start",
                "tool": evt.tool_call_name, "id": evt.tool_call_id}

    if isinstance(evt, ToolCallDeltaEvent):
        call_buf[evt.tool_call_id] = call_buf.get(evt.tool_call_id, "") + evt.delta
        return {"type": "tool_call_delta",
                "delta": evt.delta, "id": evt.tool_call_id}

    if isinstance(evt, ToolCallEndEvent):
        args = call_buf.pop(evt.tool_call_id, "{}")
        return {"type": "tool_call_end", "args": args, "id": evt.tool_call_id}

    if isinstance(evt, ToolResultStartEvent):
        res_buf[evt.tool_call_id] = ""
        return {"type": "tool_result_start",
                "tool": evt.tool_call_name, "id": evt.tool_call_id}

    if isinstance(evt, ToolResultTextDeltaEvent):
        res_buf[evt.tool_call_id] = res_buf.get(evt.tool_call_id, "") + evt.delta
        return {"type": "tool_result_delta",
                "text": evt.delta, "id": evt.tool_call_id}

    if isinstance(evt, ToolResultEndEvent):
        result_text = res_buf.pop(evt.tool_call_id, "")
        return {"type": "tool_result_end",
                "id": evt.tool_call_id,
                "state": str(evt.state),
                "result": result_text}

    if isinstance(evt, ExceedMaxItersEvent):
        return {"type": "error", "message": "Agent 超过最大迭代次数，请简化请求"}

    return None
