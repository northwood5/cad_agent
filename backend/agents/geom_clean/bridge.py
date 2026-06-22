# -*- coding: utf-8 -*-
"""
Geometry clean bridge — async subprocess wrapper.
Spawns _geom_clean_worker.py under the cax conda Python.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_CAX_PYTHON    = "/home/ubuntu/miniforge3/envs/cax/bin/python"
_WORKER_SCRIPT = str(Path(__file__).parent / "_geom_clean_worker.py")

_clean_sem = asyncio.Semaphore(1)


async def clean_geometry(
    input_path: Path,
    output_path: Path,
    tol: float = 0.01,
) -> dict:
    """Clean *input_path* and write repaired geometry to *output_path*."""
    payload = json.dumps({
        "input_path":  str(input_path),
        "output_path": str(output_path),
        "tol":         tol,
    }).encode()

    async with _clean_sem:
        try:
            proc = await asyncio.create_subprocess_exec(
                _CAX_PYTHON, _WORKER_SCRIPT,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate(input=payload)
        except Exception as exc:
            logger.exception("Failed to spawn geom_clean worker")
            return {"success": False, "error": str(exc)}

    if stderr:
        logger.debug("geom_clean worker stderr: %s", stderr.decode(errors="replace"))

    raw = stdout.strip()
    if not raw:
        err = stderr.decode(errors="replace")[:500] if stderr else "no output"
        return {"success": False, "error": f"geom_clean worker produced no output: {err}"}

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        return {"success": False, "error": f"bad JSON from worker: {exc} | {raw[:200]}"}
