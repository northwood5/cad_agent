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
from agents.cad import SUPPORTED_PROVIDERS

from db import init_db, repository as repo
from services.session_service import SessionManager, ProjectSession
from services.workflow_service import WorkflowService

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


# ── Session registry (per project) ──────────────────────────────────────────
session_manager = SessionManager()


def _project_workspace(project_id: int) -> Path:
    out = OUTPUT_DIR / str(project_id)
    out.mkdir(parents=True, exist_ok=True)
    return out


def _get_session(project_id: int) -> ProjectSession:
    return session_manager.get_or_create(
        project_id, _active_llm_cfg(), _project_workspace(project_id)
    )


# ── FastAPI app ─────────────────────────────────────────────────────────────
app = FastAPI(title="CAD Agent", version="0.1.0")
app.add_middleware(CORSMiddleware,
                   allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Initialise the SQLite persistence layer (users / projects / history).
init_db()


# ── WebSocket ───────────────────────────────────────────────────────────────

@app.websocket("/ws/chat/{project_id}")
async def ws_chat(websocket: WebSocket, project_id: int):
    await websocket.accept()
    logger.info("WS connected: project=%s", project_id)
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue

            action = data.get("action", "chat")

            # ── reset project session (clears in-memory geometry state) ──
            if action == "new_session":
                session_manager.drop(project_id)
                _get_session(project_id)
                await websocket.send_json(
                    {"type": "session_ready", "session_id": project_id})
                continue

            if action != "chat":
                continue

            user_text: str = data.get("text", "").strip()
            if not user_text:
                continue

            session = _get_session(project_id)

            # ── inject current scene state so agents stay context-aware ──
            scene_state = ""
            scene = session.cad_scene
            if scene is not None:
                shapes = scene.list_shapes()
                if shapes["count"] > 0:
                    scene_state = json.dumps(
                        [{"name": s["name"], "bounds": s["bounds"]}
                         for s in shapes["shapes"]],
                        ensure_ascii=False)

            # Persist the user's message.
            repo.add_message(project_id, "user", user_text)

            await websocket.send_json({"type": "agent_start"})

            agent_reply_buf: list[str] = []
            try:
                workflow = WorkflowService(session, _active_llm_cfg())
                async for payload in workflow.execute(user_text, scene_state):
                    if payload.get("type") == "text_delta":
                        agent_reply_buf.append(payload.get("text", ""))
                    await websocket.send_json(payload)
            except Exception as exc:
                logger.exception("Workflow error  project=%s", project_id)
                await websocket.send_json(
                    {"type": "error", "message": str(exc)})

            # Persist the agent's aggregated reply text.
            reply_text = "".join(agent_reply_buf).strip()
            if reply_text:
                repo.add_message(project_id, "agent", reply_text)

            await websocket.send_json({"type": "agent_done"})

    except WebSocketDisconnect:
        logger.info("WS disconnected: project=%s", project_id)


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

    session_manager.clear()   # recreate sessions with new model on next request
    return JSONResponse({
        "status": "ok",
        "active_provider": _config["active_provider"],
        "model_name": _config["providers"][_config["active_provider"]].get("model_name"),
    })


# ── REST: scene data ────────────────────────────────────────────────────────

@app.get("/api/sessions/{project_id}/shapes")
async def get_scene_shapes(project_id: int):
    session = session_manager.get(project_id)
    scene = session.cad_scene if session else None
    if scene is None:
        return JSONResponse({"shapes": [], "count": 0})
    return JSONResponse(scene.list_shapes())


# ── REST: model file download ───────────────────────────────────────────────

@app.get("/api/models/{project_id}/{filename}")
async def serve_model(project_id: int, filename: str):
    filepath = OUTPUT_DIR / str(project_id) / filename
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

@app.get("/api/sessions/{project_id}/export/step")
async def export_step(project_id: int):
    """Export the current project's FreeCAD document as a STEP file on demand."""
    session = session_manager.get(project_id)
    scene = session.cad_scene if session else None
    if scene is None or not scene.shapes:
        return JSONResponse({"error": "scene is empty"}, status_code=400)

    out_dir = _project_workspace(project_id)
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

    repo.add_artifact(project_id, "step", step_path.name, str(step_path))
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
