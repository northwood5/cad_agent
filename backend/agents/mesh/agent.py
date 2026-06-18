# -*- coding: utf-8 -*-
"""
MeshSpecialist — placeholder for the mesh-generation agent.

The framework, registration, orchestration and event plumbing are complete;
the actual mesh software integration (e.g. Gmsh: read STEP -> generate volume
mesh -> export .msh) will be implemented later. For now this specialist reports
what it *would* do with the upstream geometry so the workflow stays coherent
end to end.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..base import SpecialistAgent, TaskContext


class MeshSpecialist(SpecialistAgent):
    name = "mesh"
    display_name = "网格剖分"
    capabilities = (
        "对 CAD 几何（STEP/STL）进行网格剖分，生成用于有限元/CFD 仿真的体网格或面网格。"
        "（占位：未来调用 Gmsh，当前仅说明将执行的操作）"
    )
    input_kinds = ["step", "stl"]
    output_kinds = ["mesh"]

    async def run(self, instruction: str, context: TaskContext):
        upstream = context.latest("step") or context.latest("stl")
        src = Path(upstream).name if upstream else "（无上游几何）"

        msg = (
            f"【网格剖分 Agent（占位）】\n"
            f"接收到指令：{instruction}\n"
            f"上游几何：{src}\n"
            f"计划操作：导入几何 → 设定网格尺寸/算法 → 生成体网格 → 导出 .msh。\n"
            f"该步骤的真实 Gmsh 集成尚未实现，已跳过实际计算。"
        )
        yield {"type": "text_start"}
        yield {"type": "text_delta", "text": msg}
        yield {"type": "text_end"}
