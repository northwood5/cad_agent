# -*- coding: utf-8 -*-
"""
AgentScope 2.x ToolBase implementations wrapping CADScene.

FreeCAD (via freecad_bridge) is the primary geometry engine.
trimesh is used as a fallback when FreeCAD is unavailable or fails,
and for operations FreeCAD does not support (extrude_polygon).
"""
import json
import asyncio
import uuid
from pathlib import Path
from typing import Any

from agentscope.tool import ToolBase
from agentscope.tool._response import ToolChunk
from agentscope.permission import (
    PermissionContext,
    PermissionDecision,
    PermissionBehavior,
)
from agentscope.message import TextBlock, ToolResultState

from .cad_engine import CADScene
from . import freecad_bridge


def _ok_chunk(data: dict) -> ToolChunk:
    return ToolChunk(
        content=[TextBlock(text=json.dumps(data, ensure_ascii=False, indent=2))],
        state=ToolResultState.SUCCESS if data.get("success") else ToolResultState.ERROR,
        is_last=True,
    )


def _auto_allow() -> PermissionDecision:
    return PermissionDecision(
        behavior=PermissionBehavior.ALLOW,
        message="CAD operation auto-allowed (local, non-destructive)",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _load_fc_stl(scene: CADScene, name: str, stl_path: Path) -> None:
    """Load a FreeCAD-produced STL back into the trimesh scene for the viewer."""
    await asyncio.to_thread(scene.load_stl_into_scene, name, stl_path)


# ---------------------------------------------------------------------------
# create_primitive
# ---------------------------------------------------------------------------

class CreatePrimitive(ToolBase):
    """Create a basic 3-D primitive shape in the scene."""

    name: str = "create_primitive"
    description: str = (
        "Create a primitive 3-D shape and add it to the CAD scene.\n"
        "Supported types: box, cylinder, sphere, cone, torus.\n"
        "All dimensions in millimetres.\n"
        "Use fillet_radius to round edges (box and cylinder only, via FreeCAD)."
    )
    input_schema: dict = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Unique name for this shape (e.g. 'main_body', 'top_hole')",
            },
            "shape_type": {
                "type": "string",
                "enum": ["box", "cylinder", "sphere", "cone", "torus"],
                "description": "Type of primitive to create",
            },
            "length":       {"type": "number", "description": "Box length (X) in mm"},
            "width":        {"type": "number", "description": "Box width (Y) in mm"},
            "height":       {"type": "number", "description": "Height in mm (box / cylinder / cone)"},
            "radius":       {"type": "number", "description": "Radius in mm (cylinder / sphere / cone)"},
            "major_radius": {"type": "number", "description": "Major radius of torus in mm"},
            "minor_radius": {"type": "number", "description": "Minor radius of torus in mm"},
            "segments": {
                "type": "integer",
                "description": "Resolution for curved shapes — trimesh fallback only (default 64)",
                "default": 64,
            },
            "fillet_radius": {
                "type": "number",
                "description": (
                    "Round edges with this radius in mm (box / cylinder only). "
                    "0 = no fillet (default)."
                ),
                "default": 0,
            },
        },
        "required": ["name", "shape_type"],
    }
    is_concurrency_safe: bool = False
    is_read_only: bool = False

    def __init__(self, scene: CADScene) -> None:
        self._scene = scene

    async def check_permissions(self, tool_input: dict, context: PermissionContext) -> PermissionDecision:
        return _auto_allow()

    async def __call__(
        self,
        name: str,
        shape_type: str,
        length: float = 10.0,
        width: float = 10.0,
        height: float = 10.0,
        radius: float = 5.0,
        major_radius: float = 10.0,
        minor_radius: float = 3.0,
        segments: int = 64,
        fillet_radius: float = 0.0,
        **_: Any,
    ) -> ToolChunk:
        stl_path = self._scene.output_dir / f"{name}.stl"

        # ── FreeCAD primary path ─────────────────────────────────────────────
        fc_result = await freecad_bridge.fc_create_primitive(
            doc_path=self._scene.fc_doc_path,
            stl_path=stl_path,
            name=name,
            shape_type=shape_type,
            length=length, width=width, height=height,
            radius=radius,
            major_radius=major_radius, minor_radius=minor_radius,
            fillet_radius=fillet_radius,
        )
        if fc_result.get("success"):
            await _load_fc_stl(self._scene, name, stl_path)
            mesh = self._scene.shapes[name]
            return _ok_chunk({
                "success": True,
                "name": name,
                "type": shape_type,
                "fillet_radius": fillet_radius,
                "engine": "freecad",
                "vertices": len(mesh.vertices),
                "faces": len(mesh.faces),
                "bounds": fc_result.get("bounds"),
            })

        # ── trimesh fallback ─────────────────────────────────────────────────
        import trimesh, trimesh.creation
        import numpy as np

        logger_warn = f"FreeCAD failed ({fc_result.get('error')}), falling back to trimesh"

        result: dict
        if shape_type == "box":
            result = await asyncio.to_thread(self._scene.create_box, name, length, width, height)
        elif shape_type == "cylinder":
            result = await asyncio.to_thread(self._scene.create_cylinder, name, radius, height, segments)
        elif shape_type == "sphere":
            result = await asyncio.to_thread(self._scene.create_sphere, name, radius)
        elif shape_type == "cone":
            result = await asyncio.to_thread(self._scene.create_cone, name, radius, height, segments)
        elif shape_type == "torus":
            result = await asyncio.to_thread(self._scene.create_torus, name, major_radius, minor_radius)
        else:
            result = {"success": False, "error": f"Unknown shape_type: {shape_type}"}

        if result.get("success"):
            result["engine"] = "trimesh"
            result["warning"] = logger_warn
        return _ok_chunk(result)


# ---------------------------------------------------------------------------
# boolean_operation
# ---------------------------------------------------------------------------

class BooleanOperation(ToolBase):
    """Combine two named shapes with a boolean operation."""

    name: str = "boolean_operation"
    description: str = (
        "Perform a boolean operation between two existing shapes.\n"
        "- union: merge both shapes into one\n"
        "- difference: subtract shape_b from shape_a\n"
        "- intersection: keep only the overlapping volume"
    )
    input_schema: dict = {
        "type": "object",
        "properties": {
            "result_name": {"type": "string", "description": "Name for the resulting shape"},
            "operation": {"type": "string", "enum": ["union", "difference", "intersection"]},
            "shape_a": {"type": "string", "description": "Name of the first (base) shape"},
            "shape_b": {"type": "string", "description": "Name of the second (tool) shape"},
        },
        "required": ["result_name", "operation", "shape_a", "shape_b"],
    }
    is_concurrency_safe: bool = False
    is_read_only: bool = False

    def __init__(self, scene: CADScene) -> None:
        self._scene = scene

    async def check_permissions(self, tool_input: dict, context: PermissionContext) -> PermissionDecision:
        return _auto_allow()

    async def __call__(
        self,
        result_name: str,
        operation: str,
        shape_a: str,
        shape_b: str,
        **_: Any,
    ) -> ToolChunk:
        stl_path = self._scene.output_dir / f"{result_name}.stl"

        # ── FreeCAD primary path ─────────────────────────────────────────────
        fc_result = await freecad_bridge.fc_boolean_op(
            doc_path=self._scene.fc_doc_path,
            stl_path=stl_path,
            result_name=result_name,
            operation=operation,
            shape_a=shape_a,
            shape_b=shape_b,
        )
        if fc_result.get("success"):
            await _load_fc_stl(self._scene, result_name, stl_path)
            mesh = self._scene.shapes[result_name]
            return _ok_chunk({
                "success": True,
                "name": result_name,
                "operation": operation,
                "inputs": [shape_a, shape_b],
                "engine": "freecad",
                "vertices": len(mesh.vertices),
                "faces": len(mesh.faces),
            })

        # ── trimesh fallback ─────────────────────────────────────────────────
        result = await asyncio.to_thread(
            self._scene.boolean_op, result_name, operation, shape_a, shape_b
        )
        if result.get("success"):
            result["engine"] = "trimesh"
            result["warning"] = fc_result.get("error")
        return _ok_chunk(result)


# ---------------------------------------------------------------------------
# transform_shape
# ---------------------------------------------------------------------------

class TransformShape(ToolBase):
    """Translate, rotate, or scale an existing shape."""

    name: str = "transform_shape"
    description: str = (
        "Apply geometric transforms to an existing shape in the scene.\n"
        "translate: move by [dx, dy, dz] in mm\n"
        "rotate_axis + rotate_angle_deg: rotate around axis by angle\n"
        "scale: uniform (number) or per-axis ([sx, sy, sz])"
    )
    input_schema: dict = {
        "type": "object",
        "properties": {
            "shape_name": {"type": "string", "description": "Name of the shape to transform"},
            "translate": {
                "type": "array", "items": {"type": "number"},
                "minItems": 3, "maxItems": 3,
                "description": "[dx, dy, dz] translation in mm",
            },
            "rotate_axis": {
                "type": "array", "items": {"type": "number"},
                "minItems": 3, "maxItems": 3,
                "description": "[x, y, z] rotation axis vector",
            },
            "rotate_angle_deg": {"type": "number", "description": "Rotation angle in degrees"},
            "scale": {
                "description": "Uniform scale (number) or [sx, sy, sz]",
                "oneOf": [
                    {"type": "number"},
                    {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3},
                ],
            },
        },
        "required": ["shape_name"],
    }
    is_concurrency_safe: bool = False
    is_read_only: bool = False

    def __init__(self, scene: CADScene) -> None:
        self._scene = scene

    async def check_permissions(self, tool_input: dict, context: PermissionContext) -> PermissionDecision:
        return _auto_allow()

    async def __call__(
        self,
        shape_name: str,
        translate: list | None = None,
        rotate_axis: list | None = None,
        rotate_angle_deg: float | None = None,
        scale: Any = None,
        **_: Any,
    ) -> ToolChunk:
        stl_path = self._scene.output_dir / f"{shape_name}.stl"

        # ── FreeCAD primary path ─────────────────────────────────────────────
        fc_result = await freecad_bridge.fc_transform(
            doc_path=self._scene.fc_doc_path,
            stl_path=stl_path,
            name=shape_name,
            translate=translate,
            rotate_axis=rotate_axis,
            rotate_angle_deg=rotate_angle_deg,
            scale=scale,
        )
        if fc_result.get("success"):
            await _load_fc_stl(self._scene, shape_name, stl_path)
            return _ok_chunk({"success": True, "name": shape_name, "engine": "freecad"})

        # ── trimesh fallback ─────────────────────────────────────────────────
        result = await asyncio.to_thread(
            self._scene.transform_shape,
            shape_name, translate, rotate_axis, rotate_angle_deg, scale,
        )
        if result.get("success"):
            result["engine"] = "trimesh"
        return _ok_chunk(result)


# ---------------------------------------------------------------------------
# extrude_polygon  (trimesh only — FreeCAD extrusion not yet bridged)
# ---------------------------------------------------------------------------

class ExtrudePolygon(ToolBase):
    """Extrude a 2-D polygon outline into a 3-D solid."""

    name: str = "extrude_polygon"
    description: str = (
        "Create a 3-D solid by extruding a 2-D polygon defined by its vertices.\n"
        "Useful for L-shapes, T-profiles, custom cross-sections, etc."
    )
    input_schema: dict = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Name for the resulting shape"},
            "vertices": {
                "type": "array",
                "items": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2},
                "minItems": 3,
                "description": "Ordered 2-D vertices [[x,y], ...] of the polygon cross-section",
            },
            "height": {"type": "number", "description": "Extrusion height in mm"},
        },
        "required": ["name", "vertices", "height"],
    }
    is_concurrency_safe: bool = False
    is_read_only: bool = False

    def __init__(self, scene: CADScene) -> None:
        self._scene = scene

    async def check_permissions(self, tool_input: dict, context: PermissionContext) -> PermissionDecision:
        return _auto_allow()

    async def __call__(self, name: str, vertices: list, height: float, **_: Any) -> ToolChunk:
        result = await asyncio.to_thread(self._scene.extrude_polygon, name, vertices, height)
        if result.get("success"):
            result["engine"] = "trimesh"
        return _ok_chunk(result)


# ---------------------------------------------------------------------------
# list_shapes
# ---------------------------------------------------------------------------

class ListShapes(ToolBase):
    """List all shapes currently in the CAD scene."""

    name: str = "list_shapes"
    description: str = "List all shapes currently in the CAD scene with their dimensions and status."
    input_schema: dict = {"type": "object", "properties": {}}
    is_concurrency_safe: bool = True
    is_read_only: bool = True

    def __init__(self, scene: CADScene) -> None:
        self._scene = scene

    async def check_permissions(self, tool_input: dict, context: PermissionContext) -> PermissionDecision:
        return _auto_allow()

    async def __call__(self, **_: Any) -> ToolChunk:
        result = await asyncio.to_thread(self._scene.list_shapes)
        return _ok_chunk(result)


# ---------------------------------------------------------------------------
# export_model
# ---------------------------------------------------------------------------

class ExportModel(ToolBase):
    """Export the current scene as a 3-D file for visualization or CAD use."""

    name: str = "export_model"
    description: str = (
        "Export all shapes in the scene as a single merged 3-D model.\n"
        "- stl (default): mesh format for 3-D preview and printing\n"
        "- obj: mesh format\n"
        "- step: standard parametric CAD format (B-rep via FreeCAD); use when the user\n"
        "  needs to open the model in SolidWorks, Fusion 360, or other CAD tools"
    )
    input_schema: dict = {
        "type": "object",
        "properties": {
            "format": {
                "type": "string",
                "enum": ["stl", "obj", "step"],
                "default": "stl",
                "description": "Output file format (stl / obj / step)",
            }
        },
    }
    is_concurrency_safe: bool = True
    is_read_only: bool = True

    def __init__(self, scene: CADScene) -> None:
        self._scene = scene

    async def check_permissions(self, tool_input: dict, context: PermissionContext) -> PermissionDecision:
        return _auto_allow()

    async def __call__(self, format: str = "stl", **_: Any) -> ToolChunk:
        import uuid as _uuid
        tag = _uuid.uuid4().hex[:8]

        if format == "step":
            step_path = self._scene.output_dir / f"model_{tag}.step"
            # Try true B-rep STEP export from the FreeCAD document first
            if self._scene.fc_doc_path.exists():
                result = await freecad_bridge.fc_export_step(
                    self._scene.fc_doc_path, step_path
                )
                if result.get("success"):
                    result.update({"filename": step_path.name, "format": "step"})
                    return _ok_chunk(result)
            # Fallback: export STL then convert (lossy)
            stl_result = await asyncio.to_thread(self._scene.export_model, "stl")
            if not stl_result.get("success"):
                return _ok_chunk(stl_result)
            fc_result = await freecad_bridge.stl_to_step(
                Path(stl_result["path"]), step_path
            )
            if fc_result.get("success"):
                fc_result.update({
                    "filename": step_path.name,
                    "path": str(step_path),
                    "format": "step",
                    "vertices": stl_result.get("vertices"),
                    "faces": stl_result.get("faces"),
                    "warning": "Converted from mesh — not true B-rep",
                })
            return _ok_chunk(fc_result)

        # STL / OBJ — always from trimesh (viewer-ready)
        if format == "stl" and self._scene.fc_doc_path.exists():
            # Export merged STL directly from the FreeCAD document
            stl_path = self._scene.output_dir / f"model_{tag}.stl"
            fc_result = await freecad_bridge.fc_export_stl(
                self._scene.fc_doc_path, stl_path
            )
            if fc_result.get("success"):
                fc_result.update({"filename": stl_path.name, "format": "stl", "engine": "freecad"})
                return _ok_chunk(fc_result)

        result = await asyncio.to_thread(self._scene.export_model, format)
        if result.get("success"):
            result["engine"] = "trimesh"
        return _ok_chunk(result)


# ---------------------------------------------------------------------------
# reset_scene
# ---------------------------------------------------------------------------

class ResetScene(ToolBase):
    """Clear all shapes from the CAD scene and start fresh."""

    name: str = "reset_scene"
    description: str = "Remove all shapes from the current CAD scene."
    input_schema: dict = {"type": "object", "properties": {}}
    is_concurrency_safe: bool = False
    is_read_only: bool = False

    def __init__(self, scene: CADScene) -> None:
        self._scene = scene

    async def check_permissions(self, tool_input: dict, context: PermissionContext) -> PermissionDecision:
        return _auto_allow()

    async def __call__(self, **_: Any) -> ToolChunk:
        # Delete the FreeCAD document so the next operation starts fresh
        fc_doc = self._scene.fc_doc_path
        if fc_doc.exists():
            fc_doc.unlink()
        result = await asyncio.to_thread(self._scene.reset_scene)
        return _ok_chunk(result)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def build_cad_toolkit(scene: CADScene) -> list[ToolBase]:
    """Return all CAD tools bound to the given scene."""
    return [
        CreatePrimitive(scene),
        BooleanOperation(scene),
        TransformShape(scene),
        ExtrudePolygon(scene),
        ListShapes(scene),
        ExportModel(scene),
        ResetScene(scene),
    ]
