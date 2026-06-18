# -*- coding: utf-8 -*-
"""
FreeCAD subprocess bridge.

FreeCAD snap uses Python 3.12; the project venv uses Python 3.14,
so direct import is impossible. This module runs FreeCAD operations via
`freecad.cmd` subprocess and communicates through JSON on stdout.

Document mode (fc_*): all operations share a persistent .FCStd document
per session. FreeCAD B-rep geometry is the source of truth; STL is
exported for the 3-D viewer after every mutating call.
"""
import asyncio
import json
import logging
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

FREECAD_CMD = "freecad.cmd"
TIMEOUT = 60  # seconds per operation


# ── Low-level runner ─────────────────────────────────────────────────────────

async def run_freecad_script(script: str) -> dict:
    """Write *script* to a temp file and execute it inside freecad.cmd.

    The freecad snap cannot access /tmp, so the script is placed in $HOME.
    The last JSON line in stdout is parsed and returned as a dict.
    """
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
            return {"success": False, "error": f"FreeCAD exited with code {proc.returncode}: {err[:200]}"}

        return {"success": False, "error": "No JSON result from FreeCAD"}
    finally:
        Path(script_path).unlink(missing_ok=True)


# ── Document-mode primitives ──────────────────────────────────────────────────

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
    """Create a primitive, add it to the session document, export STL."""
    dp = str(doc_path)
    sp = str(stl_path)
    fr = fillet_radius

    script = f"""
import FreeCAD, Part, json, os

doc_path = {dp!r}
stl_path = {sp!r}
name = {name!r}

try:
    if os.path.exists(doc_path):
        doc = FreeCAD.openDocument(doc_path)
    else:
        doc = FreeCAD.newDocument("CADScene")

    existing = doc.getObject(name)
    if existing:
        doc.removeObject(name)

    shape_type = {shape_type!r}
    if shape_type == "box":
        shape = Part.makeBox({length}, {width}, {height})
    elif shape_type == "cylinder":
        shape = Part.makeCylinder({radius}, {height})
    elif shape_type == "sphere":
        shape = Part.makeSphere({radius})
    elif shape_type == "cone":
        shape = Part.makeCone(0, {radius}, {height})
    elif shape_type == "torus":
        shape = Part.makeTorus({major_radius}, {minor_radius})
    else:
        raise ValueError("Unknown shape_type: " + shape_type)

    if {fr} > 0 and shape_type in ("box", "cylinder"):
        try:
            if shape_type == "box":
                shape = shape.makeFillet({fr}, shape.Edges)
            else:
                circ = [e for e in shape.Edges if hasattr(e.Curve, "Radius")]
                if circ:
                    shape = shape.makeFillet({fr}, circ[:2])
        except Exception:
            pass  # proceed without fillet if radius too large

    obj = doc.addObject("Part::Feature", name)
    obj.Shape = shape
    doc.recompute()

    if os.path.exists(doc_path):
        doc.save()
    else:
        doc.saveAs(doc_path)

    obj.Shape.exportStl(stl_path)
    bb = obj.Shape.BoundBox
    print(json.dumps({{
        "success": True,
        "name": name,
        "stl_path": stl_path,
        "bounds": [[bb.XMin, bb.YMin, bb.ZMin], [bb.XMax, bb.YMax, bb.ZMax]],
    }}))
except Exception as exc:
    print(json.dumps({{"success": False, "error": str(exc)}}))
"""
    return await run_freecad_script(script)


# ── Boolean operations ────────────────────────────────────────────────────────

async def fc_boolean_op(
    doc_path: Path,
    stl_path: Path,
    result_name: str,
    operation: str,
    shape_a: str,
    shape_b: str,
) -> dict:
    """Perform a boolean operation between two named shapes in the document."""
    op_map = {"union": "fuse", "difference": "cut", "intersection": "common"}
    if operation not in op_map:
        return {"success": False, "error": f"Unknown operation: {operation!r}"}
    fc_op = op_map[operation]

    script = f"""
import FreeCAD, Part, json, os

doc_path = {str(doc_path)!r}
stl_path = {str(stl_path)!r}
result_name = {result_name!r}

try:
    doc = FreeCAD.openDocument(doc_path)

    obj_a = doc.getObject({shape_a!r})
    obj_b = doc.getObject({shape_b!r})
    if obj_a is None:
        raise RuntimeError("Shape {shape_a!r} not found in document")
    if obj_b is None:
        raise RuntimeError("Shape {shape_b!r} not found in document")

    result_shape = obj_a.Shape.{fc_op}(obj_b.Shape)
    if result_shape.isNull():
        raise RuntimeError("Boolean result is null — shapes may not intersect")

    existing = doc.getObject(result_name)
    if existing:
        doc.removeObject(result_name)

    new_obj = doc.addObject("Part::Feature", result_name)
    new_obj.Shape = result_shape
    doc.recompute()
    doc.save()

    new_obj.Shape.exportStl(stl_path)
    bb = new_obj.Shape.BoundBox
    print(json.dumps({{
        "success": True,
        "name": result_name,
        "stl_path": stl_path,
        "bounds": [[bb.XMin, bb.YMin, bb.ZMin], [bb.XMax, bb.YMax, bb.ZMax]],
    }}))
except Exception as exc:
    print(json.dumps({{"success": False, "error": str(exc)}}))
"""
    return await run_freecad_script(script)


# ── Transform ─────────────────────────────────────────────────────────────────

async def fc_transform(
    doc_path: Path,
    stl_path: Path,
    name: str,
    translate: list | None = None,
    rotate_axis: list | None = None,
    rotate_angle_deg: float | None = None,
    scale: float | list | None = None,
) -> dict:
    """Apply translate / rotate / scale to a named shape in the document."""
    translate_code = f"shape.translate(FreeCAD.Vector{tuple(translate)})" if translate else ""
    rotate_code = ""
    if rotate_axis and rotate_angle_deg is not None:
        rotate_code = (
            f"shape.rotate(FreeCAD.Vector(0,0,0), "
            f"FreeCAD.Vector{tuple(rotate_axis)}, {rotate_angle_deg})"
        )
    scale_code = ""
    if scale is not None:
        if isinstance(scale, (int, float)):
            scale_code = f"shape = shape.scale({float(scale)})"
        else:
            scale_code = (
                f"m = FreeCAD.Matrix(); "
                f"m.scale(FreeCAD.Vector{tuple(scale)}); "
                f"shape = shape.transformGeometry(m)"
            )

    script = f"""
import FreeCAD, Part, json, os

doc_path = {str(doc_path)!r}
stl_path = {str(stl_path)!r}
name = {name!r}

try:
    doc = FreeCAD.openDocument(doc_path)
    obj = doc.getObject(name)
    if obj is None:
        raise RuntimeError("Shape " + name + " not found in document")

    shape = obj.Shape.copy()
    {translate_code}
    {rotate_code}
    {scale_code}

    obj.Shape = shape
    doc.recompute()
    doc.save()

    obj.Shape.exportStl(stl_path)
    bb = obj.Shape.BoundBox
    print(json.dumps({{
        "success": True,
        "name": name,
        "stl_path": stl_path,
        "bounds": [[bb.XMin, bb.YMin, bb.ZMin], [bb.XMax, bb.YMax, bb.ZMax]],
    }}))
except Exception as exc:
    print(json.dumps({{"success": False, "error": str(exc)}}))
"""
    return await run_freecad_script(script)


# ── Export ────────────────────────────────────────────────────────────────────

async def fc_export_stl(doc_path: Path, output_stl: Path) -> dict:
    """Merge all shapes in the document and export as STL."""
    script = f"""
import FreeCAD, Part, json, os

doc_path = {str(doc_path)!r}
output_stl = {str(output_stl)!r}

try:
    doc = FreeCAD.openDocument(doc_path)
    shapes = [o.Shape for o in doc.Objects
              if o.TypeId == "Part::Feature" and not o.Shape.isNull()]
    if not shapes:
        raise RuntimeError("No shapes in document")
    merged = shapes[0] if len(shapes) == 1 else Part.makeCompound(shapes)
    merged.exportStl(output_stl)
    print(json.dumps({{"success": True, "path": output_stl}}))
except Exception as exc:
    print(json.dumps({{"success": False, "error": str(exc)}}))
"""
    return await run_freecad_script(script)


async def fc_export_step(doc_path: Path, output_step: Path) -> dict:
    """Export all shapes in the document as STEP (true B-rep, not mesh)."""
    script = f"""
import FreeCAD, Part, json, os

doc_path = {str(doc_path)!r}
output_step = {str(output_step)!r}

try:
    doc = FreeCAD.openDocument(doc_path)
    shapes = [o.Shape for o in doc.Objects
              if o.TypeId == "Part::Feature" and not o.Shape.isNull()]
    if not shapes:
        raise RuntimeError("No shapes in document")
    merged = shapes[0] if len(shapes) == 1 else Part.makeCompound(shapes)
    merged.exportStep(output_step)
    print(json.dumps({{"success": True, "path": output_step}}))
except Exception as exc:
    print(json.dumps({{"success": False, "error": str(exc)}}))
"""
    return await run_freecad_script(script)


# ── Legacy helpers ────────────────────────────────────────────────────────────

async def stl_to_step(stl_path: Path, step_path: Path) -> dict:
    """Convert an STL mesh to STEP (lossy — prefer fc_export_step when possible)."""
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
    length: float, width: float, height: float, fillet_radius: float, output_stl: Path
) -> dict:
    script = f"""
import FreeCAD, Part, json

try:
    shape = Part.makeBox({length}, {width}, {height})
    shape = shape.makeFillet({fillet_radius}, shape.Edges)
    shape.exportStl({str(output_stl)!r})
    print(json.dumps({{"success": True}}))
except Exception as exc:
    print(json.dumps({{"success": False, "error": str(exc)}}))
"""
    return await run_freecad_script(script)


async def create_filleted_cylinder(
    radius: float, height: float, fillet_radius: float, output_stl: Path
) -> dict:
    script = f"""
import FreeCAD, Part, json

try:
    shape = Part.makeCylinder({radius}, {height})
    circ = [e for e in shape.Edges if hasattr(e.Curve, "Radius")]
    shape = shape.makeFillet({fillet_radius}, circ[:2])
    shape.exportStl({str(output_stl)!r})
    print(json.dumps({{"success": True}}))
except Exception as exc:
    print(json.dumps({{"success": False, "error": str(exc)}}))
"""
    return await run_freecad_script(script)
