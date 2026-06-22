# -*- coding: utf-8 -*-
"""
FreeCAD bridge — subprocess-based (cax conda Python 3.11).

Each call spawns _freecad_worker.py under the cax Python, which has
FreeCAD 1.1.0 installed.  State is persisted in the project's .FCStd
file between calls, so no long-lived process is needed.

A semaphore ensures at most one FreeCAD operation runs at a time (FreeCAD
is not thread-safe across processes either, since they share .FCStd files).

The public async API and the script-capture sink are unchanged so that
cad_tools.py needs no edits.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_CAX_PYTHON    = "/home/ubuntu/miniforge3/envs/cax/bin/python"
_WORKER_SCRIPT = str(Path(__file__).parent / "_freecad_worker.py")

# One FreeCAD operation at a time.
_fc_sem = asyncio.Semaphore(1)

# ── Script capture sink (unchanged API) ──────────────────────────────────────
_script_sink: contextvars.ContextVar = contextvars.ContextVar(
    "freecad_script_sink", default=None
)


def set_script_sink(fn):
    return _script_sink.set(fn)


def reset_script_sink(token) -> None:
    _script_sink.reset(token)


def _emit(script: str) -> None:
    sink = _script_sink.get()
    if sink is not None:
        try:
            sink(script)
        except Exception:
            logger.debug("script sink raised", exc_info=True)


# ── Internal subprocess helper ────────────────────────────────────────────────

async def _run_worker(payload: dict) -> dict:
    """Spawn the FreeCAD worker, pass payload as JSON, return parsed result."""
    data = json.dumps(payload).encode()
    async with _fc_sem:
        try:
            proc = await asyncio.create_subprocess_exec(
                _CAX_PYTHON, _WORKER_SCRIPT,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate(input=data)
        except Exception as exc:
            logger.exception("Failed to spawn FreeCAD worker")
            return {"success": False, "error": str(exc)}

    if stderr:
        logger.debug("FreeCAD worker stderr: %s", stderr.decode(errors="replace"))

    raw = stdout.strip()
    if not raw:
        err = stderr.decode(errors="replace")[:500] if stderr else "no output"
        return {"success": False, "error": f"FreeCAD worker produced no output: {err}"}

    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {"success": False, "error": f"FreeCAD worker bad JSON: {exc} | {raw[:200]}"}

    # Emit the representative script only when the operation actually succeeded.
    if result.get("success") and result.get("script"):
        _emit(result["script"])

    return result


# ── Public async API (same signatures as before) ──────────────────────────────

async def fc_create_primitive(
    doc_path: Path,
    stl_path: Path,
    name: str,
    shape_type: str,
    *,
    length: float = 10.0,
    width: float = 10.0,
    height: float = 10.0,
    radius: float = 5.0,
    major_radius: float = 10.0,
    minor_radius: float = 3.0,
    fillet_radius: float = 0.0,
) -> dict:
    return await _run_worker({
        "op": "create_primitive",
        "doc_path":  str(doc_path),
        "stl_path":  str(stl_path),
        "name":      name,
        "shape_type": shape_type,
        "length":    length,  "width": width,    "height": height,
        "radius":    radius,
        "major_radius": major_radius, "minor_radius": minor_radius,
        "fillet_radius": fillet_radius,
    })


async def fc_boolean_op(
    doc_path: Path,
    stl_path: Path,
    result_name: str,
    operation: str,
    shape_a: str,
    shape_b: str,
) -> dict:
    return await _run_worker({
        "op": "boolean_op",
        "doc_path":    str(doc_path),
        "stl_path":    str(stl_path),
        "result_name": result_name,
        "operation":   operation,
        "shape_a":     shape_a,
        "shape_b":     shape_b,
    })


async def fc_transform(
    doc_path: Path,
    stl_path: Path,
    name: str,
    translate: list | None = None,
    rotate_axis: list | None = None,
    rotate_angle_deg: float | None = None,
    scale: float | list | None = None,
) -> dict:
    return await _run_worker({
        "op": "transform",
        "doc_path":         str(doc_path),
        "stl_path":         str(stl_path),
        "name":             name,
        "translate":        translate,
        "rotate_axis":      rotate_axis,
        "rotate_angle_deg": rotate_angle_deg,
        "scale":            scale,
    })


async def fc_export_stl(doc_path: Path, output_stl: Path) -> dict:
    return await _run_worker({
        "op":         "export_stl",
        "doc_path":   str(doc_path),
        "output_stl": str(output_stl),
    })


async def fc_export_step(doc_path: Path, output_step: Path) -> dict:
    return await _run_worker({
        "op":          "export_step",
        "doc_path":    str(doc_path),
        "output_step": str(output_step),
    })


async def stl_to_step(stl_path: Path, step_path: Path) -> dict:
    return await _run_worker({
        "op":        "stl_to_step",
        "stl_path":  str(stl_path),
        "step_path": str(step_path),
    })


async def fc_reset(doc_path: Path) -> dict:
    return await _run_worker({
        "op":       "reset",
        "doc_path": str(doc_path),
    })
