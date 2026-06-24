# -*- coding: utf-8 -*-
"""
GeomCleanSpecialist — geometry healing agent.

Takes the raw CAD output (STEP or STL) from the CAD agent, diagnoses common
problems (open shells, free edges, tiny faces, non-manifold topology), applies
OCC ShapeFix + sewing + makeSolid, and exports a clean STEP that gmsh can mesh
without error.

Typical placement in a workflow:  cad → geom_clean → mesh → cae → post
"""
from __future__ import annotations

import re
from pathlib import Path

from ..base import SpecialistAgent, TaskContext
from .bridge import clean_geometry


def _parse_tol(instruction: str) -> float:
    """Extract a tolerance value from the instruction string."""
    m = re.search(r"(?:tol(?:erance)?|公差)\s*[=:＝：]?\s*(\d+(?:\.\d+)?(?:e[-+]?\d+)?)",
                  instruction, re.IGNORECASE)
    if m:
        return float(m.group(1))
    if "精细" in instruction or "tight" in instruction.lower():
        return 0.001
    if "宽松" in instruction or "loose" in instruction.lower():
        return 0.1
    return 0.01  # default


def _severity(issues: list[str]) -> str:
    if not issues:
        return "无"
    if len(issues) >= 3:
        return "严重"
    if any("无效" in i or "非封闭" in i or "自由边" in i for i in issues):
        return "中等"
    return "轻微"


class GeomCleanSpecialist(SpecialistAgent):
    name = "geom_clean"
    display_name = "几何清理"
    capabilities = (
        "对 CAD 几何（STEP/STL）进行质量检查与修复：修复公差不一致、封闭开放壳体、"
        "缝合面间裂缝、移除极小面/退化边，将几何提升为干净的封闭实体，"
        "输出可供 gmsh 高质量网格剖分的 STEP 文件。"
        "应在 CAD 建模之后、网格剖分之前使用。"
    )
    input_kinds  = ["step", "stl"]
    output_kinds = ["step"]

    async def run(self, instruction: str, context: TaskContext):
        # Prefer STEP, fall back to STL
        geo_path = context.latest("step") or context.latest("stl")
        if geo_path is None:
            yield {"type": "text_start"}
            yield {"type": "text_delta",
                   "text": "错误：未找到上游几何文件（需要 STEP 或 STL）。"}
            yield {"type": "text_end"}
            yield {"type": "node_result", "ok": False, "kind": "error",
                   "error": "上游未产出 STEP/STL，无法执行几何清理。"}
            return

        tol = _parse_tol(instruction)
        stem = geo_path.stem
        clean_path = self.workspace / f"clean_{stem}.step"

        # ── Reasoning ──────────────────────────────────────────────────────
        yield {"type": "thinking_start"}
        yield {"type": "thinking_delta", "text": (
            f"输入几何：{geo_path.name}\n"
            f"清理公差：{tol}\n"
            f"执行管线：ShapeFix（公差/退化面修复）→ 缝合（gap ≤ {tol * 10:.3g}）"
            f"→ makeSolid（壳体提升为实体）→ 导出干净 STEP"
        )}
        yield {"type": "thinking_end"}

        yield {"type": "text_start"}
        yield {"type": "text_delta",
               "text": f"正在清理几何：{geo_path.name}…\n"}

        result = await clean_geometry(geo_path, clean_path, tol)

        if not result["success"]:
            msg = f"几何清理失败：{result['error']}"
            yield {"type": "text_delta", "text": msg}
            yield {"type": "text_end"}
            yield {"type": "node_result", "ok": False, "kind": "error",
                   "error": msg,
                   "diagnostics": {"geometry": geo_path.name, "tol": tol}}
            return

        # The worker may have changed the output path (STL → STEP conversion)
        actual_output = Path(result.get("output_path", str(clean_path)))

        before = result["before"]
        after  = result["after"]
        issues_before = result.get("issues_before", [])
        issues_after  = result.get("issues_after",  [])
        fixes         = result.get("fixes_applied",  [])

        # Emit the cleaning script to the script log
        if result.get("script"):
            yield {
                "type": "script_generated",
                "software": "FreeCAD/OCC",
                "language": "python",
                "content": result["script"],
            }

        # ── Post-reasoning ─────────────────────────────────────────────────
        yield {"type": "thinking_start"}
        yield {"type": "thinking_delta", "text": (
            f"修复前问题（严重度：{_severity(issues_before)}）：\n"
            + ("\n".join(f"  · {i}" for i in issues_before) if issues_before else "  · 无")
            + f"\n\n修复后剩余问题（严重度：{_severity(issues_after)}）：\n"
            + ("\n".join(f"  · {i}" for i in issues_after) if issues_after else "  · 无")
            + "\n\n执行的修复操作：\n"
            + ("\n".join(f"  ✓ {f}" for f in fixes) if fixes else "  · 几何已满足要求，无需修复")
        )}
        yield {"type": "thinking_end"}

        # ── Summary message ────────────────────────────────────────────────
        lines = [f"几何清理完成：{actual_output.name}\n"]
        lines.append(f"  输入：{before['solids']} 实体 / {before['faces']} 面 / {before['free_edges']} 自由边")
        lines.append(f"  输出：{after['solids']} 实体 / {after['faces']} 面 / {after['free_edges']} 自由边")
        if fixes:
            lines.append(f"\n已修复 {len(fixes)} 项问题：")
            lines.extend(f"  ✓ {f}" for f in fixes)
        if issues_after:
            lines.append(f"\n剩余注意项（{len(issues_after)} 项）：")
            lines.extend(f"  ⚠ {i}" for i in issues_after)
        else:
            lines.append("\n几何质量：可用于网格剖分 ✓")

        yield {"type": "text_delta", "text": "\n".join(lines)}
        yield {"type": "text_end"}

        # Record the clean STEP so downstream mesh agent can find it
        context.record("step", actual_output)
        context.record("clean_step", actual_output)

        yield {
            "type": "artifact_produced",
            "filename": actual_output.name,
            "kind": "step",
        }
        yield {
            "type": "node_result",
            "ok": True,
            "kind": "ok",
            "diagnostics": {
                "input":        geo_path.name,
                "output":       actual_output.name,
                "tol":          tol,
                "issues_before": issues_before,
                "issues_after":  issues_after,
                "fixes_applied": fixes,
                "before":       before,
                "after":        after,
            },
        }
