# -*- coding: utf-8 -*-
"""
CAESpecialist — placeholder for the simulation/solver agent.

Future work: take a mesh + boundary conditions, write a solver input deck
(e.g. CalculiX .inp), run the solver, and parse results. For now it reports
the intended operation so the CAD -> mesh -> CAE workflow is demonstrable
end to end.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..base import SpecialistAgent, TaskContext


class CAESpecialist(SpecialistAgent):
    name = "cae"
    display_name = "CAE 仿真"
    capabilities = (
        "基于网格进行有限元仿真（结构静力/模态/热分析等），生成求解结果与云图。"
        "（占位：未来调用 CalculiX/Elmer 等求解器，当前仅说明将执行的操作）"
    )
    input_kinds = ["mesh"]
    output_kinds = ["result"]

    async def run(self, instruction: str, context: TaskContext):
        mesh = context.latest("mesh")
        src = Path(mesh).name if mesh else "（无上游网格）"

        msg = (
            f"【CAE 仿真 Agent（占位）】\n"
            f"接收到指令：{instruction}\n"
            f"上游网格：{src}\n"
            f"计划操作：施加材料/约束/载荷 → 生成求解输入 (.inp) → 调用求解器 → 解析结果。\n"
            f"该步骤的真实求解器集成尚未实现，已跳过实际计算。"
        )
        yield {"type": "text_start"}
        yield {"type": "text_delta", "text": msg}
        yield {"type": "text_end"}
