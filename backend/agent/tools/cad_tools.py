# -*- coding: utf-8 -*-
"""
AgentScope 2.x ToolBase implementations wrapping CADScene.
Each tool is auto-allowed (local ops, no user confirmation needed).
"""
import json
import asyncio
import uuid
from pathlib import Path
from typing import Any, AsyncGenerator

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
# create_primitive
# ---------------------------------------------------------------------------

class CreatePrimitive(ToolBase):
    """Create a basic 3-D primitive shape in the scene."""

    name: str = "create_primitive"
    description: str = (
        "Create a primitive 3-D shape and add it to the CAD scene.\n"
        "Supported types: box, cylinder, sphere, cone, torus.\n"
        "All dimensions in millimetres."
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
            "length": {"type": "number", "description": "Box length (X) in mm"},
            "width": {"type": "number", "description": "Box width (Y) in mm"},
            "height": {"type": "number", "description": "Height in mm (box / cylinder / cone)"},
            "radius": {"type": "number", "description": "Radius in mm (cylinder / sphere / cone)"},
            "major_radius": {"type": "number", "description": "Major radius of torus in mm"},
            "minor_radius": {"type": "number", "description": "Minor radius of torus in mm"},
            "segments": {
                "type": "integer",
                "description": "Resolution for curved shapes (default 64)",
                "default": 64,
            },
            "fillet_radius": {
                "type": "number",
                "description": (
                    "Round the edges with this radius in mm (box and cylinder only). "
                    "Uses FreeCAD for precise B-rep filleting. 0 = no fillet (default)."
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

    async def check_permissions(
        self, tool_input: dict[str, Any], context: PermissionContext
    ) -> PermissionDecision:
        return _auto_allow()

    async def __call__(  # type: ignore[override]
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
        result: dict

        # ── FreeCAD path: fillet requested for box or cylinder ──────────────
        if fillet_radius > 0 and shape_type in ("box", "cylinder"):
            # FreeCAD snap can only write to home directory, not /tmp
            tmp_stl = Path.home() / f"_fc_{uuid.uuid4().hex[:8]}.stl"
            if shape_type == "box":
                fc_result = await freecad_bridge.create_filleted_box(
                    length, width, height, fillet_radius, tmp_stl
                )
            else:
                fc_result = await freecad_bridge.create_filleted_cylinder(
                    radius, height, fillet_radius, tmp_stl
                )
            if fc_result.get("success"):
                result = await asyncio.to_thread(
                    self._scene.load_stl_into_scene, name, tmp_stl
                )
                if result.get("success"):
                    result.update({"type": shape_type, "fillet_radius": fillet_radius})
                tmp_stl.unlink(missing_ok=True)
            else:
                result = fc_result
            return _ok_chunk(result)

        # ── trimesh path ─────────────────────────────────────────────────────
        if shape_type == "box":
            result = await asyncio.to_thread(
                self._scene.create_box, name, length, width, height
            )
        elif shape_type == "cylinder":
            result = await asyncio.to_thread(
                self._scene.create_cylinder, name, radius, height, segments
            )
        elif shape_type == "sphere":
            result = await asyncio.to_thread(
                self._scene.create_sphere, name, radius
            )
        elif shape_type == "cone":
            result = await asyncio.to_thread(
                self._scene.create_cone, name, radius, height, segments
            )
        elif shape_type == "torus":
            result = await asyncio.to_thread(
                self._scene.create_torus, name, major_radius, minor_radius
            )
        else:
            result = {"success": False, "error": f"Unknown shape_type: {shape_type}"}
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
            "result_name": {
                "type": "string",
                "description": "Name for the resulting shape",
            },
            "operation": {
                "type": "string",
                "enum": ["union", "difference", "intersection"],
            },
            "shape_a": {
                "type": "string",
                "description": "Name of the first (base) shape",
            },
            "shape_b": {
                "type": "string",
                "description": "Name of the second (tool) shape",
            },
        },
        "required": ["result_name", "operation", "shape_a", "shape_b"],
    }
    is_concurrency_safe: bool = False
    is_read_only: bool = False

    def __init__(self, scene: CADScene) -> None:
        self._scene = scene

    async def check_permissions(
        self, tool_input: dict[str, Any], context: PermissionContext
    ) -> PermissionDecision:
        return _auto_allow()

    async def __call__(  # type: ignore[override]
        self,
        result_name: str,
        operation: str,
        shape_a: str,
        shape_b: str,
        **_: Any,
    ) -> ToolChunk:
        result = await asyncio.to_thread(
            self._scene.boolean_op, result_name, operation, shape_a, shape_b
        )
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
            "shape_name": {
                "type": "string",
                "description": "Name of the shape to transform",
            },
            "translate": {
                "type": "array",
                "items": {"type": "number"},
                "minItems": 3,
                "maxItems": 3,
                "description": "[dx, dy, dz] translation in mm",
            },
            "rotate_axis": {
                "type": "array",
                "items": {"type": "number"},
                "minItems": 3,
                "maxItems": 3,
                "description": "[x, y, z] rotation axis vector",
            },
            "rotate_angle_deg": {
                "type": "number",
                "description": "Rotation angle in degrees",
            },
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

    async def check_permissions(
        self, tool_input: dict[str, Any], context: PermissionContext
    ) -> PermissionDecision:
        return _auto_allow()

    async def __call__(  # type: ignore[override]
        self,
        shape_name: str,
        translate: list[float] | None = None,
        rotate_axis: list[float] | None = None,
        rotate_angle_deg: float | None = None,
        scale: Any = None,
        **_: Any,
    ) -> ToolChunk:
        result = await asyncio.to_thread(
            self._scene.transform_shape,
            shape_name,
            translate,
            rotate_axis,
            rotate_angle_deg,
            scale,
        )
        return _ok_chunk(result)


# ---------------------------------------------------------------------------
# extrude_polygon
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
            "name": {
                "type": "string",
                "description": "Name for the resulting shape",
            },
            "vertices": {
                "type": "array",
                "items": {
                    "type": "array",
                    "items": {"type": "number"},
                    "minItems": 2,
                    "maxItems": 2,
                },
                "minItems": 3,
                "description": "Ordered 2-D vertices [[x,y], ...] of the polygon cross-section",
            },
            "height": {
                "type": "number",
                "description": "Extrusion height in mm",
            },
        },
        "required": ["name", "vertices", "height"],
    }
    is_concurrency_safe: bool = False
    is_read_only: bool = False

    def __init__(self, scene: CADScene) -> None:
        self._scene = scene

    async def check_permissions(
        self, tool_input: dict[str, Any], context: PermissionContext
    ) -> PermissionDecision:
        return _auto_allow()

    async def __call__(  # type: ignore[override]
        self,
        name: str,
        vertices: list[list[float]],
        height: float,
        **_: Any,
    ) -> ToolChunk:
        result = await asyncio.to_thread(
            self._scene.extrude_polygon, name, vertices, height
        )
        return _ok_chunk(result)


# ---------------------------------------------------------------------------
# list_shapes
# ---------------------------------------------------------------------------

class ListShapes(ToolBase):
    """List all shapes currently in the CAD scene."""

    name: str = "list_shapes"
    description: str = (
        "List all shapes currently in the CAD scene with their dimensions and status."
    )
    input_schema: dict = {"type": "object", "properties": {}}
    is_concurrency_safe: bool = True
    is_read_only: bool = True

    def __init__(self, scene: CADScene) -> None:
        self._scene = scene

    async def check_permissions(
        self, tool_input: dict[str, Any], context: PermissionContext
    ) -> PermissionDecision:
        return _auto_allow()

    async def __call__(self, **_: Any) -> ToolChunk:  # type: ignore[override]
        result = await asyncio.to_thread(self._scene.list_shapes)
        return _ok_chunk(result)


# ---------------------------------------------------------------------------
# export_model
# ---------------------------------------------------------------------------

class ExportModel(ToolBase):
    """Export the current scene as a 3-D file for visualization."""

    name: str = "export_model"
    description: str = (
        "Export all shapes in the scene as a single merged 3-D model (STL by default).\n"
        "Call this after completing your modeling steps so the user can see the result."
    )
    input_schema: dict = {
        "type": "object",
        "properties": {
            "format": {
                "type": "string",
                "enum": ["stl", "obj", "step"],
                "default": "stl",
                "description": (
                    "Output file format. "
                    "stl/obj: mesh formats for 3-D preview. "
                    "step: standard parametric CAD format (via FreeCAD), "
                    "ideal for sharing with other CAD tools."
                ),
            }
        },
    }
    is_concurrency_safe: bool = True
    is_read_only: bool = True

    def __init__(self, scene: CADScene) -> None:
        self._scene = scene

    async def check_permissions(
        self, tool_input: dict[str, Any], context: PermissionContext
    ) -> PermissionDecision:
        return _auto_allow()

    async def __call__(self, format: str = "stl", **_: Any) -> ToolChunk:  # type: ignore[override]
        if format == "step":
            # Export mesh to STL first, then convert to STEP via FreeCAD
            stl_result = await asyncio.to_thread(self._scene.export_model, "stl")
            if not stl_result.get("success"):
                return _ok_chunk(stl_result)
            stl_path = Path(stl_result["path"])
            step_path = stl_path.with_suffix(".step")
            fc_result = await freecad_bridge.stl_to_step(stl_path, step_path)
            if fc_result.get("success"):
                fc_result.update({
                    "filename": step_path.name,
                    "path": str(step_path),
                    "format": "step",
                    "vertices": stl_result.get("vertices"),
                    "faces": stl_result.get("faces"),
                })
            return _ok_chunk(fc_result)
        result = await asyncio.to_thread(self._scene.export_model, format)
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

    async def check_permissions(
        self, tool_input: dict[str, Any], context: PermissionContext
    ) -> PermissionDecision:
        return _auto_allow()

    async def __call__(self, **_: Any) -> ToolChunk:  # type: ignore[override]
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
