#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Standalone FreeCAD worker — invoked as a subprocess by freecad_bridge.py.

Protocol:
  stdin  : one JSON line  { "op": "<name>", ...args }
  stdout : one JSON line  { "success": true/false, "script": "...", ...result }

Runs under the cax conda Python 3.11 so FreeCAD 1.1.0 is importable.
State is persisted in the project's .FCStd file between calls.
"""
from __future__ import annotations

import json
import sys
import os

# FreeCAD lives in the cax lib dir; the shim package puts it on path.
sys.path.insert(0, "/home/ubuntu/miniforge3/envs/cax/lib")

try:
    import freecad  # noqa: F401  (bootstrap shim)
    import FreeCAD  # type: ignore
    import Part     # type: ignore
except ImportError as exc:
    print(json.dumps({"success": False, "error": f"FreeCAD import failed: {exc}"}))
    sys.exit(1)


# ── document helpers ──────────────────────────────────────────────────────────

def _get_doc(doc_path: str):
    if os.path.exists(doc_path):
        return FreeCAD.openDocument(doc_path)
    return FreeCAD.newDocument("CADScene")


def _save_doc(doc, doc_path: str) -> None:
    if doc.FileName:
        doc.save()
    else:
        doc.saveAs(doc_path)


def _bounds(shape) -> list:
    bb = shape.BoundBox
    return [[bb.XMin, bb.YMin, bb.ZMin], [bb.XMax, bb.YMax, bb.ZMax]]


def _merged_shapes(doc):
    shapes = [o.Shape for o in doc.Objects
              if o.TypeId == "Part::Feature" and not o.Shape.isNull()]
    if not shapes:
        return None
    return shapes[0] if len(shapes) == 1 else Part.makeCompound(shapes)


# ── operation handlers ────────────────────────────────────────────────────────

def op_create_primitive(args: dict) -> dict:
    doc_path  = args["doc_path"]
    stl_path  = args["stl_path"]
    name      = args["name"]
    shape_type = args["shape_type"]
    length    = float(args.get("length", 10))
    width     = float(args.get("width", 10))
    height    = float(args.get("height", 10))
    radius    = float(args.get("radius", 5))
    maj_r     = float(args.get("major_radius", 10))
    min_r     = float(args.get("minor_radius", 3))
    fillet_r  = float(args.get("fillet_radius", 0))

    doc = _get_doc(doc_path)
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
        shape = Part.makeTorus(maj_r, min_r)
    else:
        return {"success": False, "error": f"Unknown shape_type: {shape_type}"}

    if fillet_r > 0 and shape_type in ("box", "cylinder"):
        try:
            if shape_type == "box":
                shape = shape.makeFillet(fillet_r, shape.Edges)
            else:
                circ = [e for e in shape.Edges if hasattr(e.Curve, "Radius")]
                if circ:
                    shape = shape.makeFillet(fillet_r, circ[:2])
        except Exception:
            pass

    obj = doc.addObject("Part::Feature", name)
    obj.Shape = shape
    doc.recompute()
    _save_doc(doc, doc_path)
    obj.Shape.exportStl(stl_path)

    maker = {
        "box":      f"Part.makeBox({length}, {width}, {height})",
        "cylinder": f"Part.makeCylinder({radius}, {height})",
        "sphere":   f"Part.makeSphere({radius})",
        "cone":     f"Part.makeCone(0, {radius}, {height})",
        "torus":    f"Part.makeTorus({maj_r}, {min_r})",
    }.get(shape_type, "")
    lines = [
        "import FreeCAD, Part",
        f"doc = FreeCAD.openDocument({doc_path!r})",
        f"shape = {maker}",
    ]
    if fillet_r > 0 and shape_type in ("box", "cylinder"):
        lines.append(f"shape = shape.makeFillet({fillet_r}, shape.Edges)")
    lines += [
        f"obj = doc.addObject('Part::Feature', {name!r}); obj.Shape = shape",
        "doc.recompute(); doc.save()",
        f"obj.Shape.exportStl({stl_path!r})",
    ]

    return {
        "success": True, "name": name,
        "bounds": _bounds(obj.Shape),
        "script": "\n".join(lines),
    }


def op_boolean_op(args: dict) -> dict:
    doc_path    = args["doc_path"]
    stl_path    = args["stl_path"]
    result_name = args["result_name"]
    operation   = args["operation"]
    shape_a     = args["shape_a"]
    shape_b     = args["shape_b"]

    op_map = {"union": "fuse", "difference": "cut", "intersection": "common"}
    if operation not in op_map:
        return {"success": False, "error": f"Unknown operation: {operation!r}"}
    fc_op = op_map[operation]

    doc = _get_doc(doc_path)
    obj_a = doc.getObject(shape_a)
    obj_b = doc.getObject(shape_b)
    if obj_a is None:
        return {"success": False, "error": f"Shape {shape_a!r} not found"}
    if obj_b is None:
        return {"success": False, "error": f"Shape {shape_b!r} not found"}

    result_shape = getattr(obj_a.Shape, fc_op)(obj_b.Shape)
    if result_shape.isNull():
        return {"success": False, "error": "Boolean result is null — shapes may not intersect"}

    if doc.getObject(result_name):
        doc.removeObject(result_name)
    new_obj = doc.addObject("Part::Feature", result_name)
    new_obj.Shape = result_shape
    doc.recompute()
    _save_doc(doc, doc_path)
    new_obj.Shape.exportStl(stl_path)

    script = "\n".join([
        "import FreeCAD, Part",
        f"doc = FreeCAD.openDocument({doc_path!r})",
        f"a = doc.getObject({shape_a!r}); b = doc.getObject({shape_b!r})",
        f"res = a.Shape.{fc_op}(b.Shape)",
        f"obj = doc.addObject('Part::Feature', {result_name!r}); obj.Shape = res",
        "doc.recompute(); doc.save()",
        f"obj.Shape.exportStl({stl_path!r})",
    ])
    return {
        "success": True, "name": result_name,
        "bounds": _bounds(new_obj.Shape),
        "script": script,
    }


def op_transform(args: dict) -> dict:
    doc_path         = args["doc_path"]
    stl_path         = args["stl_path"]
    name             = args["name"]
    translate        = args.get("translate")
    rotate_axis      = args.get("rotate_axis")
    rotate_angle_deg = args.get("rotate_angle_deg")
    scale            = args.get("scale")

    doc = _get_doc(doc_path)
    obj = doc.getObject(name)
    if obj is None:
        return {"success": False, "error": f"Shape {name!r} not found"}

    shape = obj.Shape.copy()
    script_lines = [
        "import FreeCAD, Part",
        f"doc = FreeCAD.openDocument({doc_path!r})",
        f"obj = doc.getObject({name!r}); shape = obj.Shape.copy()",
    ]

    if translate:
        shape.translate(FreeCAD.Vector(*translate))
        script_lines.append(f"shape.translate(FreeCAD.Vector{tuple(translate)})")

    if rotate_axis and rotate_angle_deg is not None:
        shape.rotate(FreeCAD.Vector(0, 0, 0),
                     FreeCAD.Vector(*rotate_axis), rotate_angle_deg)
        script_lines.append(
            f"shape.rotate(FreeCAD.Vector(0,0,0), FreeCAD.Vector{tuple(rotate_axis)}, {rotate_angle_deg})"
        )

    if scale is not None:
        if isinstance(scale, (int, float)):
            if hasattr(shape, "scaled"):
                shape = shape.scaled(float(scale))
                script_lines.append(f"shape = shape.scaled({float(scale)})")
            else:
                m = FreeCAD.Matrix()
                m.scale(FreeCAD.Vector(scale, scale, scale))
                shape = shape.transformGeometry(m)
                script_lines.append(f"m = FreeCAD.Matrix(); m.scale(FreeCAD.Vector({scale},{scale},{scale})); shape = shape.transformGeometry(m)")
        else:
            m = FreeCAD.Matrix()
            m.scale(FreeCAD.Vector(*scale))
            shape = shape.transformGeometry(m)
            script_lines.append(f"m = FreeCAD.Matrix(); m.scale(FreeCAD.Vector{tuple(scale)}); shape = shape.transformGeometry(m)")

    obj.Shape = shape
    doc.recompute()
    _save_doc(doc, doc_path)
    obj.Shape.exportStl(stl_path)
    script_lines += [
        "obj.Shape = shape; doc.recompute(); doc.save()",
        f"obj.Shape.exportStl({stl_path!r})",
    ]
    return {
        "success": True, "name": name,
        "bounds": _bounds(obj.Shape),
        "script": "\n".join(script_lines),
    }


def op_export_stl(args: dict) -> dict:
    doc_path   = args["doc_path"]
    output_stl = args["output_stl"]

    doc = _get_doc(doc_path)
    merged = _merged_shapes(doc)
    if merged is None:
        return {"success": False, "error": "No shapes in document"}
    merged.exportStl(output_stl)
    script = (
        f"import FreeCAD, Part\n"
        f"doc = FreeCAD.openDocument({doc_path!r})\n"
        f"# merge all Part::Feature shapes → exportStl({output_stl!r})"
    )
    return {"success": True, "path": output_stl, "script": script}


def op_export_step(args: dict) -> dict:
    doc_path    = args["doc_path"]
    output_step = args["output_step"]

    doc = _get_doc(doc_path)
    merged = _merged_shapes(doc)
    if merged is None:
        return {"success": False, "error": "No shapes in document"}
    merged.exportStep(output_step)
    script = (
        f"import FreeCAD, Part\n"
        f"doc = FreeCAD.openDocument({doc_path!r})\n"
        f"# merge all Part::Feature shapes → exportStep({output_step!r})"
    )
    return {"success": True, "path": output_step, "script": script}


def op_stl_to_step(args: dict) -> dict:
    stl_path  = args["stl_path"]
    step_path = args["step_path"]

    import Mesh  # type: ignore
    tmp = FreeCAD.newDocument("export_tmp")
    try:
        Mesh.insert(stl_path, tmp.Name)
        mesh_obj = tmp.Objects[0]
        shape = Part.Shape()
        shape.makeShapeFromMesh(mesh_obj.Mesh.Topology, 0.05)
        solid = Part.Solid(shape)
        solid.exportStep(step_path)
        return {"success": True, "path": step_path, "script": f"# STL→STEP conversion via FreeCAD Mesh module\n# {stl_path} → {step_path}"}
    finally:
        FreeCAD.closeDocument(tmp.Name)


def op_reset(args: dict) -> dict:
    doc_path = args["doc_path"]
    # Close open doc (if this worker had opened it), then remove the file.
    try:
        # Find and close any open document with this path.
        for doc in FreeCAD.listDocuments().values():
            if getattr(doc, "FileName", "") == doc_path:
                FreeCAD.closeDocument(doc.Name)
    except Exception:
        pass
    if os.path.exists(doc_path):
        os.remove(doc_path)
    return {"success": True, "script": f"# Reset: removed {doc_path}"}


# ── dispatch ──────────────────────────────────────────────────────────────────

_OPS = {
    "create_primitive": op_create_primitive,
    "boolean_op":       op_boolean_op,
    "transform":        op_transform,
    "export_stl":       op_export_stl,
    "export_step":      op_export_step,
    "stl_to_step":      op_stl_to_step,
    "reset":            op_reset,
}


def main():
    raw = sys.stdin.readline()
    try:
        args = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(json.dumps({"success": False, "error": f"bad input JSON: {exc}"}))
        return

    op = args.pop("op", None)
    handler = _OPS.get(op)
    if handler is None:
        print(json.dumps({"success": False, "error": f"unknown op: {op!r}"}))
        return

    try:
        result = handler(args)
    except Exception as exc:
        import traceback
        print(json.dumps({"success": False, "error": str(exc),
                          "traceback": traceback.format_exc()}))
        return

    print(json.dumps(result))


if __name__ == "__main__":
    main()
