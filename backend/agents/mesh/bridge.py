# -*- coding: utf-8 -*-
"""
Gmsh bridge — async wrapper for mesh generation.

Spawns _mesh_worker.py under the cax conda Python interpreter so that gmsh
and all its native dependencies (libGLU, libGL, …) are resolved correctly,
independent of which venv the FastAPI backend runs under.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# The cax conda env has gmsh + libGLU/libGL installed correctly.
_CAX_PYTHON = "/home/ubuntu/miniforge3/envs/cax/bin/python"
_WORKER_SCRIPT = str(Path(__file__).parent / "_mesh_worker.py")

# Semaphore: run at most one gmsh process at a time (gmsh is not thread-safe).
_mesh_sem = asyncio.Semaphore(1)


async def mesh_geometry(
    geo_path: Path, msh_path: Path, inp_path: Path, lc: float = 5.0
) -> dict:
    """Async entry — spawns the gmsh worker in the cax Python subprocess."""
    payload = json.dumps({
        "geo": str(geo_path),
        "msh": str(msh_path),
        "inp": str(inp_path),
        "lc":  lc,
    }).encode()

    async with _mesh_sem:
        try:
            proc = await asyncio.create_subprocess_exec(
                _CAX_PYTHON, _WORKER_SCRIPT,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate(input=payload)
        except Exception as exc:
            logger.exception("Failed to spawn gmsh worker")
            return {"success": False, "error": str(exc)}

    if stderr:
        logger.debug("gmsh worker stderr: %s", stderr.decode(errors="replace"))

    raw = stdout.strip()
    if not raw:
        err_msg = stderr.decode(errors="replace")[:500] if stderr else "no output"
        return {"success": False, "error": f"gmsh worker produced no output: {err_msg}"}

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        return {"success": False, "error": f"gmsh worker bad JSON: {exc}  |  {raw[:200]}"}
