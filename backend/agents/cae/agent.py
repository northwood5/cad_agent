# -*- coding: utf-8 -*-
"""
CAESpecialist — CalculiX-based finite-element solver agent.

Reads the mesh artifact produced by MeshSpecialist (node_map + tet4_elements
stored in context.scratch["mesh_result"]), derives material properties and
loads from the instruction, and runs a linear-elastic static analysis.

The analysis result metrics are stored in context.scratch["cae_result"] so
the downstream PostSpecialist can use them.
"""
from __future__ import annotations

import re
from pathlib import Path

from ..base import SpecialistAgent, TaskContext
from .bridge import run_static_analysis


# ── Material presets (E in MPa, dimensionless nu) ────────────────────────────

_MATERIALS = {
    "steel":    {"name": "Steel",    "E": 210000.0, "nu": 0.3},
    "钢":       {"name": "Steel",    "E": 210000.0, "nu": 0.3},
    "aluminum": {"name": "Aluminum", "E":  70000.0, "nu": 0.33},
    "aluminium":{"name": "Aluminum", "E":  70000.0, "nu": 0.33},
    "铝":       {"name": "Aluminum", "E":  70000.0, "nu": 0.33},
    "copper":   {"name": "Copper",   "E": 110000.0, "nu": 0.34},
    "铜":       {"name": "Copper",   "E": 110000.0, "nu": 0.34},
    "titanium": {"name": "Titanium", "E": 114000.0, "nu": 0.34},
    "钛":       {"name": "Titanium", "E": 114000.0, "nu": 0.34},
}


def _parse_material(text: str) -> dict:
    lower = text.lower()
    for key, mat in _MATERIALS.items():
        if key in lower:
            return mat
    return _MATERIALS["steel"]  # default


def _parse_force(text: str) -> float:
    """Return total force in Z direction (N). Negative = gravity / downward."""
    lower = text.lower()
    # e.g. "1000N" "5kN" "500 牛"
    m = re.search(r"(\d+(?:\.\d+)?)\s*k?n(?:ewton)?", lower)
    if m:
        val = float(m.group(1))
        if "kn" in lower[m.start():m.end()]:
            val *= 1000
        return -val  # downward
    # fallback default: 1000 N downward
    return -1000.0


# ── agent ────────────────────────────────────────────────────────────────────

class CAESpecialist(SpecialistAgent):
    name = "cae"
    display_name = "CAE 仿真"
    capabilities = (
        "基于四面体网格进行线弹性静力有限元分析（CalculiX 求解器）。"
        "输出最大位移、最大 Von Mises 应力等指标，并保存 .frd 结果文件供后处理使用。"
    )
    input_kinds = ["mesh"]
    output_kinds = ["result"]

    async def run(self, instruction: str, context: TaskContext):
        mesh_result = context.scratch.get("mesh_result")
        if mesh_result is None:
            yield {"type": "text_start"}
            yield {
                "type": "text_delta",
                "text": "错误：未找到网格数据（需要先执行 MESH 节点）。",
            }
            yield {"type": "text_end"}
            return

        material      = _parse_material(instruction)
        total_force_z = _parse_force(instruction)

        yield {"type": "text_start"}
        yield {
            "type": "text_delta",
            "text": (
                f"启动 CalculiX 静力分析…\n"
                f"  材料：{material['name']}  E={material['E']} MPa  ν={material['nu']}\n"
                f"  总载荷：{abs(total_force_z):.0f} N（Z 方向）\n"
            ),
        }

        result = await run_static_analysis(
            mesh_result=mesh_result,
            material=material,
            total_force_z=total_force_z,
            workspace=self.workspace,
            job_name="cae_result",
        )

        if not result["success"]:
            err = result.get("error", "未知错误")
            yield {"type": "text_delta", "text": f"分析失败：{err}\n"}
            yield {"type": "text_end"}
            return

        metrics = result["metrics"]
        max_disp = metrics["max_displacement_mm"]
        max_vm   = metrics["max_von_mises_mpa"]

        summary = (
            f"分析完成：\n"
            f"  最大位移：{max_disp:.4f} mm\n"
            f"  最大 Von Mises 应力：{max_vm:.2f} MPa\n"
        )
        yield {"type": "text_delta", "text": summary}
        yield {"type": "text_end"}

        # Store results for post-processing
        context.record("result", Path(result["frd_path"]))
        context.scratch["cae_result"] = result
        context.scratch["cae_metrics"] = metrics

        yield {
            "type": "artifact_produced",
            "filename": Path(result["inp_path"]).name,
            "kind": "ccx_inp",
        }
        yield {
            "type": "artifact_produced",
            "filename": Path(result["frd_path"]).name,
            "kind": "result",
        }
