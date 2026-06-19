# -*- coding: utf-8 -*-
"""
CalculiX bridge — write .inp, run ccx, parse .frd results.

Public API
----------
run_static_analysis(mesh_result, material, total_force_z, workspace) → dict
  mesh_result   : dict returned by mesh bridge (has node_map, tet4_elements, etc.)
  material      : dict with keys 'E' (MPa), 'nu', 'name'
  total_force_z : total force in Z direction applied to loaded nodes (N)
  workspace     : Path where output files are written
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import math
import os
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="ccx"
)

CCX_BIN = str(Path(sys.prefix) / "bin" / "ccx")
if not os.path.exists(CCX_BIN):
    CCX_BIN = "ccx"  # rely on PATH


# ── CalculiX .inp writer ──────────────────────────────────────────────────────

def _write_ccx_inp(
    inp_path: Path,
    node_map: dict,
    tet4_elements: list,
    fix_nodes: list,
    load_nodes: list,
    material: dict,
    total_force_z: float,
) -> None:
    E   = material.get("E",  210000.0)   # MPa
    nu  = material.get("nu", 0.3)
    name = material.get("name", "material")

    load_per_node = total_force_z / max(len(load_nodes), 1)

    with open(inp_path, "w") as f:
        f.write("*Heading\n CAE static analysis\n")

        # Nodes
        f.write("*Node\n")
        for nid, (x, y, z) in node_map.items():
            f.write(f"{nid}, {x:.6e}, {y:.6e}, {z:.6e}\n")

        # C3D4 volume elements
        if tet4_elements:
            f.write("*Element, type=C3D4, elset=Eall\n")
            for etag, ns in tet4_elements:
                f.write(f"{etag}, {ns[0]}, {ns[1]}, {ns[2]}, {ns[3]}\n")

        # Fixed-end node set
        if fix_nodes:
            f.write("*Nset, nset=Nfix\n")
            _write_nset(f, fix_nodes)

        # Loaded-end node set
        if load_nodes:
            f.write("*Nset, nset=Nload\n")
            _write_nset(f, load_nodes)

        # Material
        f.write(f"*Material, name={name}\n")
        f.write("*Elastic\n")
        f.write(f" {E:.2f}, {nu:.4f}\n")

        # Section
        f.write(f"*Solid Section, elset=Eall, material={name}\n")
        f.write(" 1.0\n")

        # Step
        f.write("*Step\n*Static\n")

        # Boundary conditions
        if fix_nodes:
            f.write("*Boundary\nNfix, 1, 3, 0.0\n")

        # Concentrated loads (Z direction)
        if load_nodes:
            f.write("*Cload\n")
            for nid in load_nodes:
                f.write(f"{nid}, 3, {load_per_node:.6e}\n")

        # Output
        f.write("*Node File\nU, RF\n")
        f.write("*El File\nS, E\n")
        f.write("*End Step\n")


def _write_nset(f, node_ids: list) -> None:
    for i, nid in enumerate(node_ids):
        if i > 0 and i % 16 == 0:
            f.write("\n")
        elif i > 0:
            f.write(", ")
        f.write(str(nid))
    f.write("\n")


# ── CalculiX runner ───────────────────────────────────────────────────────────

def _run_ccx(inp_path: Path) -> dict:
    """Run ccx on *inp_path* (without the .inp extension as ccx expects)."""
    job = str(inp_path.with_suffix(""))
    try:
        proc = subprocess.run(
            [CCX_BIN, "-i", job],
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(inp_path.parent),
        )
        ok = proc.returncode == 0 or "Job finished" in proc.stdout
        return {
            "success": ok,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "returncode": proc.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "CalculiX timed out (> 300 s)"}
    except FileNotFoundError:
        return {"success": False, "error": f"CalculiX binary not found: {CCX_BIN}"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


# ── FRD parser ────────────────────────────────────────────────────────────────

def _parse_frd_line(line: str) -> tuple[int, list[float]]:
    """Parse one -1 record from a .frd file using 12-char fixed-width fields."""
    nid = int(line[4:13])
    data_str = line[13:].rstrip()
    vals: list[float] = []
    pos = 0
    while pos + 12 <= len(data_str):
        token = data_str[pos : pos + 12].strip()
        if token:
            vals.append(float(token))
        pos += 12
    return nid, vals


def _parse_frd(frd_path: Path) -> dict:
    """
    Parse a CalculiX .frd result file.

    Returns dict with:
      nodes        : {nid: (x, y, z)}
      displacements: {nid: [u1, u2, u3]}
      stresses     : {nid: [s11, s22, s33, s12, s23, s13]}
    """
    nodes: dict[int, tuple] = {}
    displacements: dict[int, list] = {}
    stresses: dict[int, list] = {}

    in_nodes = False
    in_disp  = False
    in_stress = False

    try:
        with open(frd_path) as f:
            lines = f.readlines()
    except OSError:
        return {"nodes": nodes, "displacements": displacements, "stresses": stresses}

    for line in lines:
        raw = line.rstrip("\n")

        # Node block start
        if raw.startswith("    2C"):
            in_nodes = True
            in_disp = in_stress = False
            continue

        # Result header
        if " -4  DISP" in raw:
            in_disp = True
            in_nodes = in_stress = False
            continue
        if " -4  STRESS" in raw:
            in_stress = True
            in_nodes = in_disp = False
            continue

        # End of block
        if raw.startswith("   -3") or raw.startswith(" -3"):
            in_nodes = in_disp = in_stress = False
            continue

        # Skip component-header lines
        if raw.strip().startswith("-5"):
            continue

        if raw.startswith(" -1"):
            try:
                nid, vals = _parse_frd_line(raw)
            except (ValueError, IndexError):
                continue
            if in_nodes:
                nodes[nid] = (vals[0], vals[1], vals[2]) if len(vals) >= 3 else (0, 0, 0)
            elif in_disp:
                displacements[nid] = vals
            elif in_stress:
                stresses[nid] = vals

    return {"nodes": nodes, "displacements": displacements, "stresses": stresses}


# ── Derived metrics ───────────────────────────────────────────────────────────

def _von_mises(s: list[float]) -> float:
    if len(s) < 6:
        return 0.0
    s11, s22, s33, s12, s23, s13 = s[:6]
    return math.sqrt(
        0.5 * (
            (s11 - s22) ** 2
            + (s22 - s33) ** 2
            + (s33 - s11) ** 2
            + 6 * (s12 ** 2 + s23 ** 2 + s13 ** 2)
        )
    )


def _compute_metrics(frd_data: dict) -> dict:
    disps  = frd_data["displacements"]
    stresses = frd_data["stresses"]

    max_disp = 0.0
    if disps:
        max_disp = max(
            math.sqrt(sum(v ** 2 for v in vals[:3])) for vals in disps.values()
        )

    max_vm = 0.0
    vm_by_node: dict[int, float] = {}
    if stresses:
        for nid, vals in stresses.items():
            vm = _von_mises(vals)
            vm_by_node[nid] = vm
        max_vm = max(vm_by_node.values()) if vm_by_node else 0.0

    return {
        "max_displacement_mm": max_disp,
        "max_von_mises_mpa": max_vm,
        "vm_by_node": vm_by_node,
        "displacements": disps,
        "nodes": frd_data["nodes"],
    }


# ── Public async entry point ──────────────────────────────────────────────────

def _blocking_analysis(
    mesh_result: dict,
    material: dict,
    total_force_z: float,
    workspace: Path,
    job_name: str,
) -> dict:
    inp_path = workspace / f"{job_name}.inp"
    frd_path = workspace / f"{job_name}.frd"

    node_map      = mesh_result.get("node_map", {})
    tet4_elements = mesh_result.get("tet4_elements", [])
    fix_nodes     = mesh_result.get("fix_nodes", [])
    load_nodes    = mesh_result.get("load_nodes", [])

    if not node_map or not tet4_elements:
        return {"success": False, "error": "No mesh data available (need 3D tet elements)"}
    if not fix_nodes:
        return {"success": False, "error": "No fixed boundary nodes found — cannot constrain the model"}
    if not load_nodes:
        return {"success": False, "error": "No loaded boundary nodes found"}

    _write_ccx_inp(inp_path, node_map, tet4_elements, fix_nodes, load_nodes,
                   material, total_force_z)

    run_result = _run_ccx(inp_path)
    if not run_result.get("success"):
        err = run_result.get("error") or run_result.get("stderr", "")[:300]
        return {"success": False, "error": f"CalculiX failed: {err}",
                "stdout": run_result.get("stdout", "")}

    frd_data = _parse_frd(frd_path)
    metrics  = _compute_metrics(frd_data)

    return {
        "success": True,
        "inp_path": str(inp_path),
        "frd_path": str(frd_path),
        "metrics": metrics,
        "stdout": run_result.get("stdout", ""),
    }


async def run_static_analysis(
    mesh_result: dict,
    material: dict,
    total_force_z: float,
    workspace: Path,
    job_name: str = "cae_result",
) -> dict:
    """Async entry point — dispatches blocking work to the ccx thread."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _executor, _blocking_analysis,
        mesh_result, material, total_force_z, workspace, job_name,
    )
