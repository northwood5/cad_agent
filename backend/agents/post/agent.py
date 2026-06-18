# -*- coding: utf-8 -*-
"""
PostSpecialist — placeholder for the post-processing agent.

Future work: take CAE results, produce contour plots / extract key metrics
(max stress, displacement, safety factor) and generate a report. For now it
reports the intended operation so the CAD -> mesh -> CAE -> post workflow is
demonstrable end to end.
"""
from __future__ import annotations

from pathlib import Path

from ..base import SpecialistAgent, TaskContext


class PostSpecialist(SpecialistAgent):
    name = "post"
    display_name = "后处理"
    capabilities = (
        "对 CAE 仿真结果进行后处理：生成应力/位移云图、提取关键指标（最大应力、"
        "最大变形、安全系数等）、汇总分析报告。"
        "（占位：未来对接结果可视化与报告生成，当前仅说明将执行的操作）"
    )
    input_kinds = ["result"]
    output_kinds = ["report"]

    async def run(self, instruction: str, context: TaskContext):
        result = context.latest("result")
        src = Path(result).name if result else "（无上游结果）"

        msg = (
            f"【后处理 Agent（占位）】\n"
            f"接收到指令：{instruction}\n"
            f"上游结果：{src}\n"
            f"计划操作：读取求解结果 → 生成云图 → 提取关键指标 → 汇总报告。\n"
            f"该步骤的真实后处理集成尚未实现，已跳过实际计算。"
        )
        yield {"type": "text_start"}
        yield {"type": "text_delta", "text": msg}
        yield {"type": "text_end"}
