# -*- coding: utf-8 -*-
"""
Builds and returns an AgentScope 2.x Agent configured for CAD modeling.
Supports OpenAI, Anthropic, DashScope (Qwen), Ollama, and DeepSeek.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from agentscope.agent import Agent, ReActConfig
from agentscope.agent._config import ContextConfig
from agentscope.tool import Toolkit

from ..llm_factory import build_model, SUPPORTED_PROVIDERS  # re-exported for callers
from .tools.cad_engine import CADScene
from .tools.cad_tools import build_cad_toolkit

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are an expert CAD modeling assistant powered by a Python-based geometry engine \
(trimesh + FreeCAD).
Your role is to translate the user's natural language descriptions into precise 3-D CAD models by
calling the provided tools step by step.

## Available Tools
- **create_primitive** – Create box, cylinder, sphere, cone, or torus.
  - Supports optional `fillet_radius` (mm) for **box** and **cylinder** to produce rounded edges
    via FreeCAD. Use this for manufacturing-ready parts that need smooth edges.
- **boolean_operation** – union / difference / intersection of two shapes
- **transform_shape** – Translate, rotate, or scale an existing shape
- **extrude_polygon** – Extrude a 2-D polygon profile into a 3-D solid
- **list_shapes** – Inspect all shapes currently in the scene
- **export_model** – Export the finished model in the chosen format:
  - `stl` (default) – mesh format for 3-D preview and printing
  - `obj` – mesh format with material support
  - `step` – standard parametric CAD format via FreeCAD; use when the user needs to open the
    model in SolidWorks, Fusion 360, FreeCAD, or other professional CAD tools
- **reset_scene** – Clear the scene and start over

## Workflow
1. **Understand** – parse the user's request for geometry, dimensions, and intent.
2. **Plan** – mentally decompose the model into primitives and operations. If dimensions are
   unspecified, infer sensible defaults (e.g. 10 mm cube, M5 through-hole has radius 2.5 mm).
3. **Execute** – call tools one by one; use descriptive names like "main_body", "drill_hole", "left_arm".
4. **Verify** – call `list_shapes` if you need to inspect the scene state.
5. **Export** – always call `export_model` as the final step so the user can see the result.
   Choose `step` format when the user mentions CAD software or needs an editable file.
6. **Describe** – briefly tell the user what was built and offer next steps.

## Coordinate System
- Right-hand XYZ; Z is up.
- All dimensions in millimetres unless the user specifies otherwise.
- trimesh centres primitives at the origin by default.

## Boolean Operations Tip
When drilling a hole, the cutting tool must be larger or protrude beyond the base shape to avoid
surface artefacts. Translate the cutting shape to the correct position **before** calling
`boolean_operation` with `difference`.

## Fillet Tip
`fillet_radius` must be smaller than half the shortest edge of the shape (e.g. for a 10×10×10 box,
keep fillet_radius ≤ 4). FreeCAD will report an error if the radius is too large.

## Iteration
If the user asks to modify the existing model, call `list_shapes` first, then apply only the
necessary operations (do not reset unless explicitly asked).
"""

# ---------------------------------------------------------------------------
# Agent factory
# ---------------------------------------------------------------------------

def build_agent(
    llm_config: dict[str, Any],
    output_dir: Path,
) -> tuple[Agent, CADScene]:
    """
    Build a fresh CAD Agent + its scene for a session.

    Returns:
        agent  – ready-to-use AgentScope Agent
        scene  – the CADScene instance (for model file lookups)
    """
    model = build_model(llm_config)
    scene = CADScene(output_dir=output_dir)
    tools = build_cad_toolkit(scene)
    toolkit = Toolkit(tools=tools)

    agent = Agent(
        name="CADAgent",
        system_prompt=SYSTEM_PROMPT,
        model=model,
        toolkit=toolkit,
        react_config=ReActConfig(max_iters=200),
        context_config=ContextConfig(tool_result_limit=30000),
    )
    return agent, scene


# ---------------------------------------------------------------------------
# Specialist wrapper (used by the orchestrator / workflow service)
# ---------------------------------------------------------------------------

from agentscope.message import UserMsg          # noqa: E402

from ..base import SpecialistAgent, TaskContext  # noqa: E402


class CADSpecialist(SpecialistAgent):
    """Wraps the CAD Agent + scene so the orchestrator can drive it uniformly."""

    name = "cad"
    display_name = "CAD 设计"
    capabilities = (
        "根据自然语言创建/修改三维 CAD 几何：基本体(box/cylinder/sphere/cone/torus)、"
        "布尔运算(并/差/交)、平移旋转缩放、多边形拉伸、圆角(FreeCAD)，"
        "并可导出 STL / OBJ / STEP。适合一切实体建模与几何编辑任务。"
    )
    input_kinds = ["text"]
    output_kinds = ["step", "stl", "obj"]

    def __init__(self, llm_config: dict[str, Any], workspace: Path) -> None:
        super().__init__(llm_config, workspace)
        self.agent, self.scene = build_agent(llm_config, workspace)

    async def run(self, instruction: str, context: TaskContext):
        """Stream the CAD agent's reasoning for one workflow node."""
        async for evt in self.agent.reply_stream(
            UserMsg(name="user", content=instruction)
        ):
            yield evt
        # Record the freshest exported artifact (if any) for downstream nodes.
        for kind in ("step", "stl", "obj"):
            matches = sorted(
                self.workspace.glob(f"*.{kind}"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if matches:
                context.record(kind, matches[0])
        # Store bounding box size hint for the mesh agent's characteristic length.
        shapes = self.scene.list_shapes()
        if shapes.get("count", 0) > 0:
            import math
            all_bounds = [s["bounds"] for s in shapes["shapes"] if s.get("bounds")]
            if all_bounds:
                xs = [b[0][0] for b in all_bounds] + [b[1][0] for b in all_bounds]
                ys = [b[0][1] for b in all_bounds] + [b[1][1] for b in all_bounds]
                zs = [b[0][2] for b in all_bounds] + [b[1][2] for b in all_bounds]
                bbox_size = math.sqrt(
                    (max(xs) - min(xs)) ** 2
                    + (max(ys) - min(ys)) ** 2
                    + (max(zs) - min(zs)) ** 2
                )
                context.scratch["bbox_size"] = max(bbox_size, 1.0)
