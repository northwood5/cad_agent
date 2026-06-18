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

from agents.cad.tools import freecad_bridge

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

from agents.cad import build_agent, SUPPORTED_PROVIDERS

from db import init_db, repository as repo

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

# Initialise the SQLite persistence layer (users / projects / history).
init_db()


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
    _media_map = {".stl": "model/stl", ".obj": "model/obj", ".step": "application/step", ".stp": "application/step"}
    media = _media_map.get(Path(filename).suffix.lower(), "application/octet-stream")
    return FileResponse(
        str(filepath),
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── REST: on-demand STEP export ─────────────────────────────────────────────

@app.get("/api/sessions/{session_id}/export/step")
async def export_step(session_id: str):
    """Export the current session's FreeCAD document as a STEP file on demand."""
    sess = _sessions.get(session_id)
    if not sess:
        return JSONResponse({"error": "session not found"}, status_code=404)

    scene = sess["scene"]
    if not scene.shapes:
        return JSONResponse({"error": "scene is empty"}, status_code=400)

    out_dir = OUTPUT_DIR / session_id
    step_path = out_dir / f"export_{uuid.uuid4().hex[:8]}.step"

    if scene.fc_doc_path.exists():
        result = await freecad_bridge.fc_export_step(scene.fc_doc_path, step_path)
    else:
        # Fallback: export trimesh STL then convert
        import trimesh, trimesh.util
        shapes = list(scene.shapes.values())
        merged = shapes[0] if len(shapes) == 1 else trimesh.util.concatenate(shapes)
        tmp_stl = out_dir / f"_tmp_{uuid.uuid4().hex[:8]}.stl"
        merged.export(str(tmp_stl))
        result = await freecad_bridge.stl_to_step(tmp_stl, step_path)
        tmp_stl.unlink(missing_ok=True)

    if not result.get("success"):
        return JSONResponse({"error": result.get("error", "Export failed")}, status_code=500)

    entry = {
        "filename": step_path.name,
        "url": f"/api/models/{session_id}/{step_path.name}",
        "timestamp": datetime.now().strftime("%H:%M:%S"),
    }
    sess["model_history"].append(entry)
    return FileResponse(
        str(step_path),
        media_type="application/step",
        headers={"Content-Disposition": f'attachment; filename="{step_path.name}"'},
    )


# ── REST: lightweight user management ───────────────────────────────────────

@app.post("/api/users/login")
async def user_login(body: dict):
    """Get-or-create a user by username (no password — lightweight identity)."""
    username = (body.get("username") or "").strip()
    if not username:
        return JSONResponse({"error": "username required"}, status_code=400)
    user = repo.get_or_create_user(username)
    return JSONResponse(user)


@app.get("/api/users/{user_id}/projects")
async def get_user_projects(user_id: int):
    if repo.get_user(user_id) is None:
        return JSONResponse({"error": "user not found"}, status_code=404)
    return JSONResponse({"projects": repo.list_projects(user_id)})


@app.post("/api/users/{user_id}/projects")
async def create_user_project(user_id: int, body: dict):
    if repo.get_user(user_id) is None:
        return JSONResponse({"error": "user not found"}, status_code=404)
    name = (body.get("name") or "未命名项目").strip()
    project = repo.create_project(user_id, name)
    return JSONResponse(project)


# ── REST: project data (history) ────────────────────────────────────────────

@app.get("/api/projects/{project_id}")
async def get_project(project_id: int):
    project = repo.get_project(project_id)
    if project is None:
        return JSONResponse({"error": "project not found"}, status_code=404)
    return JSONResponse(project)


@app.patch("/api/projects/{project_id}")
async def patch_project(project_id: int, body: dict):
    if repo.get_project(project_id) is None:
        return JSONResponse({"error": "project not found"}, status_code=404)
    name = (body.get("name") or "").strip()
    if name:
        repo.rename_project(project_id, name)
    return JSONResponse(repo.get_project(project_id))


@app.delete("/api/projects/{project_id}")
async def remove_project(project_id: int):
    repo.delete_project(project_id)
    return JSONResponse({"status": "ok"})


@app.get("/api/projects/{project_id}/messages")
async def get_project_messages(project_id: int):
    return JSONResponse({"messages": repo.list_messages(project_id)})


@app.get("/api/projects/{project_id}/runs")
async def get_project_runs(project_id: int):
    return JSONResponse({"runs": repo.list_runs(project_id)})


@app.get("/api/projects/{project_id}/scripts")
async def get_project_scripts(project_id: int):
    return JSONResponse({"scripts": repo.list_scripts(project_id)})


@app.get("/api/projects/{project_id}/artifacts")
async def get_project_artifacts(project_id: int):
    return JSONResponse({"artifacts": repo.list_artifacts(project_id)})


# ── Static frontend ─────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return FileResponse(str(FRONTEND_DIR / "index.html"))

app.mount("/css", StaticFiles(directory=str(FRONTEND_DIR / "css")), name="css")
app.mount("/js",  StaticFiles(directory=str(FRONTEND_DIR / "js")),  name="js")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
