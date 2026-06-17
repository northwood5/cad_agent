# -*- coding: utf-8 -*-
"""
CAD geometry engine: trimesh as primary backend, with FreeCAD/CadQuery
auto-detected if installed.
"""
import uuid
import asyncio
import logging
from pathlib import Path
from typing import Any

import numpy as np
import trimesh
import trimesh.boolean
import trimesh.creation
import trimesh.transformations

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional FreeCAD detection (headless)
# ---------------------------------------------------------------------------
_FREECAD_AVAILABLE = False
try:
    import FreeCAD  # type: ignore
    import Part      # type: ignore
    _FREECAD_AVAILABLE = True
    logger.info("FreeCAD detected — advanced operations available")
except ImportError:
    pass

BACKEND = "freecad" if _FREECAD_AVAILABLE else "trimesh"


class CADScene:
    """
    Manages named 3-D shapes using trimesh + manifold3d for boolean ops.
    All coordinates are in millimetres.
    """

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.shapes: dict[str, trimesh.Trimesh] = {}

    # ------------------------------------------------------------------
    # Primitives
    # ------------------------------------------------------------------

    def create_box(
        self,
        name: str,
        length: float,
        width: float,
        height: float,
    ) -> dict[str, Any]:
        mesh = trimesh.creation.box([length, width, height])
        self.shapes[name] = mesh
        return {
            "success": True,
            "name": name,
            "type": "box",
            "length": length,
            "width": width,
            "height": height,
            "vertices": len(mesh.vertices),
            "faces": len(mesh.faces),
        }

    def create_cylinder(
        self,
        name: str,
        radius: float,
        height: float,
        segments: int = 64,
    ) -> dict[str, Any]:
        mesh = trimesh.creation.cylinder(
            radius=radius, height=height, sections=segments
        )
        self.shapes[name] = mesh
        return {
            "success": True,
            "name": name,
            "type": "cylinder",
            "radius": radius,
            "height": height,
        }

    def create_sphere(
        self,
        name: str,
        radius: float,
        subdivisions: int = 4,
    ) -> dict[str, Any]:
        mesh = trimesh.creation.icosphere(subdivisions=subdivisions, radius=radius)
        self.shapes[name] = mesh
        return {"success": True, "name": name, "type": "sphere", "radius": radius}

    def create_cone(
        self,
        name: str,
        radius: float,
        height: float,
        segments: int = 64,
    ) -> dict[str, Any]:
        mesh = trimesh.creation.cone(
            radius=radius, height=height, sections=segments
        )
        self.shapes[name] = mesh
        return {
            "success": True,
            "name": name,
            "type": "cone",
            "radius": radius,
            "height": height,
        }

    def create_torus(
        self,
        name: str,
        major_radius: float,
        minor_radius: float,
        major_segments: int = 48,
        minor_segments: int = 16,
    ) -> dict[str, Any]:
        """Torus via parametric mesh generation."""
        u = np.linspace(0, 2 * np.pi, major_segments, endpoint=False)
        v = np.linspace(0, 2 * np.pi, minor_segments, endpoint=False)
        uu, vv = np.meshgrid(u, v)
        x = (major_radius + minor_radius * np.cos(vv)) * np.cos(uu)
        y = (major_radius + minor_radius * np.cos(vv)) * np.sin(uu)
        z = minor_radius * np.sin(vv)
        vertices = np.column_stack([x.ravel(), y.ravel(), z.ravel()])

        faces = []
        ms, ns = major_segments, minor_segments
        for i in range(ms):
            for j in range(ns):
                a = i * ns + j
                b = i * ns + (j + 1) % ns
                c = ((i + 1) % ms) * ns + (j + 1) % ns
                d = ((i + 1) % ms) * ns + j
                faces += [[a, b, c], [a, c, d]]

        mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=True)
        self.shapes[name] = mesh
        return {
            "success": True,
            "name": name,
            "type": "torus",
            "major_radius": major_radius,
            "minor_radius": minor_radius,
        }

    def extrude_polygon(
        self,
        name: str,
        vertices: list[list[float]],
        height: float,
    ) -> dict[str, Any]:
        """
        Extrude a 2-D polygon (vertices as [[x,y], ...]) into a 3-D solid.
        """
        try:
            from shapely.geometry import Polygon

            poly = Polygon(vertices)
            mesh = trimesh.creation.extrude_polygon(poly, height)
            self.shapes[name] = mesh
            return {
                "success": True,
                "name": name,
                "type": "extrusion",
                "vertex_count": len(vertices),
                "height": height,
            }
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    # ------------------------------------------------------------------
    # Boolean operations
    # ------------------------------------------------------------------

    def boolean_op(
        self,
        name: str,
        operation: str,
        shape_a: str,
        shape_b: str,
    ) -> dict[str, Any]:
        """Union / difference / intersection of two named shapes."""
        if shape_a not in self.shapes:
            return {"success": False, "error": f"Shape '{shape_a}' not found in scene"}
        if shape_b not in self.shapes:
            return {"success": False, "error": f"Shape '{shape_b}' not found in scene"}

        ma = self.shapes[shape_a]
        mb = self.shapes[shape_b]

        try:
            if operation == "union":
                result = trimesh.boolean.union([ma, mb], engine="manifold")
            elif operation == "difference":
                result = trimesh.boolean.difference([ma, mb], engine="manifold")
            elif operation == "intersection":
                result = trimesh.boolean.intersection([ma, mb], engine="manifold")
            else:
                return {
                    "success": False,
                    "error": f"Unknown operation '{operation}'. Use: union / difference / intersection",
                }
            self.shapes[name] = result
            return {
                "success": True,
                "name": name,
                "operation": operation,
                "inputs": [shape_a, shape_b],
                "vertices": len(result.vertices),
                "faces": len(result.faces),
            }
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    # ------------------------------------------------------------------
    # Transforms
    # ------------------------------------------------------------------

    def transform_shape(
        self,
        shape_name: str,
        translate: list[float] | None = None,
        rotate_axis: list[float] | None = None,
        rotate_angle_deg: float | None = None,
        scale: float | list[float] | None = None,
    ) -> dict[str, Any]:
        """
        Translate, rotate and/or scale a named shape (in-place).
        rotate_axis: [x, y, z] unit vector
        rotate_angle_deg: degrees
        """
        if shape_name not in self.shapes:
            return {"success": False, "error": f"Shape '{shape_name}' not found"}

        mesh = self.shapes[shape_name].copy()

        if translate:
            mesh.apply_translation(translate)

        if rotate_axis is not None and rotate_angle_deg is not None:
            axis = np.array(rotate_axis, dtype=float)
            norm = np.linalg.norm(axis)
            if norm > 0:
                axis /= norm
            angle = np.radians(rotate_angle_deg)
            mat = trimesh.transformations.rotation_matrix(angle, axis)
            mesh.apply_transform(mat)

        if scale is not None:
            if isinstance(scale, (int, float)):
                mesh.apply_scale(float(scale))
            else:
                mat = np.diag([scale[0], scale[1], scale[2], 1.0])
                mesh.apply_transform(mat)

        self.shapes[shape_name] = mesh
        return {"success": True, "name": shape_name}

    # ------------------------------------------------------------------
    # Scene management
    # ------------------------------------------------------------------

    def list_shapes(self) -> dict[str, Any]:
        result = []
        for shape_name, mesh in self.shapes.items():
            bounds = mesh.bounds.tolist() if mesh.bounds is not None else None
            result.append(
                {
                    "name": shape_name,
                    "vertices": len(mesh.vertices),
                    "faces": len(mesh.faces),
                    "bounds": bounds,
                    "is_watertight": bool(mesh.is_watertight),
                }
            )
        return {"success": True, "shapes": result, "count": len(result)}

    def export_model(self, fmt: str = "stl") -> dict[str, Any]:
        if not self.shapes:
            return {"success": False, "error": "Scene is empty — create some shapes first"}

        if len(self.shapes) == 1:
            combined = next(iter(self.shapes.values()))
        else:
            combined = trimesh.util.concatenate(list(self.shapes.values()))

        filename = f"model_{uuid.uuid4().hex[:8]}.{fmt}"
        filepath = self.output_dir / filename
        combined.export(str(filepath))

        return {
            "success": True,
            "filename": filename,
            "path": str(filepath),
            "format": fmt,
            "vertices": len(combined.vertices),
            "faces": len(combined.faces),
        }

    def reset_scene(self) -> dict[str, Any]:
        cleared = len(self.shapes)
        self.shapes.clear()
        return {"success": True, "cleared_shapes": cleared}
