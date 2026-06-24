# -*- coding: utf-8 -*-
"""
OrchestratorAgent — decomposes a natural-language request into a workflow.

AgentScope 2.0.2 has no built-in pipeline/msghub primitives, so orchestration
is implemented here in a Plan-then-Execute style:

  1. ``plan()`` asks an LLM to break the request into an ordered list of nodes,
     each assigned to a registered specialist (cad / mesh / cae …). The plan is
     returned as a :class:`Workflow` so the frontend can render it immediately.
  2. The WorkflowService (see services/workflow_service.py) executes the nodes.

If planning fails or the request is trivial, we fall back to a single CAD node
so simple "make a box" requests behave exactly like the original single agent.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from agentscope.agent import Agent
from agentscope.message import UserMsg

from .llm_factory import build_model
from .registry import describe_agents, available_agent_names

logger = logging.getLogger(__name__)


# ── Workflow data structures ──────────────────────────────────────────────────

@dataclass
class WorkflowNode:
    id: str
    agent: str
    title: str
    instruction: str
    depends_on: list[str] = field(default_factory=list)
    status: str = "pending"            # pending|running|success|failed|skipped

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "agent": self.agent,
            "title": self.title,
            "instruction": self.instruction,
            "depends_on": self.depends_on,
            "status": self.status,
        }


@dataclass
class Workflow:
    run_id: str
    user_request: str
    nodes: list[WorkflowNode]

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "user_request": self.user_request,
            "nodes": [n.to_dict() for n in self.nodes],
        }


# ── Planner prompt ────────────────────────────────────────────────────────────

_PLANNER_SYSTEM = """你是一个 CAx（CAD/CAE）流程编排专家。你的职责是把用户的自然语言需求\
分解为一条有序的工作流，交给不同的专业 Agent 执行。

可用的专业 Agent 如下（只能使用这些 agent 的 name）：
{agents}

## 输出要求
严格输出 JSON，且只输出 JSON（不要解释、不要 markdown 代码块）。格式为节点数组：
[
  {{
    "id": "n1",
    "agent": "<上面某个 agent 的 name>",
    "title": "简短中文标题（≤12字）",
    "instruction": "交给该 agent 的具体子指令（自然语言，尽量明确尺寸/操作）",
    "depends_on": []
  }}
]

## 规则
1. 节点 id 形如 n1, n2, n3…，按执行顺序排列。
2. depends_on 列出该节点依赖的前置节点 id（线性流程可写前一个节点；首节点为 []）。
3. 只能使用上面列出的 agent。当前若只有 cad，则把任务拆成若干 cad 子步骤。
4. 简单需求（如"做一个立方体"）可以只有 1 个节点。
5. 复杂零件应拆成"创建主体 → 细节特征 → 导出"等清晰步骤，便于用户在流程图中观察。
6. 涉及仿真的完整流程理想顺序为：
   cad（建模/导出STEP）→ geom_clean（几何清理）→ mesh（网格）→ cae（求解）→ post（后处理/报告）
   只要工作流包含 mesh 步骤，就必须在 mesh 之前插入 geom_clean 节点。
   只需建模不需仿真时不必加 geom_clean。
7. 只能编排当前可用的 agent。
"""


def _extract_json(text: str) -> Any | None:
    """Pull the first JSON array/object out of an LLM response."""
    if not text:
        return None
    # Strip ```json fences if present.
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else text
    # Find the outermost [...] or {...}.
    for opener, closer in (("[", "]"), ("{", "}")):
        start = candidate.find(opener)
        end = candidate.rfind(closer)
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(candidate[start : end + 1])
            except json.JSONDecodeError:
                continue
    return None


# ── Orchestrator ──────────────────────────────────────────────────────────────

class OrchestratorAgent:
    """Plans a workflow from a user request using an LLM."""

    def __init__(self, llm_config: dict[str, Any]) -> None:
        # Planning is a single structured call; disable streaming.
        self.model = build_model({**llm_config, "stream": False})

    def _agents_block(self) -> str:
        lines = []
        for a in describe_agents():
            lines.append(
                f"- name: {a['name']} | {a['display_name']} | "
                f"能力: {a['capabilities']} | 产出: {', '.join(a['output_kinds']) or '—'}"
            )
        return "\n".join(lines)

    async def plan(self, user_request: str, scene_state: str = "") -> Workflow:
        """Decompose *user_request* into a Workflow (falls back to single CAD node)."""
        run_id = uuid.uuid4().hex[:12]
        valid = set(available_agent_names())

        system = _PLANNER_SYSTEM.format(agents=self._agents_block())
        agent = Agent(name="Orchestrator", system_prompt=system, model=self.model)

        user_content = user_request
        if scene_state:
            user_content = f"[当前场景: {scene_state}]\n\n{user_request}"

        try:
            msg = await agent.reply(UserMsg(name="user", content=user_content))
            text = msg.get_text_content() if hasattr(msg, "get_text_content") else str(msg.content)
            parsed = _extract_json(text)
        except Exception as exc:
            logger.warning("Orchestrator planning failed: %s", exc)
            parsed = None

        nodes = self._parse_nodes(parsed, valid)
        if not nodes:
            nodes = [self._fallback_node(user_request)]
        return Workflow(run_id=run_id, user_request=user_request, nodes=nodes)

    def _parse_nodes(self, parsed: Any, valid: set[str]) -> list[WorkflowNode]:
        if not isinstance(parsed, list):
            return []
        nodes: list[WorkflowNode] = []
        for i, item in enumerate(parsed, 1):
            if not isinstance(item, dict):
                continue
            agent = str(item.get("agent", "")).strip()
            if agent not in valid:
                # Re-route unknown/future agents to CAD if present, else skip.
                agent = "cad" if "cad" in valid else (next(iter(valid), ""))
                if not agent:
                    continue
            nodes.append(
                WorkflowNode(
                    id=str(item.get("id") or f"n{i}"),
                    agent=agent,
                    title=str(item.get("title") or f"步骤 {i}"),
                    instruction=str(item.get("instruction") or item.get("title") or ""),
                    depends_on=[str(d) for d in item.get("depends_on", []) if d],
                )
            )
        return self._merge_same_agent_nodes(nodes)

    def _merge_same_agent_nodes(self, nodes: list[WorkflowNode]) -> list[WorkflowNode]:
        """Merge consecutive nodes that use the same agent into a single sub-task.

        Tasks that call the same tool/agent are treated as one sub-task type.
        Their instructions are concatenated so the specialist receives the full context.
        """
        if not nodes:
            return nodes
        merged: list[WorkflowNode] = [nodes[0]]
        for node in nodes[1:]:
            last = merged[-1]
            if node.agent == last.agent:
                # Same agent: append this step's instruction to the existing node.
                last.instruction = last.instruction.rstrip() + "\n\n" + node.instruction
            else:
                merged.append(node)
        # Re-number ids and fix depends_on for the merged list.
        id_map: dict[str, str] = {}
        for i, node in enumerate(merged, 1):
            new_id = f"n{i}"
            id_map[node.id] = new_id
            node.id = new_id
        for node in merged:
            node.depends_on = list({id_map.get(d, d) for d in node.depends_on
                                     if id_map.get(d, d) != node.id})
        return merged

    @staticmethod
    def _fallback_node(user_request: str) -> WorkflowNode:
        return WorkflowNode(
            id="n1",
            agent="cad",
            title="CAD 建模",
            instruction=user_request,
            depends_on=[],
        )
