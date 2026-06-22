#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Standalone mesh worker — invoked as a subprocess by bridge.py.

Reads one JSON line from stdin:
  { "geo": "<path>", "msh": "<path>", "inp": "<path>", "lc": <float> }

Writes one JSON line to stdout with the result.

This file is executed with the cax conda Python so that gmsh and all its
native libraries (libGLU, libGL, …) are found on the system path.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path


def main():
    args = json.loads(sys.stdin.readline())
    geo_path = Path(args["geo"])
    msh_path = Path(args["msh"])
    inp_path = Path(args["inp"])
    lc       = float(args["lc"])

    try:
        import gmsh
    except ImportError as exc:
        print(json.dumps({"success": False, "error": f"gmsh import failed: {exc}"}))
        return

    gmsh.initialize()
    gmsh.option.setNumber("General.Terminal", 0)
    try:
        gmsh.logger.start()
    except Exception:
        pass

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
            print(json.dumps({"success": False, "error": f"Unsupported geometry: {ext}"}))
            return

        vols  = gmsh.model.getEntities(3)
        surfs = gmsh.model.getEntities(2)

        if vols:
            gmsh.model.addPhysicalGroup(3, [v[1] for v in vols], tag=1, name="Eall")
        elif surfs:
            gmsh.model.addPhysicalGroup(2, [s[1] for s in surfs], tag=1, name="Eall")

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
        gmsh.model.mesh.generate(3 if vols else 2)

        all_node_tags, all_coords, _ = gmsh.model.mesh.getNodes()
        n_nodes = len(all_node_tags)

        node_map = {}
        for i, nid in enumerate(all_node_tags):
            node_map[int(nid)] = [
                float(all_coords[3 * i]),
                float(all_coords[3 * i + 1]),
                float(all_coords[3 * i + 2]),
            ]

        elem_types, elem_tags_list, elem_node_tags = gmsh.model.mesh.getElements(
            3 if vols else 2
        )
        n_elems = sum(len(t) for t in elem_tags_list)

        tet4 = []
        for i, etype in enumerate(elem_types):
            if etype == 4:
                etags = elem_node_tags[i]
                for j, etag in enumerate(elem_tags_list[i]):
                    tet4.append([int(etag), [int(etags[4 * j + k]) for k in range(4)]])

        bb = list(gmsh.model.getBoundingBox(-1, -1))

        if node_map:
            x_min_n = min(v[0] for v in node_map.values())
            x_max_n = max(v[0] for v in node_map.values())
            tol_n = max((x_max_n - x_min_n) * 0.01, 0.01)
            fix_nodes  = [nid for nid, (x, y, z) in node_map.items() if abs(x - x_min_n) < tol_n]
            load_nodes = [nid for nid, (x, y, z) in node_map.items() if abs(x - x_max_n) < tol_n]
        else:
            fix_nodes, load_nodes = [], []

        gmsh.write(str(msh_path))
        gmsh.option.setNumber("Mesh.SaveAll", 1)
        gmsh.write(str(inp_path))
        gmsh.finalize()

        print(json.dumps({
            "success": True,
            "nodes":        n_nodes,
            "elements":     n_elems,
            "tet4_elements": tet4,
            "node_map":     node_map,
            "fix_nodes":    fix_nodes,
            "load_nodes":   load_nodes,
            "bounding_box": bb,
        }))

    except Exception as exc:
        log_text = ""
        try:
            log_text = "\n".join(gmsh.logger.get()[-30:])
        except Exception:
            pass
        try:
            gmsh.finalize()
        except Exception:
            pass
        print(json.dumps({"success": False, "error": str(exc), "log": log_text}))


if __name__ == "__main__":
    main()
