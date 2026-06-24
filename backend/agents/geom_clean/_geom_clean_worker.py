#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Geometry cleaning worker — invoked as a subprocess by bridge.py.

Protocol  (stdin → stdout, one JSON line each):
  in  : {"input_path": "...", "output_path": "...", "tol": 0.01}
  out : {"success": bool, "report": {...}, "script": "..."}

Runs under the cax conda Python 3.11 (FreeCAD 1.1.0 + trimesh available).

Cleaning pipeline
─────────────────
STEP / STP input
  1. Load with FreeCAD (OCC kernel)
  2. Diagnose: valid, closed, free edges, tiny faces, shell count …
  3. ShapeFix pass: fix tolerances, degenerate wires/faces, small edges
  4. Sewing pass: close gaps ≤ sew_tol (= 10× tol)
  5. makeSolid pass: promote open shells to closed solid when possible
  6. Export repaired STEP

STL input
  1. trimesh load + watertight repair (fill holes, fix normals, remove degen)
  2. FreeCAD Mesh → Part conversion to get a B-rep solid
  3. ShapeFix + Sewing on the B-rep
  4. Export STEP (preferred for downstream gmsh) or STL if conversion fails
"""
from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

sys.path.insert(0, "/home/ubuntu/miniforge3/envs/cax/lib")

try:
    import freecad  # noqa: F401
    import FreeCAD  # type: ignore
    import Part     # type: ignore
    _FC_OK = True
except ImportError as exc:
    print(json.dumps({"success": False, "error": f"FreeCAD import failed: {exc}"}))
    sys.exit(1)


# ── diagnostic helpers ────────────────────────────────────────────────────────

def _diagnose(shape) -> dict:
    """Return a dict of common geometry quality metrics."""
    solids    = len(shape.Solids)
    shells    = len(shape.Shells)
    faces     = len(shape.Faces)
    edges     = len(shape.Edges)
    vertices  = len(shape.Vertexes)
    is_valid  = shape.isValid()
    is_closed = shape.isClosed()

    # Free edges = edges shared by < 2 faces  (open boundary indicator)
    free_edges = 0
    try:
        free_edges = sum(
            1 for e in shape.Edges
            if len(shape.ancestorsOfType(e, Part.Face)) < 2
        )
    except Exception:
        pass

    # Tiny face check: face area < 1% of mean face area
    tiny_faces = 0
    if faces > 0:
        areas = []
        try:
            areas = [f.Area for f in shape.Faces]
        except Exception:
            pass
        if areas:
            mean_area = sum(areas) / len(areas)
            threshold = mean_area * 0.01
            tiny_faces = sum(1 for a in areas if a < threshold)

    # Bounding box
    try:
        bb = shape.BoundBox
        bbox = [bb.XMin, bb.YMin, bb.ZMin, bb.XMax, bb.YMax, bb.ZMax]
        bbox_size = max(bb.XLength, bb.YLength, bb.ZLength)
    except Exception:
        bbox = []
        bbox_size = 0.0

    return {
        "is_valid":   is_valid,
        "is_closed":  is_closed,
        "solids":     solids,
        "shells":     shells,
        "faces":      faces,
        "edges":      edges,
        "vertices":   vertices,
        "free_edges": free_edges,
        "tiny_faces": tiny_faces,
        "bbox":       bbox,
        "bbox_size":  bbox_size,
    }


def _issues(diag: dict) -> list[str]:
    issues = []
    if not diag["is_valid"]:
        issues.append("几何体无效（OCC 校验失败）")
    if not diag["is_closed"]:
        issues.append("几何体非封闭（开放壳体）")
    if diag["free_edges"] > 0:
        issues.append(f"存在 {diag['free_edges']} 条自由边（缺口/裂缝）")
    if diag["solids"] == 0:
        issues.append("无实体（仅有面/壳，无体积）")
    if diag["tiny_faces"] > 0:
        issues.append(f"存在 {diag['tiny_faces']} 个极小面（可能导致过细网格）")
    return issues


# ── STEP / STP cleaning ───────────────────────────────────────────────────────

def _load_step(path: str):
    """Load a STEP file into a FreeCAD document and return (doc, shape)."""
    doc = FreeCAD.newDocument("geom_clean_tmp")
    Part.insert(path, doc.Name)
    doc.recompute()
    shapes = [o.Shape for o in doc.Objects
              if hasattr(o, "Shape") and not o.Shape.isNull()]
    if not shapes:
        raise ValueError("STEP ファイルに形状が見つかりません")
    compound = shapes[0] if len(shapes) == 1 else Part.makeCompound(shapes)
    return doc, compound


def _shape_fix(shape, tol: float):
    """Run the full OCC ShapeFix chain and return the fixed shape."""
    fixed = shape

    # 1. General ShapeFix_Shape (fixes tolerances, degenerate edges, etc.)
    try:
        fixer = Part.ShapeFix.Shape(fixed)
        fixer.Perform()
        candidate = fixer.Shape()
        if not candidate.isNull():
            fixed = candidate
    except Exception:
        pass

    # 2. Fix tiny / sliver faces
    try:
        sf_face = Part.ShapeFix.FixSmallFace(fixed)
        sf_face.Perform()
        candidate = sf_face.FixShape()
        if not candidate.isNull():
            fixed = candidate
    except Exception:
        pass

    return fixed


def _sew(shape, sew_tol: float):
    """Sew surfaces together within sew_tol to close small gaps."""
    try:
        builder = Part.BRep_Builder()
        sewed_shell = Part.BRepBuilderAPI.MakeSewing()
        # Add all faces
        for face in shape.Faces:
            sewed_shell.Add(face)
        sewed_shell.Perform(sew_tol)
        result = sewed_shell.SewedShape()
        if not result.isNull():
            return result
    except Exception:
        pass

    # Fallback: try Part's built-in sewing
    try:
        compound_faces = Part.makeCompound(shape.Faces)
        sewn = compound_faces.makeShapeFromMesh(compound_faces.tessellate(0.1), 0.1)
    except Exception:
        pass

    return shape


def _try_make_solid(shape):
    """Promote shells → solid if possible."""
    # Already a solid
    if shape.Solids:
        return shape

    shells = shape.Shells
    if not shells:
        return shape

    # Try closing each shell and making a solid
    solids = []
    for shell in shells:
        try:
            if not shell.isClosed():
                fixer = Part.ShapeFix.Shell(shell)
                fixer.Perform()
                shell = fixer.Shell()
            if shell.isClosed():
                solid = Part.makeSolid(shell)
                if solid and not solid.isNull():
                    solids.append(solid)
        except Exception:
            pass

    if solids:
        return solids[0] if len(solids) == 1 else Part.makeCompound(solids)

    return shape


def clean_step(input_path: str, output_path: str, tol: float) -> dict:
    doc = None
    try:
        doc, shape = _load_step(input_path)
        before = _diagnose(shape)
        issues_before = _issues(before)

        sew_tol = tol * 10

        # Fix → Sew → Solid
        shape = _shape_fix(shape, tol)
        if not shape.isClosed():
            shape = _sew(shape, sew_tol)
        shape = _try_make_solid(shape)

        after = _diagnose(shape)
        issues_after = _issues(after)

        shape.exportStep(output_path)

        fixes_applied = []
        if not before["is_valid"] and after["is_valid"]:
            fixes_applied.append("修复了几何无效问题")
        if not before["is_closed"] and after["is_closed"]:
            fixes_applied.append(f"缝合封闭了壳体（公差 {sew_tol:.3g}）")
        if before["free_edges"] > after["free_edges"]:
            fixes_applied.append(
                f"自由边从 {before['free_edges']} 减少至 {after['free_edges']}"
            )
        if before["solids"] == 0 and after["solids"] > 0:
            fixes_applied.append("成功生成实体")
        if before["tiny_faces"] > after["tiny_faces"]:
            fixes_applied.append(
                f"移除了 {before['tiny_faces'] - after['tiny_faces']} 个极小面"
            )

        script = _gen_script_step(input_path, output_path, tol, sew_tol)
        return {
            "success":       True,
            "before":        before,
            "after":         after,
            "issues_before": issues_before,
            "issues_after":  issues_after,
            "fixes_applied": fixes_applied,
            "output_path":   output_path,
            "script":        script,
        }

    except Exception as exc:
        import traceback
        return {"success": False, "error": str(exc),
                "traceback": traceback.format_exc()}
    finally:
        if doc is not None:
            try:
                FreeCAD.closeDocument(doc.Name)
            except Exception:
                pass


# ── STL cleaning ─────────────────────────────────────────────────────────────

def clean_stl(input_path: str, output_path: str, tol: float) -> dict:
    """
    Repair an STL mesh, then convert to STEP B-rep for gmsh.
    Falls back to exporting a repaired STL if B-rep conversion fails.
    """
    import trimesh

    # 1. Load + trimesh repair
    mesh = trimesh.load(input_path, force="mesh")
    before_watertight = mesh.is_watertight
    before_verts = len(mesh.vertices)
    before_faces = len(mesh.faces)

    trimesh.repair.fix_normals(mesh)
    trimesh.repair.fix_winding(mesh)
    trimesh.repair.fill_holes(mesh)
    mesh.remove_degenerate_faces()
    mesh.remove_duplicate_faces()
    mesh.remove_unreferenced_vertices()
    mesh.merge_vertices(merge_tex=True)

    after_watertight = mesh.is_watertight

    issues_before, issues_after = [], []
    if not before_watertight:
        issues_before.append("STL 非封闭（有孔/缺口）")
    if not after_watertight:
        issues_after.append("修复后仍非封闭（孔洞过大或拓扑复杂）")

    # 2. Try FreeCAD Mesh → Part → STEP conversion
    step_output = output_path if output_path.lower().endswith((".step", ".stp")) else None
    step_success = False
    doc = None
    try:
        import Mesh as FcMesh  # type: ignore
        tmp_stl = input_path + "_repaired_tmp.stl"
        mesh.export(tmp_stl)

        doc = FreeCAD.newDocument("stl_clean")
        FcMesh.insert(tmp_stl, doc.Name)
        doc.recompute()
        mesh_obj = doc.Objects[0]

        shape = Part.Shape()
        shape.makeShapeFromMesh(mesh_obj.Mesh.Topology, tol)
        solid_shape = Part.makeSolid(shape)

        if solid_shape and not solid_shape.isNull():
            solid_shape = _shape_fix(solid_shape, tol)
            after_diag = _diagnose(solid_shape)
        else:
            after_diag = _diagnose(shape)
            solid_shape = shape

        out = step_output or (output_path.rsplit(".", 1)[0] + ".step")
        solid_shape.exportStep(out)
        output_path = out
        step_success = True

        try:
            os.remove(tmp_stl)
        except Exception:
            pass

    except Exception as exc:
        issues_after.append(f"B-rep 转换失败，输出修复后的 STL：{exc}")
        after_diag = {
            "is_valid": after_watertight, "is_closed": after_watertight,
            "solids": 1 if after_watertight else 0,
            "faces": len(mesh.faces), "vertices": len(mesh.vertices),
            "free_edges": 0, "tiny_faces": 0, "bbox": [], "bbox_size": 0.0,
        }
        mesh.export(output_path)
    finally:
        if doc:
            try:
                FreeCAD.closeDocument(doc.Name)
            except Exception:
                pass

    before_diag = {
        "is_valid": before_watertight, "is_closed": before_watertight,
        "solids": 0, "faces": before_faces, "vertices": before_verts,
        "free_edges": 0 if before_watertight else "?", "tiny_faces": 0,
        "bbox": [], "bbox_size": 0.0,
    }

    fixes = []
    if not before_watertight and after_watertight:
        fixes.append("trimesh 修复了非封闭 STL（填孔 + 法向一致化）")
    if step_success:
        fixes.append("成功将 STL 网格转换为 STEP B-rep（便于高质量体网格划分）")

    script = _gen_script_stl(input_path, output_path, tol)
    return {
        "success":       True,
        "before":        before_diag,
        "after":         after_diag,
        "issues_before": issues_before,
        "issues_after":  issues_after,
        "fixes_applied": fixes,
        "output_path":   output_path,
        "script":        script,
    }


# ── Script generation ─────────────────────────────────────────────────────────

def _gen_script_step(inp, out, tol, sew_tol) -> str:
    return f"""import FreeCAD, Part

# 1. 加载原始 STEP
doc = FreeCAD.newDocument("geom_clean")
Part.insert("{inp}", doc.Name); doc.recompute()
shape = doc.Objects[0].Shape

# 2. ShapeFix：修复公差 / 退化面 / 悬空边
fixer = Part.ShapeFix.Shape(shape)
fixer.Perform()
shape = fixer.Shape()

# 3. FixSmallFace：移除极小面
sf = Part.ShapeFix.FixSmallFace(shape)
sf.Perform(); shape = sf.FixShape()

# 4. 缝合（tol={sew_tol:.3g}）：封闭壳体间的裂缝
sewing = Part.BRepBuilderAPI.MakeSewing()
for f in shape.Faces: sewing.Add(f)
sewing.Perform({sew_tol})
shape = sewing.SewedShape()

# 5. makeSolid：将封闭壳体提升为实体
if shape.Shells and not shape.Solids:
    shape = Part.makeSolid(shape.Shells[0])

# 6. 导出干净 STEP
shape.exportStep("{out}")
"""


def _gen_script_stl(inp, out, tol) -> str:
    return f"""import trimesh, FreeCAD, Part, Mesh as FcMesh

# 1. trimesh 网格修复
mesh = trimesh.load("{inp}", force="mesh")
trimesh.repair.fix_normals(mesh)
trimesh.repair.fill_holes(mesh)
mesh.remove_degenerate_faces()
mesh.merge_vertices()

# 2. 导出修复后的 STL，再用 FreeCAD 转 STEP
tmp = "{inp}_repaired.stl"
mesh.export(tmp)

doc = FreeCAD.newDocument("stl2step")
FcMesh.insert(tmp, doc.Name); doc.recompute()
shape = Part.Shape()
shape.makeShapeFromMesh(doc.Objects[0].Mesh.Topology, {tol})
solid = Part.makeSolid(shape)

fixer = Part.ShapeFix.Shape(solid)
fixer.Perform(); solid = fixer.Shape()

solid.exportStep("{out}")
"""


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    raw = sys.stdin.readline()
    try:
        args = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(json.dumps({"success": False, "error": f"bad JSON: {exc}"}))
        return

    input_path  = args["input_path"]
    output_path = args["output_path"]
    tol         = float(args.get("tol", 0.01))

    ext = Path(input_path).suffix.lower()
    if ext in (".step", ".stp"):
        result = clean_step(input_path, output_path, tol)
    elif ext == ".stl":
        result = clean_stl(input_path, output_path, tol)
    else:
        result = {"success": False, "error": f"不支持的格式：{ext}（仅支持 STEP/STL）"}

    print(json.dumps(result))


if __name__ == "__main__":
    main()
