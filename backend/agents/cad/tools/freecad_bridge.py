# -*- coding: utf-8 -*-
"""
FreeCAD bridge — in-process (conda-forge build).

The backend runs under a conda env (``cax``) whose Python matches the
conda-forge FreeCAD build, so FreeCAD is imported directly instead of being
shelled out to ``freecad.cmd``. Benefits over the old subprocess approach:

  * no per-operation process startup,
  * a live, in-memory ``FreeCAD.Document`` per project is kept hot and only
    saved to disk after each mutation (the .FCStd still persists state and
    feeds the on-demand STEP export / survives restarts).

Concurrency: FreeCAD is not thread-safe, so every operation runs on a single
dedicated worker thread (``_executor``) guarded by an ``asyncio.Lock`` — all
FreeCAD access is therefore serialised and single-threaded, while the event
loop stays responsive.

The public async API (``fc_create_primitive`` / ``fc_boolean_op`` /
``fc_transform`` / ``fc_export_stl`` / ``fc_export_step`` / ``stl_to_step`` /
``fc_reset``) and the script-capture sink are unchanged, so upper layers
(cad_tools, workflow_service) need no edits.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# ── FreeCAD bootstrap (conda-forge) ───────────────────────────────────────────
_FC_AVAILABLE = False
try:
    # The conda-forge package ships a `freecad` shim that puts FreeCAD.so on
    # sys.path; FreeCAD.so lives in <env>/lib. Point at it explicitly (works
    # whether or not the conda env is "activated") to avoid a startup warning.
    if "PATH_TO_FREECAD_LIBDIR" not in os.environ:
        os.environ["PATH_TO_FREECAD_LIBDIR"] = str(Path(sys.prefix) / "lib")
    import freecad  # noqa: F401  (bootstrap shim)
    import FreeCAD  # type: ignore
    import Part     # type: ignore
    _FC_AVAILABLE = True
    logger.info("FreeCAD %s imported in-process", ".".join(FreeCAD.Version()[:3]))
except Exception as exc:  # pragma: no cover - depends on runtime env
    logger.warning("In-process FreeCAD unavailable (%s); CAD will use trimesh", exc)


# Single dedicated thread so all FreeCAD C++ access happens on one thread.
_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="freecad"
)
_lock = asyncio.Lock()

# Live documents keyed by .FCStd path.
_docs: dict[str, Any] = {}


async def _run(func: Callable, *args) -> dict:
    """Serialise and run a blocking FreeCAD call on the dedicated thread."""
    if not _FC_AVAILABLE:
        return {"success": False, "error": "FreeCAD not available in this environment"}
    async with _lock:
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(_executor, func, *args)
        except Exception as exc:  # pragma: no cover
            logger.exception("FreeCAD operation crashed")
            return {"success": False, "error": str(exc)}


# ── Script capture sink (unchanged API) ───────────────────────────────────────
_script_sink: contextvars.ContextVar = contextvars.ContextVar(
    "freecad_script_sink", default=None
)


def set_script_sink(fn):
    return _script_sink.set(fn)


def reset_script_sink(token) -> None:
    _script_sink.reset(token)


def _emit(script: str) -> None:
    """Report a representative FreeCAD script for the UI script log."""
    sink = _script_sink.get()
    if sink is not None:
        try:
            sink(script)
        except Exception:
            logger.debug("script sink raised", exc_info=True)


# ── Document helpers (run on the FreeCAD thread) ──────────────────────────────

def _get_doc(doc_path: str):
    doc = _docs.get(doc_path)
    if doc is None:
        if os.path.exists(doc_path):
            doc = FreeCAD.openDocument(doc_path)
        else:
            doc = FreeCAD.newDocument("CADScene")
        _docs[doc_path] = doc
    return doc


def _save_doc(doc, doc_path: str) -> None:
    if doc.FileName:
        doc.save()
    else:
        doc.saveAs(doc_path)


def _bounds(shape) -> list[list[float]]:
    bb = shape.BoundBox
    return [[bb.XMin, bb.YMin, bb.ZMin], [bb.XMax, bb.YMax, bb.ZMax]]


# ── Create primitive ──────────────────────────────────────────────────────────

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
    _emit(_script_create(name, shape_type, length, width, height, radius,
                         major_radius, minor_radius, fillet_radius, doc_path, stl_path))

    def work() -> dict:
        doc = _get_doc(str(doc_path))
        if doc.getObject(name):
            doc.removeObject(name)

        if shape_type == "box":
            shape = Part.makeBox(length, width, height)
        elif shape_type == "cylinder":
            shape = Part.makeCylinder(radius, height)
        elif shape_type == "sphere":
            shape = Part.makeSphere(radius)
        elif shape_type == "cone":
            shape = Part.makeCone(0, radius, height)
        elif shape_type == "torus":
            shape = Part.makeTorus(major_radius, minor_radius)
        else:
            return {"success": False, "error": f"Unknown shape_type: {shape_type}"}

        if fillet_radius and fillet_radius > 0 and shape_type in ("box", "cylinder"):
            try:
                if shape_type == "box":
                    shape = shape.makeFillet(fillet_radius, shape.Edges)
                else:
                    circ = [e for e in shape.Edges if hasattr(e.Curve, "Radius")]
                    if circ:
                        shape = shape.makeFillet(fillet_radius, circ[:2])
            except Exception:
                pass  # radius too large — keep unfilleted

        obj = doc.addObject("Part::Feature", name)
        obj.Shape = shape
        doc.recompute()
        _save_doc(doc, str(doc_path))
        obj.Shape.exportStl(str(stl_path))
        return {"success": True, "name": name, "stl_path": str(stl_path),
                "bounds": _bounds(obj.Shape)}

    return await _run(work)


# ── Boolean ───────────────────────────────────────────────────────────────────

async def fc_boolean_op(
    doc_path: Path,
    stl_path: Path,
    result_name: str,
    operation: str,
    shape_a: str,
    shape_b: str,
) -> dict:
    op_map = {"union": "fuse", "difference": "cut", "intersection": "common"}
    if operation not in op_map:
        return {"success": False, "error": f"Unknown operation: {operation!r}"}
    fc_op = op_map[operation]
    _emit(_script_boolean(result_name, fc_op, shape_a, shape_b, doc_path, stl_path))

    def work() -> dict:
        doc = _get_doc(str(doc_path))
        obj_a = doc.getObject(shape_a)
        obj_b = doc.getObject(shape_b)
        if obj_a is None:
            return {"success": False, "error": f"Shape {shape_a!r} not found in document"}
        if obj_b is None:
            return {"success": False, "error": f"Shape {shape_b!r} not found in document"}

        result_shape = getattr(obj_a.Shape, fc_op)(obj_b.Shape)
        if result_shape.isNull():
            return {"success": False, "error": "Boolean result is null — shapes may not intersect"}

        if doc.getObject(result_name):
            doc.removeObject(result_name)
        new_obj = doc.addObject("Part::Feature", result_name)
        new_obj.Shape = result_shape
        doc.recompute()
        _save_doc(doc, str(doc_path))
        new_obj.Shape.exportStl(str(stl_path))
        return {"success": True, "name": result_name, "stl_path": str(stl_path),
                "bounds": _bounds(new_obj.Shape)}

    return await _run(work)


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
    _emit(_script_transform(name, translate, rotate_axis, rotate_angle_deg, scale,
                            doc_path, stl_path))

    def work() -> dict:
        doc = _get_doc(str(doc_path))
        obj = doc.getObject(name)
        if obj is None:
            return {"success": False, "error": f"Shape {name!r} not found in document"}

        shape = obj.Shape.copy()
        if translate:
            shape.translate(FreeCAD.Vector(*translate))
        if rotate_axis and rotate_angle_deg is not None:
            shape.rotate(FreeCAD.Vector(0, 0, 0),
                         FreeCAD.Vector(*rotate_axis), rotate_angle_deg)
        if scale is not None:
            if isinstance(scale, (int, float)):
                shape = shape.scaled(float(scale)) if hasattr(shape, "scaled") else _scale_matrix(shape, [scale, scale, scale])
            else:
                shape = _scale_matrix(shape, scale)

        obj.Shape = shape
        doc.recompute()
        _save_doc(doc, str(doc_path))
        obj.Shape.exportStl(str(stl_path))
        return {"success": True, "name": name, "stl_path": str(stl_path),
                "bounds": _bounds(obj.Shape)}

    return await _run(work)


def _scale_matrix(shape, factors):
    m = FreeCAD.Matrix()
    m.scale(FreeCAD.Vector(*factors))
    return shape.transformGeometry(m)


# ── Export ────────────────────────────────────────────────────────────────────

def _merged_shapes(doc):
    shapes = [o.Shape for o in doc.Objects
              if o.TypeId == "Part::Feature" and not o.Shape.isNull()]
    if not shapes:
        return None
    return shapes[0] if len(shapes) == 1 else Part.makeCompound(shapes)


async def fc_export_stl(doc_path: Path, output_stl: Path) -> dict:
    _emit(f"# 导出 STL\nimport FreeCAD, Part\n"
          f"doc = FreeCAD.openDocument({str(doc_path)!r})\n"
          f"# 合并所有 Part::Feature 后 exportStl({str(output_stl)!r})")

    def work() -> dict:
        doc = _get_doc(str(doc_path))
        merged = _merged_shapes(doc)
        if merged is None:
            return {"success": False, "error": "No shapes in document"}
        merged.exportStl(str(output_stl))
        return {"success": True, "path": str(output_stl)}

    return await _run(work)


async def fc_export_step(doc_path: Path, output_step: Path) -> dict:
    _emit(f"# 导出 STEP (真实 B-rep)\nimport FreeCAD, Part\n"
          f"doc = FreeCAD.openDocument({str(doc_path)!r})\n"
          f"# 合并所有 Part::Feature 后 exportStep({str(output_step)!r})")

    def work() -> dict:
        doc = _get_doc(str(doc_path))
        merged = _merged_shapes(doc)
        if merged is None:
            return {"success": False, "error": "No shapes in document"}
        merged.exportStep(str(output_step))
        return {"success": True, "path": str(output_step)}

    return await _run(work)


async def stl_to_step(stl_path: Path, step_path: Path) -> dict:
    """Convert an STL mesh to STEP (lossy — prefer fc_export_step)."""
    def work() -> dict:
        import Mesh  # type: ignore
        tmp = FreeCAD.newDocument("export_tmp")
        try:
            Mesh.insert(str(stl_path), tmp.Name)
            mesh_obj = tmp.Objects[0]
            shape = Part.Shape()
            shape.makeShapeFromMesh(mesh_obj.Mesh.Topology, 0.05)
            solid = Part.Solid(shape)
            solid.exportStep(str(step_path))
            return {"success": True, "path": str(step_path)}
        finally:
            FreeCAD.closeDocument(tmp.Name)

    return await _run(work)


# ── Reset (drop a project's live doc) ─────────────────────────────────────────

async def fc_reset(doc_path: Path) -> dict:
    """Close + forget the cached document so the project starts fresh."""
    def work() -> dict:
        key = str(doc_path)
        doc = _docs.pop(key, None)
        if doc is not None:
            try:
                FreeCAD.closeDocument(doc.Name)
            except Exception:
                pass
        return {"success": True}

    return await _run(work)


# ── Representative scripts for the UI log ─────────────────────────────────────

def _script_create(name, shape_type, length, width, height, radius,
                   major_radius, minor_radius, fillet_radius, doc_path, stl_path) -> str:
    maker = {
        "box": f"Part.makeBox({length}, {width}, {height})",
        "cylinder": f"Part.makeCylinder({radius}, {height})",
        "sphere": f"Part.makeSphere({radius})",
        "cone": f"Part.makeCone(0, {radius}, {height})",
        "torus": f"Part.makeTorus({major_radius}, {minor_radius})",
    }.get(shape_type, f"# unknown {shape_type}")
    lines = [
        "import FreeCAD, Part",
        f"doc = FreeCAD.openDocument({str(doc_path)!r}) if os.path.exists({str(doc_path)!r}) else FreeCAD.newDocument('CADScene')",
        f"shape = {maker}",
    ]
    if fillet_radius and fillet_radius > 0 and shape_type in ("box", "cylinder"):
        lines.append(f"shape = shape.makeFillet({fillet_radius}, shape.Edges)")
    lines += [
        f"obj = doc.addObject('Part::Feature', {name!r}); obj.Shape = shape",
        "doc.recompute(); doc.save()",
        f"obj.Shape.exportStl({str(stl_path)!r})",
    ]
    return "\n".join(lines)


def _script_boolean(result_name, fc_op, shape_a, shape_b, doc_path, stl_path) -> str:
    return "\n".join([
        "import FreeCAD, Part",
        f"doc = FreeCAD.openDocument({str(doc_path)!r})",
        f"a = doc.getObject({shape_a!r}); b = doc.getObject({shape_b!r})",
        f"res = a.Shape.{fc_op}(b.Shape)",
        f"obj = doc.addObject('Part::Feature', {result_name!r}); obj.Shape = res",
        "doc.recompute(); doc.save()",
        f"obj.Shape.exportStl({str(stl_path)!r})",
    ])


def _script_transform(name, translate, rotate_axis, rotate_angle_deg, scale,
                      doc_path, stl_path) -> str:
    lines = [
        "import FreeCAD, Part",
        f"doc = FreeCAD.openDocument({str(doc_path)!r})",
        f"obj = doc.getObject({name!r}); shape = obj.Shape.copy()",
    ]
    if translate:
        lines.append(f"shape.translate(FreeCAD.Vector{tuple(translate)})")
    if rotate_axis and rotate_angle_deg is not None:
        lines.append(f"shape.rotate(FreeCAD.Vector(0,0,0), FreeCAD.Vector{tuple(rotate_axis)}, {rotate_angle_deg})")
    if scale is not None:
        if isinstance(scale, (int, float)):
            lines.append(f"shape = shape.scaled({float(scale)})")
        else:
            lines.append(f"m = FreeCAD.Matrix(); m.scale(FreeCAD.Vector{tuple(scale)}); shape = shape.transformGeometry(m)")
    lines += [
        "obj.Shape = shape; doc.recompute(); doc.save()",
        f"obj.Shape.exportStl({str(stl_path)!r})",
    ]
    return "\n".join(lines)
