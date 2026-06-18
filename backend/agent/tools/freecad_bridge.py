# -*- coding: utf-8 -*-
"""
FreeCAD subprocess bridge.

FreeCAD snap uses Python 3.12; the project venv uses Python 3.14,
so direct import is impossible. This module runs FreeCAD operations via
`freecad.cmd` subprocess and communicates through JSON on stdout.
"""
import asyncio
import json
import logging
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

FREECAD_CMD = "freecad.cmd"
TIMEOUT = 60  # seconds per operation


async def run_freecad_script(script: str) -> dict:
    """Write *script* to a temp file and execute it inside freecad.cmd."""
    # freecad snap cannot access /tmp; write to home directory instead
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, prefix="fc_",
        dir=Path.home(),
    ) as f:
        f.write(script)
        script_path = f.name

    try:
        proc = await asyncio.create_subprocess_exec(
            FREECAD_CMD, script_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=TIMEOUT
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return {"success": False, "error": "FreeCAD operation timed out"}

        output = stdout.decode("utf-8", errors="replace")
        # freecad.cmd prepends a PYTHONPATH info line; scan from the bottom
        for line in reversed(output.strip().splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    pass

        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace")
            logger.warning("FreeCAD stderr: %s", err[:400])
            return {
                "success": False,
                "error": f"FreeCAD exited with code {proc.returncode}",
            }

        return {"success": False, "error": "No JSON result from FreeCAD"}
    finally:
        Path(script_path).unlink(missing_ok=True)


async def stl_to_step(stl_path: Path, step_path: Path) -> dict:
    """Convert an STL mesh file to STEP format using FreeCAD's Part module."""
    script = f"""
import FreeCAD, Mesh, Part, json
try:
    doc = FreeCAD.newDocument("export")
    Mesh.insert({str(stl_path)!r}, doc.Name)
    mesh_obj = doc.Objects[0]
    shape = Part.Shape()
    shape.makeShapeFromMesh(mesh_obj.Mesh.Topology, 0.05)
    solid = Part.Solid(shape)
    solid.exportStep({str(step_path)!r})
    print(json.dumps({{"success": True, "path": {str(step_path)!r}}}))
except Exception as exc:
    print(json.dumps({{"success": False, "error": str(exc)}}))
"""
    return await run_freecad_script(script)


async def create_filleted_box(
    length: float,
    width: float,
    height: float,
    fillet_radius: float,
    output_stl: Path,
) -> dict:
    """Create a box with rounded edges in FreeCAD and export as STL."""
    script = f"""
import FreeCAD, Part, json
try:
    doc = FreeCAD.newDocument("fillet_box")
    box = doc.addObject("Part::Box", "Box")
    box.Length = {length}
    box.Width = {width}
    box.Height = {height}
    doc.recompute()
    fillet = doc.addObject("Part::Fillet", "Fillet")
    fillet.Base = box
    fillet.Edges = [(i + 1, {fillet_radius}, {fillet_radius}) for i in range(len(box.Shape.Edges))]
    doc.recompute()
    if fillet.Shape.isNull():
        raise RuntimeError("Fillet shape is null — radius may be too large")
    fillet.Shape.exportStl({str(output_stl)!r})
    print(json.dumps({{"success": True}}))
except Exception as exc:
    print(json.dumps({{"success": False, "error": str(exc)}}))
"""
    return await run_freecad_script(script)


async def create_filleted_cylinder(
    radius: float,
    height: float,
    fillet_radius: float,
    output_stl: Path,
) -> dict:
    """Create a cylinder with filleted top/bottom edges in FreeCAD and export as STL."""
    script = f"""
import FreeCAD, Part, json
try:
    doc = FreeCAD.newDocument("fillet_cyl")
    cyl = doc.addObject("Part::Cylinder", "Cylinder")
    cyl.Radius = {radius}
    cyl.Height = {height}
    doc.recompute()
    fillet = doc.addObject("Part::Fillet", "Fillet")
    fillet.Base = cyl
    # edges 1 and 2 are the top and bottom circular edges
    fillet.Edges = [(1, {fillet_radius}, {fillet_radius}), (2, {fillet_radius}, {fillet_radius})]
    doc.recompute()
    if fillet.Shape.isNull():
        raise RuntimeError("Fillet shape is null — radius may be too large")
    fillet.Shape.exportStl({str(output_stl)!r})
    print(json.dumps({{"success": True}}))
except Exception as exc:
    print(json.dumps({{"success": False, "error": str(exc)}}))
"""
    return await run_freecad_script(script)
