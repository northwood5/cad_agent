# -*- coding: utf-8 -*-
"""
PostSpecialist — result visualisation and report generation.

Reads CAE metrics from context.scratch["cae_metrics"] and generates:
  - displacement_plot.png  : bar chart + colour map of displacement by node
  - stress_plot.png        : Von Mises stress distribution histogram
  - report.html            : self-contained HTML report embedding the plots

Both PNG files and the HTML report are emitted as artifacts.
"""
from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import io
import logging
import math
from pathlib import Path

from ..base import SpecialistAgent, TaskContext

logger = logging.getLogger(__name__)
_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="post"
)


# ── Plot generation (blocking, runs in thread pool) ──────────────────────────

def _generate_plots(metrics: dict, workspace: Path) -> dict:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.colors as mcolors
        import numpy as np
    except ImportError:
        return {"success": False, "error": "matplotlib / numpy not available"}

    disps    = metrics.get("displacements", {})
    vm_nodes = metrics.get("vm_by_node", {})
    node_pos = metrics.get("nodes", {})
    max_disp = metrics.get("max_displacement_mm", 0)
    max_vm   = metrics.get("max_von_mises_mpa", 0)

    plot_files: list[str] = []

    # ── Displacement scatter plot coloured by magnitude ───────────────────
    if disps and node_pos:
        mags = np.array([
            math.sqrt(sum(v ** 2 for v in disps[nid][:3]))
            for nid in sorted(disps)
            if nid in node_pos
        ])
        xs = np.array([node_pos[nid][0] for nid in sorted(disps) if nid in node_pos])
        ys = np.array([node_pos[nid][1] for nid in sorted(disps) if nid in node_pos])
        zs = np.array([node_pos[nid][2] for nid in sorted(disps) if nid in node_pos])

        fig, axes = plt.subplots(1, 2, figsize=(12, 5))
        fig.patch.set_facecolor("#1a1a2e")

        for ax in axes:
            ax.set_facecolor("#16213e")
            ax.tick_params(colors="#cccccc")
            for spine in ax.spines.values():
                spine.set_edgecolor("#444466")

        # XZ scatter
        sc = axes[0].scatter(xs, zs, c=mags, cmap="plasma", s=8, alpha=0.8)
        axes[0].set_xlabel("X (mm)", color="#aaaacc")
        axes[0].set_ylabel("Z (mm)", color="#aaaacc")
        axes[0].set_title("Displacement Magnitude (XZ)", color="#eeeeee")
        cb = fig.colorbar(sc, ax=axes[0])
        cb.set_label("Displacement (mm)", color="#eeeeee")
        cb.ax.yaxis.set_tick_params(color="#cccccc")
        plt.setp(cb.ax.yaxis.get_ticklabels(), color="#cccccc")

        # Histogram
        axes[1].hist(mags, bins=30, color="#8888ff", edgecolor="#4444aa", alpha=0.85)
        axes[1].set_xlabel("Displacement (mm)", color="#aaaacc")
        axes[1].set_ylabel("Node Count", color="#aaaacc")
        axes[1].set_title("Displacement Distribution", color="#eeeeee")
        axes[1].axvline(max_disp, color="#ff6666", linewidth=1.5,
                        label=f"Max {max_disp:.4f} mm")
        axes[1].legend(facecolor="#2a2a4a", labelcolor="#eeeeee")

        plt.tight_layout()
        disp_png = workspace / "displacement_plot.png"
        plt.savefig(str(disp_png), dpi=100, bbox_inches="tight",
                    facecolor=fig.get_facecolor())
        plt.close(fig)
        plot_files.append(disp_png.name)

    # ── Von Mises stress histogram ────────────────────────────────────────
    if vm_nodes:
        vm_vals = np.array(list(vm_nodes.values()))

        fig, ax = plt.subplots(figsize=(8, 5))
        fig.patch.set_facecolor("#1a1a2e")
        ax.set_facecolor("#16213e")
        ax.tick_params(colors="#cccccc")
        for spine in ax.spines.values():
            spine.set_edgecolor("#444466")

        ax.hist(vm_vals, bins=35, color="#ffaa44", edgecolor="#cc7700", alpha=0.85)
        ax.set_xlabel("Von Mises Stress (MPa)", color="#aaaacc")
        ax.set_ylabel("Node Count", color="#aaaacc")
        ax.set_title("Von Mises Stress Distribution", color="#eeeeee")
        ax.axvline(max_vm, color="#ff4444", linewidth=1.5,
                   label=f"Max {max_vm:.2f} MPa")
        ax.legend(facecolor="#2a2a4a", labelcolor="#eeeeee")

        plt.tight_layout()
        stress_png = workspace / "stress_plot.png"
        plt.savefig(str(stress_png), dpi=100, bbox_inches="tight",
                    facecolor=fig.get_facecolor())
        plt.close(fig)
        plot_files.append(stress_png.name)

    return {"success": True, "plot_files": plot_files}


def _generate_report(
    metrics: dict, plot_files: list[str], workspace: Path
) -> str:
    """Build a self-contained HTML report with base64-embedded plots."""
    max_disp = metrics.get("max_displacement_mm", 0)
    max_vm   = metrics.get("max_von_mises_mpa", 0)
    n_nodes  = len(metrics.get("displacements", {}))

    imgs_html = ""
    for fname in plot_files:
        p = workspace / fname
        if p.exists():
            data = base64.b64encode(p.read_bytes()).decode()
            imgs_html += (
                f'<div class="plot">'
                f'<img src="data:image/png;base64,{data}" style="max-width:100%;border-radius:8px;"/>'
                f"</div>\n"
            )

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8"/>
<title>CAE 分析报告</title>
<style>
  body{{background:#1a1a2e;color:#ccc;font-family:system-ui,sans-serif;margin:24px;}}
  h1{{color:#8888ff;}} h2{{color:#aaaaee;border-bottom:1px solid #333;padding-bottom:6px;}}
  table{{border-collapse:collapse;width:100%;max-width:540px;}}
  th,td{{border:1px solid #444;padding:8px 14px;text-align:left;}}
  th{{background:#22224a;color:#aac;}} td{{color:#ddd;}}
  .plot{{margin:20px 0;}}
  .badge{{display:inline-block;padding:3px 10px;border-radius:12px;
          background:#22334a;color:#88bbff;font-size:.85em;margin:4px 2px;}}
</style>
</head>
<body>
<h1>CAE 静力分析报告</h1>
<h2>关键指标</h2>
<table>
  <tr><th>指标</th><th>数值</th></tr>
  <tr><td>参与节点数</td><td>{n_nodes}</td></tr>
  <tr><td>最大位移</td><td><strong>{max_disp:.4f} mm</strong></td></tr>
  <tr><td>最大 Von Mises 应力</td><td><strong>{max_vm:.2f} MPa</strong></td></tr>
</table>
<h2>结果云图</h2>
{imgs_html if imgs_html else '<p>（无图表）</p>'}
<p style="color:#555;font-size:.8em;margin-top:32px;">
  由 CAx Agent 自动生成 · 求解器: CalculiX 2.23
</p>
</body>
</html>"""
    report_path = workspace / "report.html"
    report_path.write_text(html, encoding="utf-8")
    return report_path.name


# ── Blocking runner ───────────────────────────────────────────────────────────

def _blocking_post(metrics: dict, workspace: Path) -> dict:
    plot_result = _generate_plots(metrics, workspace)
    plot_files  = plot_result.get("plot_files", []) if plot_result["success"] else []
    report_name = _generate_report(metrics, plot_files, workspace)
    return {
        "success": True,
        "plot_files": plot_files,
        "report": report_name,
        "plot_error": None if plot_result["success"] else plot_result.get("error"),
    }


# ── Agent ─────────────────────────────────────────────────────────────────────

class PostSpecialist(SpecialistAgent):
    name = "post"
    display_name = "后处理"
    capabilities = (
        "对 CAE 仿真结果进行后处理：生成位移云图和 Von Mises 应力分布图，"
        "提取关键指标（最大位移、最大应力），汇总生成 HTML 分析报告。"
    )
    input_kinds = ["result"]
    output_kinds = ["report"]

    async def run(self, instruction: str, context: TaskContext):
        metrics = context.scratch.get("cae_metrics")
        if metrics is None:
            yield {"type": "text_start"}
            yield {
                "type": "text_delta",
                "text": "错误：未找到 CAE 仿真结果（需要先执行 CAE 节点）。",
            }
            yield {"type": "text_end"}
            return

        yield {"type": "text_start"}
        yield {"type": "text_delta", "text": "正在生成结果图表与报告…\n"}

        loop = asyncio.get_running_loop()
        post_result = await loop.run_in_executor(
            _executor, _blocking_post, metrics, self.workspace
        )

        if not post_result["success"]:
            yield {
                "type": "text_delta",
                "text": f"后处理失败：{post_result.get('plot_error', '未知错误')}\n",
            }
            yield {"type": "text_end"}
            return

        plot_files  = post_result["plot_files"]
        report_name = post_result["report"]
        max_disp    = metrics.get("max_displacement_mm", 0)
        max_vm      = metrics.get("max_von_mises_mpa", 0)

        summary = (
            f"后处理完成：\n"
            f"  最大位移：{max_disp:.4f} mm\n"
            f"  最大 Von Mises 应力：{max_vm:.2f} MPa\n"
            f"  生成图表：{', '.join(plot_files) or '（无）'}\n"
            f"  分析报告：{report_name}\n"
        )
        yield {"type": "text_delta", "text": summary}
        yield {"type": "text_end"}

        context.record("report", self.workspace / report_name)
        context.scratch["post_result"] = post_result

        for fname in plot_files:
            yield {"type": "artifact_produced", "filename": fname, "kind": "image"}

        yield {"type": "artifact_produced", "filename": report_name, "kind": "report"}
