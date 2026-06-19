# -*- coding: utf-8 -*-
"""
Gmsh bridge — async wrapper for mesh generation.

Runs gmsh in a dedicated thread-pool executor (gmsh is not thread-safe but
a single-thread executor serialises all calls correctly).

Public API
----------
mesh_geometry(geo_path, msh_path, inp_path, lc) → dict
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import math
from pathlib import Path

logger = logging.getLogger(__name__)

# gmsh.initialize() installs a signal handler which fails in non-main threads.
# Use a ProcessPoolExecutor so each gmsh call runs in a dedicated subprocess
# (which has its own main thread and can install signal handlers freely).
_executor = concurrent.futures.ProcessPoolExecutor(max_workers=1)


def _do_mesh(geo_path: Path, msh_path: Path, inp_path: Path, lc: float) -> dict:
    """Blocking mesh operation — must run on the gmsh executor thread."""
    try:
        import gmsh
    except ImportError:
        return {"success": False, "error": "gmsh Python API not installed"}

    gmsh.initialize()
    gmsh.option.setNumber("General.Terminal", 0)
    try:
        ext = geo_path.suffix.lower()

        if ext in (".step", ".stp"):
            gmsh.model.add("model")
            gmsh.model.occ.importShapes(str(geo_path))
            gmsh.model.occ.synchronize()
        elif ext == ".stl":
            gmsh.merge(str(geo_path))
            gmsh.model.mesh.classifySurfaces(math.pi, True, True, math.pi)
            gmsh.model.mesh.createGeometry()
        else:
            gmsh.finalize()
            return {"success": False, "error": f"Unsupported geometry: {ext}"}

        # Physical groups so the .inp has named sets
        vols = gmsh.model.getEntities(3)
        surfs = gmsh.model.getEntities(2)

        if vols:
            gmsh.model.addPhysicalGroup(3, [v[1] for v in vols], tag=1, name="Eall")
        else:
            # Surface-only model — still useful for preview
            if surfs:
                gmsh.model.addPhysicalGroup(2, [s[1] for s in surfs], tag=1, name="Eall")

        # Tag min/max-X surfaces for boundary conditions (works for beams)
        fix_surfs, load_surfs = [], []
        if surfs:
            overall_bb = gmsh.model.getBoundingBox(-1, -1)
            x_min, x_max = overall_bb[0], overall_bb[3]
            tol = max((x_max - x_min) * 0.01, 0.01)
            for dim, tag in surfs:
                bb = gmsh.model.getBoundingBox(dim, tag)
                s_xmin, s_xmax = bb[0], bb[3]
                if abs(s_xmax - x_min) < tol:
                    fix_surfs.append(tag)
                elif abs(s_xmin - x_max) < tol:
                    load_surfs.append(tag)

        if fix_surfs:
            gmsh.model.addPhysicalGroup(2, fix_surfs, tag=10, name="SurfFix")
        if load_surfs:
            gmsh.model.addPhysicalGroup(2, load_surfs, tag=20, name="SurfLoad")

        gmsh.option.setNumber("Mesh.CharacteristicLengthMin", lc * 0.3)
        gmsh.option.setNumber("Mesh.CharacteristicLengthMax", lc)
        if vols:
            gmsh.model.mesh.generate(3)
        else:
            gmsh.model.mesh.generate(2)

        # Stats
        all_node_tags, all_coords, _ = gmsh.model.mesh.getNodes()
        n_nodes = len(all_node_tags)

        # Build node coordinate map for downstream use
        node_map: dict[int, tuple[float, float, float]] = {}
        for i, nid in enumerate(all_node_tags):
            node_map[int(nid)] = (
                float(all_coords[3 * i]),
                float(all_coords[3 * i + 1]),
                float(all_coords[3 * i + 2]),
            )

        elem_types, elem_tags_list, elem_node_tags = gmsh.model.mesh.getElements(
            3 if vols else 2
        )
        n_elems = sum(len(t) for t in elem_tags_list)

        # Extract C3D4 (etype=4) elements
        tet4: list[tuple[int, list[int]]] = []
        for i, etype in enumerate(elem_types):
            if etype == 4:  # C3D4
                etags = elem_node_tags[i]
                for j, etag in enumerate(elem_tags_list[i]):
                    tet4.append((int(etag), [int(etags[4 * j + k]) for k in range(4)]))

        # Bounding box
        bb = gmsh.model.getBoundingBox(-1, -1)

        # Boundary node identification (by bounding box)
        if node_map:
            x_min_n = min(v[0] for v in node_map.values())
            x_max_n = max(v[0] for v in node_map.values())
            tol_n = max((x_max_n - x_min_n) * 0.01, 0.01)
            fix_nodes = [
                nid for nid, (x, y, z) in node_map.items() if abs(x - x_min_n) < tol_n
            ]
            load_nodes = [
                nid for nid, (x, y, z) in node_map.items() if abs(x - x_max_n) < tol_n
            ]
        else:
            fix_nodes, load_nodes = [], []

        gmsh.write(str(msh_path))
        # Abaqus/CalculiX export for downstream solver
        gmsh.option.setNumber("Mesh.SaveAll", 1)
        gmsh.write(str(inp_path))

        gmsh.finalize()
        return {
            "success": True,
            "nodes": n_nodes,
            "elements": n_elems,
            "tet4_elements": tet4,
            "node_map": node_map,
            "fix_nodes": fix_nodes,
            "load_nodes": load_nodes,
            "bounding_box": list(bb),
        }

    except Exception as exc:
        try:
            gmsh.finalize()
        except Exception:
            pass
        logger.exception("Gmsh mesh failed: %s", geo_path)
        return {"success": False, "error": str(exc)}


async def mesh_geometry(
    geo_path: Path, msh_path: Path, inp_path: Path, lc: float = 5.0
) -> dict:
    """Async entry point — dispatches blocking work to the gmsh thread."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, _do_mesh, geo_path, msh_path, inp_path, lc)
