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


def _trim_deck(deck: str, head: int = 12) -> str:
    """Collapse long *Node / *Element data blocks so the script log stays readable."""
    out: list[str] = []
    kept = 0
    in_data = False
    skipped = 0
    for line in deck.splitlines():
        is_keyword = line.lstrip().startswith("*")
        if is_keyword:
            if in_data and skipped:
                out.append(f"    ... (省略 {skipped} 行数据)")
            in_data = line.lstrip().lower().startswith(("*node", "*element", "*nset"))
            kept = 0
            skipped = 0
            out.append(line)
            continue
        if in_data:
            if kept < head:
                out.append(line)
                kept += 1
            else:
                skipped += 1
        else:
            out.append(line)
    if in_data and skipped:
        out.append(f"    ... (省略 {skipped} 行数据)")
    return "\n".join(out)


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
    # CAE may solve cleanly yet produce physically unreasonable numbers; flag the
    # node for an LLM quality review so the workflow can loop back if needed.
    quality_gate = True

    async def run(self, instruction: str, context: TaskContext):
        mesh_result = context.scratch.get("mesh_result")
        if mesh_result is None:
            yield {"type": "text_start"}
            yield {
                "type": "text_delta",
                "text": "错误：未找到网格数据（需要先执行 MESH 节点）。",
            }
            yield {"type": "text_end"}
            yield {"type": "node_result", "ok": False, "kind": "error",
                   "error": "上游缺少网格数据（mesh_result）；需要先成功执行 MESH 节点。",
                   "diagnostics": {}}
            return

        material      = _parse_material(instruction)
        total_force_z = _parse_force(instruction)

        n_fix  = len(mesh_result.get("fix_nodes", []))
        n_load = len(mesh_result.get("load_nodes", []))

        # ── Reasoning → 推理面板 ────────────────────────────────────────────
        yield {"type": "thinking_start"}
        yield {"type": "thinking_delta", "text": (
            f"分析类型：线弹性静力（*Static），求解器 CalculiX。\n"
            f"从指令解析材料：{material['name']}  E={material['E']} MPa  ν={material['nu']}。\n"
            f"从指令解析载荷：{abs(total_force_z):.0f} N（Z 方向）。\n"
            f"边界条件：固定端 {n_fix} 节点（全约束），加载端 {n_load} 节点（均分集中力）。\n"
            f"步骤：写 CalculiX .inp 算例 → 运行 ccx → 解析 .frd → 提取位移/Von Mises 应力。"
        )}
        yield {"type": "thinking_end"}

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

        # The .inp deck written by the bridge is the real script sent to ccx.
        inp_path = result.get("inp_path")
        if inp_path:
            try:
                deck = Path(inp_path).read_text(encoding="utf-8", errors="replace")
                yield {"type": "script_generated", "software": "calculix",
                       "language": "inp", "content": _trim_deck(deck)}
            except OSError:
                pass
        yield {"type": "script_generated", "software": "calculix", "language": "bash",
               "content": f"# 运行 CalculiX 求解器\nccx -i cae_result"}

        if not result["success"]:
            err = result.get("error", "未知错误")
            yield {"type": "text_delta", "text": f"分析失败：{err}\n"}
            yield {"type": "text_end"}
            solver_out = (result.get("stdout") or "")[-1500:]
            yield {"type": "node_result", "ok": False, "kind": "error",
                   "error": f"CalculiX 求解失败：{err}",
                   "diagnostics": {"material": material, "total_force_z": total_force_z,
                                   "fix_nodes": n_fix, "load_nodes": n_load,
                                   "solver_stdout_tail": solver_out}}
            return

        metrics = result["metrics"]
        max_disp = metrics["max_displacement_mm"]
        max_vm   = metrics["max_von_mises_mpa"]

        yield {"type": "thinking_start"}
        yield {"type": "thinking_delta", "text": (
            f"求解收敛，已解析 .frd 结果。\n"
            f"最大位移 {max_disp:.4g} mm，最大 Von Mises 应力 {max_vm:.4g} MPa。\n"
            f"结果指标已写入上下文，供后处理(POST)生成云图与报告。"
        )}
        yield {"type": "thinking_end"}

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

        # Solved cleanly — but hand the metrics to the quality gate (LLM review)
        # which judges whether the result is physically reasonable.
        bbox = mesh_result.get("bounding_box", [])
        yield {"type": "node_result", "ok": True, "kind": "quality",
               "diagnostics": {
                   "max_displacement_mm": max_disp,
                   "max_von_mises_mpa": max_vm,
                   "material": material,
                   "total_force_z": total_force_z,
                   "n_nodes": len(metrics.get("nodes", {})),
                   "bounding_box": bbox,
               }}
