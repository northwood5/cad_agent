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
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from agents.cad.tools import freecad_bridge
from agents.cad import SUPPORTED_PROVIDERS

from db import init_db, repository as repo
from services.session_service import SessionManager, ProjectSession
from services.workflow_service import WorkflowService, WorkflowController
from services.run_manager import RunManager

# ── Paths ──────────────────────────────────────────────────────────────────
BASE_DIR     = Path(__file__).parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"
CONFIG_PATH  = BASE_DIR / "backend" / "config" / "llm_config.yaml"
OUTPUT_DIR   = BASE_DIR / "backend" / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Load secrets from a gitignored .env (if present) before reading the config, so
# API keys live in the environment rather than in the committed/tracked tree.
load_dotenv(BASE_DIR / ".env")

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
        "zhipu":     "ZHIPU_API_KEY",
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
    cfg = dict(_config["providers"][provider])
    # Surface workflow-level settings alongside the provider config so the
    # WorkflowService/ReviewAgent can read them (e.g. self-heal iteration budget).
    if "max_repair_iterations" in _config:
        cfg["max_repair_iterations"] = _config["max_repair_iterations"]
    return cfg


# ── Session registry (per project) ──────────────────────────────────────────
session_manager = SessionManager()

# Connection-independent registry of in-flight workflow runs. Keeping a run here
# (rather than in the WebSocket handler's local state) lets a refreshed/reconnected
# page reattach to a run that is still executing instead of cancelling it.
run_manager = RunManager()


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

    # The run lives in the connection-independent registry, not here, so this
    # socket can drop (page refresh) without cancelling the workflow.
    active_run = run_manager.get_or_create(project_id)

    # Subscribe BEFORE snapshotting the log: subscription + snapshot happen with
    # no await between them, and _broadcast appends+enqueues atomically per loop
    # tick — so events emitted during replay land in the queue (not the snapshot),
    # giving exactly-once delivery with no gap.
    queue = active_run.subscribe()
    replay: list[dict] = list(active_run.event_log) if active_run.running else []

    async def drain() -> None:
        """Forward live events from this connection's queue to the socket."""
        while True:
            ev = await queue.get()
            await websocket.send_json(ev)

    # Rebuild the in-flight view on (re)connect, then go live via the queue.
    if replay:
        await websocket.send_json({"type": "replay_start"})
        for ev in replay:
            await websocket.send_json(ev)
        await websocket.send_json({"type": "replay_end"})

    sender = asyncio.create_task(drain())

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue

            action = data.get("action", "chat")

            # ── interrupt the running workflow ──
            if action == "interrupt":
                active_run.interrupt()
                continue

            # ── reset / rerun from a node (original instruction) ──
            if action == "reset_node":
                if active_run.busy or active_run.service is None:
                    continue
                node_id = data.get("node_id")
                if not node_id:
                    continue
                ctrl = WorkflowController()
                active_run.controller = ctrl
                active_run.start(active_run.service.rerun_from(node_id, ctrl))
                continue

            # ── rerun a node with a new natural-language instruction ──
            if action == "reset_node_with_instruction":
                if active_run.busy or active_run.service is None:
                    continue
                node_id = data.get("node_id")
                instruction = (data.get("instruction") or "").strip()
                if not node_id or not instruction:
                    continue
                ctrl = WorkflowController()
                active_run.controller = ctrl
                active_run.start(
                    active_run.service.rerun_from(node_id, ctrl, override_instruction=instruction)
                )
                continue

            # ── set a breakpoint on a workflow node ──
            if action == "set_breakpoint":
                node_id = data.get("node_id")
                if node_id and active_run.controller is not None:
                    active_run.controller.set_breakpoint(node_id)
                continue

            # ── remove a breakpoint from a workflow node ──
            if action == "remove_breakpoint":
                node_id = data.get("node_id")
                if node_id and active_run.controller is not None:
                    active_run.controller.remove_breakpoint(node_id)
                continue

            # ── resume a node paused at a breakpoint ──
            if action == "resume_node":
                node_id = data.get("node_id")
                instruction = (data.get("instruction") or "").strip() or None
                if node_id and active_run.controller is not None:
                    active_run.controller.resume(node_id, instruction)
                continue

            # ── reset project session (clears in-memory geometry state) ──
            if action == "new_session":
                # Interrupt any running workflow but keep the ActiveRun (and its
                # subscribers/queue) so this and other tabs stay attached. A stale
                # event_log won't replay since `running` flips False once stopped,
                # and the next chat clears it.
                active_run.interrupt()
                active_run.service = None
                session_manager.drop(project_id)
                _get_session(project_id)
                await websocket.send_json({"type": "session_ready", "session_id": project_id})
                continue

            if action != "chat":
                continue

            if active_run.busy:
                continue  # ignore new requests while a workflow runs

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

            repo.add_message(project_id, "user", user_text)

            active_run.service = WorkflowService(session, _active_llm_cfg())
            ctrl = WorkflowController()
            active_run.controller = ctrl
            active_run.start(active_run.service.execute(user_text, scene_state, ctrl))

    except WebSocketDisconnect:
        logger.info("WS disconnected: project=%s", project_id)
        # Detach this connection only — the run keeps executing and a reconnect
        # will replay the buffered events to catch up.
        active_run.unsubscribe(queue)
        sender.cancel()


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
    run_manager.clear()       # interrupt in-flight runs bound to the old model
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

@app.api_route("/api/models/{project_id}/{filepath:path}", methods=["GET", "HEAD"])
async def serve_model(project_id: int, filepath: str):
    # Prevent path traversal
    base = (OUTPUT_DIR / str(project_id)).resolve()
    target = (base / filepath).resolve()
    if not str(target).startswith(str(base)):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    if not target.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    filename = target.name
    _media_map = {
        ".stl": "model/stl", ".obj": "model/obj",
        ".step": "application/step", ".stp": "application/step",
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".svg": "image/svg+xml", ".webp": "image/webp",
        ".html": "text/html", ".htm": "text/html",
    }
    media = _media_map.get(target.suffix.lower(), "application/octet-stream")
    # Use inline disposition so browsers (and Three.js FileLoader) can read the
    # response body directly; attachment disposition interferes with XHR in some
    # configurations.
    return FileResponse(
        str(target),
        media_type=media,
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


# ── REST: project file management ───────────────────────────────────────────

def _build_file_tree(base_dir: Path) -> list[dict]:
    """Recursively list files and dirs under *base_dir*, returning relative paths."""
    if not base_dir.exists():
        return []

    def _scan(directory: Path) -> list[dict]:
        items: list[dict] = []
        try:
            entries = sorted(directory.iterdir(), key=lambda e: (e.is_file(), e.name.lower()))
        except PermissionError:
            return items
        for entry in entries:
            if entry.name.startswith('.') or entry.name.startswith('__'):
                continue
            rel = entry.relative_to(base_dir)
            if entry.is_dir():
                items.append({
                    "name": entry.name,
                    "type": "dir",
                    "path": rel.as_posix(),
                    "children": _scan(entry),
                })
            else:
                items.append({
                    "name": entry.name,
                    "type": "file",
                    "path": rel.as_posix(),
                    "size": entry.stat().st_size,
                    "ext": entry.suffix.lower(),
                })
        return items

    return _scan(base_dir)


@app.get("/api/projects/{project_id}/files")
async def get_project_files(project_id: int):
    project_dir = OUTPUT_DIR / str(project_id)
    return JSONResponse({"files": _build_file_tree(project_dir)})


@app.get("/api/projects/{project_id}/latest-stl")
async def get_latest_stl(project_id: int):
    """Return the URL of the most recently modified STL file in the project."""
    project_dir = OUTPUT_DIR / str(project_id)
    if not project_dir.exists():
        return JSONResponse({"url": None})
    stl_files = sorted(
        project_dir.rglob("*.stl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not stl_files:
        return JSONResponse({"url": None})
    rel = stl_files[0].relative_to(project_dir).as_posix()
    return JSONResponse({
        "url":      f"/api/models/{project_id}/{rel}",
        "filename": stl_files[0].name,
        "path":     rel,
    })


@app.get("/api/projects/{project_id}/file-content")
async def get_file_content(project_id: int, path: str):
    base = (OUTPUT_DIR / str(project_id)).resolve()
    target = (base / path).resolve()
    if not str(target).startswith(str(base)):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    if not target.exists() or not target.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    ext = target.suffix.lower()
    _IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp"}
    _HTML_EXTS  = {".html", ".htm"}
    _TEXT_EXTS  = {".py", ".txt", ".md", ".step", ".stp", ".inp", ".log",
                   ".json", ".yaml", ".yml", ".sh", ".csv", ".geo", ".cfg",
                   ".stl", ".obj"}
    # Images: return a URL the browser can use directly in <img>
    if ext in _IMAGE_EXTS:
        url = f"/api/models/{project_id}/{path}"
        return JSONResponse({"encoding": "image", "url": url})
    # HTML: return content as html encoding for sandboxed <iframe srcdoc>
    if ext in _HTML_EXTS:
        try:
            content = target.read_text(encoding="utf-8", errors="replace")
            return JSONResponse({"encoding": "html", "content": content})
        except Exception as exc:
            return JSONResponse({"encoding": "binary", "content": None, "error": str(exc)})
    if ext not in _TEXT_EXTS:
        return JSONResponse({"encoding": "binary", "content": None})
    try:
        raw = target.read_bytes()
        truncated = len(raw) > 1_000_000
        if truncated:
            raw = raw[:1_000_000]
        content = raw.decode("utf-8", errors="replace")
        if truncated:
            content += "\n\n… (文件过大，已截断显示前 1 MB)"
        return JSONResponse({"encoding": "text", "content": content})
    except Exception as exc:
        return JSONResponse({"encoding": "binary", "content": None, "error": str(exc)})


@app.delete("/api/projects/{project_id}/files")
async def delete_project_file(project_id: int, path: str):
    base = (OUTPUT_DIR / str(project_id)).resolve()
    target = (base / path).resolve()
    if not str(target).startswith(str(base)):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    if not target.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    if target.is_file():
        target.unlink()
    elif target.is_dir():
        shutil.rmtree(target)
    return JSONResponse({"status": "ok"})


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
    session_manager.drop(project_id)
    run_manager.drop(project_id)
    project_dir = OUTPUT_DIR / str(project_id)
    if project_dir.exists():
        shutil.rmtree(project_dir, ignore_errors=True)
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
