# -*- coding: utf-8 -*-
"""
ReviewAgent — 复盘智能体.

Consulted by the WorkflowService whenever a node either

  * **hard-fails** (mesh 剖分失败 / CalculiX 求解报错), or
  * passes a **quality gate** (CAE solved, but the metrics may be physically
    unreasonable),

and decides what the closed-loop workflow should do next:

  accept | retry | goto(<upstream agent> + 修正指令) | abort

It reuses the same single-shot structured-LLM pattern as
:class:`agents.orchestrator.OrchestratorAgent` (``build_model`` + JSON parse),
and streams its reasoning to the frontend as ``thinking_*`` events tagged
``agent="review"`` so the user can watch the self-healing decision unfold.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from agentscope.agent import Agent
from agentscope.message import UserMsg

from ..llm_factory import build_model

logger = logging.getLogger(__name__)


_VALID_ACTIONS = {"accept", "retry", "goto", "abort"}


@dataclass
class ReviewDecision:
    action: str = "retry"                       # accept | retry | goto | abort
    target_agent: str = ""                       # for goto/retry: cad | mesh | cae
    instruction: str = ""                        # corrective sub-instruction
    reason: str = ""                             # human-readable rationale

    @classmethod
    def safe_retry(cls, target_agent: str, reason: str) -> "ReviewDecision":
        """Fallback when the LLM output can't be parsed — retry the node once."""
        return cls(action="retry", target_agent=target_agent,
                   instruction="", reason=reason)


_SYSTEM = """你是一个 CAx（CAD/CAE）流程的复盘与纠错专家。一个多步工作流（cad → mesh → cae → post）\
中的某个节点失败、或求解结果不合理，需要你判断如何回退修正，让流程自愈而不是中止。

你只能做出以下四种决策之一：
- "accept"：结果其实可以接受，继续往下走（仅在 quality 评审且指标合理时使用）。
- "retry"：原节点重试（通常配合更明确的指令，如调整网格密度）。
- "goto"：回退到某个**上游**节点重做，并给出具体修正指令。
- "abort"：问题无法靠重试/回退解决（如几何根本错误且已多次失败），终止。

## 领域知识
- 网格剖分失败（gmsh）常见原因：几何自交/非流形、特征过小导致 lc 过大无法剖分、薄壁、布尔运算残留。
  → 优先 goto mesh 调整网格密度（如"使用更细网格 lc=2"）；若反复失败或日志指向几何缺陷，则 goto cad 修形（简化特征/加圆角/修复布尔）。
- CAE 求解失败（CalculiX）常见原因：缺少固定/加载边界、单元退化、材料/载荷异常。
  → 缺边界多为网格标记问题 → goto mesh；若几何本身无法施加约束 → goto cad。
- CAE 结果不合理判据：
  · 最大位移与几何尺寸（bounding_box）同量级甚至更大 → 多半网格过粗或边界/载荷设置不当。
  · 最大 Von Mises 远超材料屈服强度（钢≈250MPa, 铝≈270MPa 量级）多个数量级 → 应力奇异/网格过粗。
  · 位移/应力为 0 或 NaN → 边界/载荷未生效。
  合理则 accept；不合理优先 goto mesh 加密网格，必要时 goto cad。

## 输出要求
严格输出 JSON，且只输出 JSON（不要解释、不要 markdown 代码块）：
{
  "action": "accept" | "retry" | "goto" | "abort",
  "target_agent": "cad" | "mesh" | "cae",
  "instruction": "给目标节点的具体修正子指令（自然语言，明确改什么；accept/abort 可为空）",
  "reason": "简短中文说明你的判断依据"
}

## 规则
1. target_agent 只能取「可回退目标」列表里的值。
2. goto 的 instruction 必须具体可执行（例如"将网格特征长度改为 lc=2 以获得更细网格""把圆角半径从 0 增大到 2mm 避免应力奇异"），并尽量结合上一次的失败原因。
3. 不要无意义地重复完全相同的指令；若历史里已尝试过同样修正且仍失败，请换一种策略或升级到更上游（mesh→cad）或 abort。
"""


class ReviewAgent:
    """Plans a self-healing decision from a node failure / quality gate."""

    def __init__(self, llm_config: dict[str, Any]) -> None:
        # Single structured call — disable streaming for clean JSON.
        self.model = build_model({**llm_config, "stream": False})

    @staticmethod
    def _extract_json(text: str) -> Any | None:
        if not text:
            return None
        fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
        candidate = fenced.group(1) if fenced else text
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(candidate[start : end + 1])
            except json.JSONDecodeError:
                return None
        return None

    def _build_user_prompt(
        self, *, user_request: str, node: dict[str, Any], outcome: dict[str, Any],
        upstream_targets: list[dict[str, str]], history: list[dict[str, Any]],
        scene_state: str,
    ) -> str:
        targets = ", ".join(
            f"{t['agent']}(节点{t['id']}:{t['title']})" for t in upstream_targets
        ) or "（无更上游节点，只能 retry 当前节点或 abort）"
        hist = "\n".join(
            f"  - 第{h['iteration']}轮: {h['from_node']}→{h['to_node']} "
            f"({h['target_agent']}) 指令：{h['instruction']}"
            for h in history
        ) or "  （无）"
        gate = "质量评审（求解成功，判断结果是否合理）" if outcome.get("kind") == "quality" else "硬失败处理"
        diag = json.dumps(outcome.get("diagnostics", {}), ensure_ascii=False, default=str)[:2000]
        scene = f"\n当前 CAD 场景: {scene_state}" if scene_state else ""
        return (
            f"## 用户原始需求\n{user_request}{scene}\n\n"
            f"## 触发复盘的场景\n{gate}\n\n"
            f"## 当前节点\n"
            f"  角色(agent): {node['agent']}\n"
            f"  标题: {node['title']}\n"
            f"  指令: {node['instruction']}\n\n"
            f"## 结果\n"
            f"  ok: {outcome.get('ok')}  kind: {outcome.get('kind')}\n"
            f"  error: {outcome.get('error') or '（无）'}\n"
            f"  diagnostics: {diag}\n\n"
            f"## 可回退目标（target_agent 只能取这些 agent 之一，或 retry 当前节点 {node['agent']}）\n"
            f"  {targets}\n\n"
            f"## 历史回退记录（避免重复无效修正）\n{hist}\n\n"
            f"请输出你的决策 JSON。"
        )

    async def review(
        self, *,
        user_request: str,
        node: dict[str, Any],
        outcome: dict[str, Any],
        upstream_targets: list[dict[str, str]],
        history: list[dict[str, Any]],
        scene_state: str = "",
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield ``thinking_*`` events for the UI, then a final internal
        ``{"type": "review_decision", "decision": {...}}`` event the workflow
        consumes to drive control flow."""
        valid_targets = {t["agent"] for t in upstream_targets} | {node["agent"]}

        system = _SYSTEM
        user = self._build_user_prompt(
            user_request=user_request, node=node, outcome=outcome,
            upstream_targets=upstream_targets, history=history, scene_state=scene_state,
        )

        decision: ReviewDecision
        try:
            agent = Agent(name="Reviewer", system_prompt=system, model=self.model)
            msg = await agent.reply(UserMsg(name="user", content=user))
            text = msg.get_text_content() if hasattr(msg, "get_text_content") else str(msg.content)
            parsed = self._extract_json(text)
            decision = self._coerce(parsed, valid_targets, node["agent"])
        except Exception as exc:
            logger.warning("ReviewAgent failed: %s", exc)
            decision = ReviewDecision.safe_retry(
                node["agent"], reason=f"复盘 LLM 调用异常，降级为原地重试一次：{exc}")

        # Stream the rationale so the user sees the self-healing reasoning.
        yield {"type": "thinking_start", "agent": "review", "node_id": node["id"]}
        yield {"type": "thinking_delta", "agent": "review", "node_id": node["id"],
               "text": (f"【复盘决策】{decision.action}"
                        + (f" → {decision.target_agent}" if decision.action in ("goto", "retry") else "")
                        + f"\n依据：{decision.reason}"
                        + (f"\n修正指令：{decision.instruction}" if decision.instruction else ""))}
        yield {"type": "thinking_end", "agent": "review", "node_id": node["id"]}

        yield {"type": "review_decision", "decision": {
            "action": decision.action,
            "target_agent": decision.target_agent,
            "instruction": decision.instruction,
            "reason": decision.reason,
        }}

    def _coerce(
        self, parsed: Any, valid_targets: set[str], current_agent: str
    ) -> ReviewDecision:
        if not isinstance(parsed, dict):
            return ReviewDecision.safe_retry(
                current_agent, reason="复盘输出无法解析为 JSON，降级为原地重试一次。")
        action = str(parsed.get("action", "")).strip().lower()
        if action not in _VALID_ACTIONS:
            action = "retry"
        target = str(parsed.get("target_agent", "")).strip().lower()
        instruction = str(parsed.get("instruction", "")).strip()
        reason = str(parsed.get("reason", "")).strip() or "（无说明）"

        if action in ("goto", "retry"):
            if target not in valid_targets:
                # Invalid/empty target → safest is retrying the current node.
                target = current_agent
                if action == "goto":
                    action = "retry"
        else:
            target = ""
        return ReviewDecision(action=action, target_agent=target,
                              instruction=instruction, reason=reason)
