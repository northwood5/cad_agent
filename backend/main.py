# -*- coding: utf-8 -*-
"""
FastAPI backend for CAD Agent  (P1-P7 complete).

Routes
──────
WS   /ws/chat/{session_id}             streaming agent events
GET  /api/config                        read LLM config
POST /api/config                        update + persist LLM config
GET  /api/sessions/{sid}/history        list exported model files
GET  /api/sessions/{sid}/shapes         current scene shapes
GET  /api/models/{sid}/{filename}       serve generated STL/OBJ
GET  /                                  frontend SPA
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

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
from agentscope.message import UserMsg

from agent.cad_agent import build_agent, SUPPORTED_PROVIDERS

# ── Paths ──────────────────────────────────────────────────────────────────
BASE_DIR     = Path(__file__).parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"
CONFIG_PATH  = BASE_DIR / "backend" / "config" / "llm_config.yaml"
OUTPUT_DIR   = BASE_DIR / "backend" / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s  %(message)s",
)
logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────

def _load_config() -> dict[str, Any]:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        raw: dict = yaml.safe_load(f)

    env_map = {
        "openai":    "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "dashscope": "DASHSCOPE_API_KEY",
        "deepseek":  "DEEPSEEK_API_KEY",
    }
    for provider, env_var in env_map.items():
        val = os.environ.get(env_var, "")
        if val and provider in raw.get("providers", {}):
            raw["providers"][provider]["api_key"] = val
    return raw


def _save_config(cfg: dict[str, Any]) -> None:
    """Persist the runtime config back to disk (overwrites comments)."""
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False,
                  sort_keys=False)


_config: dict[str, Any] = _load_config()


def _active_llm_cfg() -> dict[str, Any]:
    provider = _config.get("active_provider", "openai")
    return dict(_config["providers"][provider])


# ── Session registry ────────────────────────────────────────────────────────
#   session_id → { agent, scene, model_history: [{filename, url, ts}] }
_sessions: dict[str, Any] = {}


def _get_or_create(sid: str) -> dict[str, Any]:
    if sid not in _sessions:
        out = OUTPUT_DIR / sid
        out.mkdir(parents=True, exist_ok=True)
        agent, scene = build_agent(_active_llm_cfg(), out)
        _sessions[sid] = {"agent": agent, "scene": scene, "model_history": []}
        logger.info("New session %s  provider=%s", sid, _config.get("active_provider"))
    return _sessions[sid]


# ── Event serialiser (AgentScope 2.x) ──────────────────────────────────────

def _to_json(
    evt: Any,
    call_buf: dict[str, str],
    res_buf:  dict[str, str],
) -> dict[str, Any] | None:
    """
    AgentScope 2.x correct field names:
      ToolCallStartEvent  → tool_call_id, tool_call_name
      ToolCallDeltaEvent  → tool_call_id, delta
      ToolCallEndEvent    → tool_call_id   (NO arguments field)
      ToolResultStart     → tool_call_id, tool_call_name
      ToolResultTextDelta → tool_call_id, delta
      ToolResultEndEvent  → tool_call_id, state
    """
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


# ── FastAPI app ─────────────────────────────────────────────────────────────
app = FastAPI(title="CAD Agent", version="0.1.0")
app.add_middleware(CORSMiddleware,
                   allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ── WebSocket ───────────────────────────────────────────────────────────────

@app.websocket("/ws/chat/{session_id}")
async def ws_chat(websocket: WebSocket, session_id: str):
    await websocket.accept()
    logger.info("WS connected: %s", session_id)
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue

            action = data.get("action", "chat")

            # ── new session / reset ──
            if action == "new_session":
                _sessions.pop(session_id, None)
                _get_or_create(session_id)
                await websocket.send_json(
                    {"type": "session_ready", "session_id": session_id})
                continue

            if action != "chat":
                continue

            user_text: str = data.get("text", "").strip()
            if not user_text:
                continue

            sess  = _get_or_create(session_id)
            agent = sess["agent"]
            scene = sess["scene"]

            # ── P7: inject current scene state so agent is aware ──
            scene_state = scene.list_shapes()
            if scene_state["count"] > 0:
                shapes_brief = json.dumps(
                    [{"name": s["name"], "bounds": s["bounds"]}
                     for s in scene_state["shapes"]],
                    ensure_ascii=False)
                ctx_text = (
                    f"[场景上下文 — 当前已有 {scene_state['count']} 个形状: "
                    f"{shapes_brief}]\n\n{user_text}"
                )
            else:
                ctx_text = user_text

            await websocket.send_json({"type": "agent_start"})

            call_buf: dict[str, str] = {}
            res_buf:  dict[str, str] = {}
            new_exports: list[str]   = []

            try:
                async for evt in agent.reply_stream(UserMsg(name="user", content=ctx_text)):
                    payload = _to_json(evt, call_buf, res_buf)
                    if payload:
                        await websocket.send_json(payload)

                    # Detect export_model results
                    if isinstance(evt, ToolResultEndEvent):
                        result_text = res_buf.get(evt.tool_call_id, "")
                        # res_buf already popped; use payload
                        result_text = (payload or {}).get("result", "")
                        try:
                            parsed = json.loads(result_text)
                            if parsed.get("success") and "filename" in parsed:
                                new_exports.append(parsed["filename"])
                        except Exception:
                            pass

            except Exception as exc:
                logger.exception("Agent error  session=%s", session_id)
                await websocket.send_json(
                    {"type": "error", "message": str(exc)})

            # Notify frontend of each new model file
            for fname in new_exports:
                entry = {
                    "filename": fname,
                    "url":  f"/api/models/{session_id}/{fname}",
                    "timestamp": datetime.now().strftime("%H:%M:%S"),
                }
                sess["model_history"].append(entry)
                await websocket.send_json({"type": "model_ready", **entry})

            await websocket.send_json({"type": "agent_done"})

    except WebSocketDisconnect:
        logger.info("WS disconnected: %s", session_id)


# ── REST: LLM config ────────────────────────────────────────────────────────

@app.get("/api/config")
async def get_config():
    return JSONResponse({
        "active_provider": _config.get("active_provider"),
        "supported_providers": SUPPORTED_PROVIDERS,
        "providers": {
            k: {
                "provider":    v.get("provider"),
                "model_name":  v.get("model_name"),
                "base_url":    v.get("base_url"),
                "stream":      v.get("stream", True),
                "has_api_key": bool(v.get("api_key")),
            }
            for k, v in _config.get("providers", {}).items()
        },
    })


@app.post("/api/config")
async def update_config(body: dict):
    """
    Body: { "active_provider": "deepseek",
            "provider_config": {"api_key": "...", "model_name": "deepseek-chat"} }
    Changes are persisted to llm_config.yaml.
    """
    if "active_provider" in body:
        p = body["active_provider"]
        if p not in _config.get("providers", {}):
            return JSONResponse({"error": f"Unknown provider: {p}"}, status_code=400)
        _config["active_provider"] = p

    if "provider_config" in body:
        provider = _config["active_provider"]
        _config["providers"][provider].update(body["provider_config"])

    # Persist to disk
    try:
        _save_config(_config)
    except Exception as e:
        logger.warning("Config save failed: %s", e)

    _sessions.clear()   # recreate sessions with new model on next request
    return JSONResponse({
        "status": "ok",
        "active_provider": _config["active_provider"],
        "model_name": _config["providers"][_config["active_provider"]].get("model_name"),
    })


# ── REST: session data ──────────────────────────────────────────────────────

@app.get("/api/sessions/{session_id}/history")
async def get_model_history(session_id: str):
    sess = _sessions.get(session_id)
    if not sess:
        return JSONResponse({"history": []})
    return JSONResponse({"history": sess["model_history"]})


@app.get("/api/sessions/{session_id}/shapes")
async def get_scene_shapes(session_id: str):
    sess = _sessions.get(session_id)
    if not sess:
        return JSONResponse({"shapes": [], "count": 0})
    return JSONResponse(sess["scene"].list_shapes())


# ── REST: model file download ───────────────────────────────────────────────

@app.get("/api/models/{session_id}/{filename}")
async def serve_model(session_id: str, filename: str):
    filepath = OUTPUT_DIR / session_id / filename
    if not filepath.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    media = "model/stl" if filename.endswith(".stl") else "application/octet-stream"
    return FileResponse(
        str(filepath),
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Static frontend ─────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return FileResponse(str(FRONTEND_DIR / "index.html"))

app.mount("/css", StaticFiles(directory=str(FRONTEND_DIR / "css")), name="css")
app.mount("/js",  StaticFiles(directory=str(FRONTEND_DIR / "js")),  name="js")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
