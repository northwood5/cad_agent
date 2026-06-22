# -*- coding: utf-8 -*-
"""
MeshSpecialist — gmsh-based mesh generation agent.

Accepts a STEP or STL upstream artifact, generates a tetrahedral volume mesh
with gmsh, and emits:
  - <workspace>/mesh_<stem>.msh   (gmsh native format)
  - <workspace>/mesh_<stem>.inp   (Abaqus/CalculiX format for downstream CAE)

Mesh parameters can be specified in the instruction:
  "细网格"  / "fine"           → lc = geometry_size / 20
  "中等网格" / "medium" (default) → lc = geometry_size / 10
  "粗网格"  / "coarse"         → lc = geometry_size / 5
  or a bare number like "网格尺寸 3.5"
"""
from __future__ import annotations

import re
from pathlib import Path

from ..base import SpecialistAgent, TaskContext
from .bridge import mesh_geometry


# ── helpers ──────────────────────────────────────────────────────────────────

def _parse_lc(instruction: str, bbox_size: float) -> float:
    """Derive mesh characteristic length from the instruction text."""
    lower = instruction.lower()
    # explicit number: "网格尺寸 2.5" or "lc=3" etc.
    m = re.search(r"(?:网格尺寸|lc|mesh\s+size)[^\d]*(\d+(?:\.\d+)?)", lower)
    if m:
        return float(m.group(1))
    if "细" in lower or "fine" in lower or "密" in lower:
        return bbox_size / 20
    if "粗" in lower or "coarse" in lower:
        return bbox_size / 5
    return bbox_size / 10  # medium / default


def _density_label(instruction: str) -> str:
    lower = instruction.lower()
    if re.search(r"(?:网格尺寸|lc|mesh\s+size)[^\d]*\d", lower):
        return "显式指定"
    if "细" in lower or "fine" in lower or "密" in lower:
        return "细网格 (bbox/20)"
    if "粗" in lower or "coarse" in lower:
        return "粗网格 (bbox/5)"
    return "中等网格 (bbox/10)"


def _build_gmsh_script(geo_path: Path, lc: float) -> str:
    """Representative gmsh Python-API script reflecting the actual run params."""
    ext = geo_path.suffix.lower()
    if ext in (".step", ".stp"):
        import_block = (
            f'gmsh.model.add("model")\n'
            f'gmsh.model.occ.importShapes(r"{geo_path.name}")\n'
            f"gmsh.model.occ.synchronize()"
        )
    else:
        import_block = (
            f'gmsh.merge(r"{geo_path.name}")\n'
            f"gmsh.model.mesh.classifySurfaces(math.pi, True, True, math.pi)\n"
            f"gmsh.model.mesh.createGeometry()"
        )
    return (
        f"import gmsh, math\n"
        f"gmsh.initialize()\n"
        f"gmsh.option.setNumber('General.Terminal', 0)\n\n"
        f"# 导入上游几何 {geo_path.name}\n"
        f"{import_block}\n\n"
        f"# 特征长度（lc）控制网格密度\n"
        f"gmsh.option.setNumber('Mesh.CharacteristicLengthMin', {lc * 0.3:.4g})\n"
        f"gmsh.option.setNumber('Mesh.CharacteristicLengthMax', {lc:.4g})\n\n"
        f"# 生成四面体体网格（无体则退化为面网格）\n"
        f"gmsh.model.mesh.generate(3)\n\n"
        f"gmsh.write('mesh_{geo_path.stem}.msh')\n"
        f"gmsh.option.setNumber('Mesh.SaveAll', 1)\n"
        f"gmsh.write('mesh_{geo_path.stem}.inp')  # CalculiX/Abaqus 格式\n"
        f"gmsh.finalize()\n"
    )


# ── agent ────────────────────────────────────────────────────────────────────

class MeshSpecialist(SpecialistAgent):
    name = "mesh"
    display_name = "网格剖分"
    capabilities = (
        "对 CAD 几何（STEP/STL）进行网格剖分，生成用于有限元/CFD 仿真的四面体体网格。"
        "输出 .msh（gmsh 格式）和 .inp（CalculiX/Abaqus 格式）供下游 CAE 使用。"
    )
    input_kinds = ["step", "stl"]
    output_kinds = ["mesh"]

    async def run(self, instruction: str, context: TaskContext):
        # Prefer STEP (higher fidelity), fall back to STL
        geo_path = context.latest("step") or context.latest("stl")
        if geo_path is None:
            yield {"type": "text_start"}
            yield {"type": "text_delta", "text": "错误：未找到上游几何文件（需要 STEP 或 STL）。"}
            yield {"type": "text_end"}
            yield {"type": "node_result", "ok": False, "kind": "error",
                   "error": "上游未产出可用几何（缺少 STEP/STL）；需要 CAD 先建模并导出。",
                   "diagnostics": {"have_step": context.latest("step") is not None,
                                   "have_stl": context.latest("stl") is not None}}
            return

        stem = geo_path.stem
        msh_path = self.workspace / f"mesh_{stem}.msh"
        inp_path = self.workspace / f"mesh_{stem}.inp"

        # Mesh characteristic length: use bbox hint from scratch if available
        bbox_size = context.scratch.get("bbox_size", 50.0)
        lc = _parse_lc(instruction, bbox_size)

        # ── Reasoning → 推理面板 ────────────────────────────────────────────
        yield {"type": "thinking_start"}
        yield {"type": "thinking_delta", "text": (
            f"选择上游几何：{geo_path.name}（优先 STEP，其次 STL）。\n"
            f"几何特征尺寸 bbox ≈ {bbox_size:.3g} mm。\n"
            f"解析网格密度：{_density_label(instruction)} → 特征长度 lc = {lc:.3g}。\n"
            f"调用 gmsh：导入几何 → 设定 lc → 生成四面体网格 → 导出 .msh / .inp。"
        )}
        yield {"type": "thinking_end"}

        # ── 真实执行脚本 → 脚本日志 ──────────────────────────────────────────
        yield {"type": "script_generated", "software": "gmsh", "language": "python",
               "content": _build_gmsh_script(geo_path, lc)}

        yield {"type": "text_start"}
        yield {"type": "text_delta", "text": f"正在对 {geo_path.name} 进行网格剖分…\n"}

        result = await mesh_geometry(geo_path, msh_path, inp_path, lc)

        if not result["success"]:
            msg = f"网格剖分失败：{result['error']}"
            yield {"type": "text_delta", "text": msg}
            yield {"type": "text_end"}
            yield {"type": "node_result", "ok": False, "kind": "error",
                   "error": f"gmsh 网格剖分失败：{result['error']}",
                   "diagnostics": {"geometry": geo_path.name, "lc": lc,
                                   "gmsh_log": result.get("log", ""),
                                   "density": _density_label(instruction)}}
            return

        n_nodes = result["nodes"]
        n_elems = result["elements"]
        bb = result.get("bounding_box", [])

        # Store mesh data in context for downstream CAE agent
        context.record("mesh", msh_path)
        context.record("mesh_inp", inp_path)
        context.scratch["mesh_result"] = result

        yield {"type": "thinking_start"}
        yield {"type": "thinking_delta", "text": (
            f"网格生成成功：{n_nodes} 节点 / {n_elems} 单元。\n"
            f"已标记固定端(SurfFix)与加载端(SurfLoad)边界供下游 CAE 施加约束与载荷。"
        )}
        yield {"type": "thinking_end"}

        summary = (
            f"网格生成完成：\n"
            f"  节点数：{n_nodes}\n"
            f"  四面体单元数：{n_elems}\n"
            f"  特征长度 lc = {lc:.3g}\n"
        )
        if bb:
            summary += (
                f"  几何范围：X [{bb[0]:.2f}, {bb[3]:.2f}]  "
                f"Y [{bb[1]:.2f}, {bb[4]:.2f}]  "
                f"Z [{bb[2]:.2f}, {bb[5]:.2f}]\n"
            )

        yield {"type": "text_delta", "text": summary}
        yield {"type": "text_end"}

        # Emit artifact events so the workflow records them
        yield {
            "type": "artifact_produced",
            "filename": msh_path.name,
            "kind": "mesh",
        }
        yield {
            "type": "artifact_produced",
            "filename": inp_path.name,
            "kind": "mesh_inp",
        }

        yield {"type": "node_result", "ok": True, "kind": "ok",
               "diagnostics": {"nodes": n_nodes, "elements": n_elems, "lc": lc,
                               "fix_nodes": len(result.get("fix_nodes", [])),
                               "load_nodes": len(result.get("load_nodes", [])),
                               "bounding_box": bb}}
