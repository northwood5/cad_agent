# -*- coding: utf-8 -*-
"""
WorkflowService — plan, then execute a multi-agent workflow as a closed loop.

Execution is an async generator yielding frontend event dicts. It:

  1. asks the OrchestratorAgent to plan a Workflow,
  2. persists the run + nodes to SQLite,
  3. executes each node via its specialist, streaming reasoning/tool events
     (tagged with node_id + agent),
  4. detects produced artifacts (export results) → ``model_ready``,
  5. persists node/run status and emits ``workflow_*`` lifecycle events.

Self-healing loop
-----------------
Instead of stopping on the first failure, the executor consults a
:class:`~agents.review.ReviewAgent` whenever a node **hard-fails** (mesh 剖分
失败 / 求解器报错) or passes a **quality gate** (CAE solved but the numbers may
be unreasonable). The reviewer decides whether to *accept*, *retry*, *goto* an
upstream node with a corrective instruction, or *abort*. The loop then resets
the target node (and everything downstream) and re-executes from there, bounded
by a global repair-iteration budget so it always terminates.

Interruption / reset
--------------------
A :class:`WorkflowController` carries an interrupt flag. Execution checks it
before each node and after each streamed event, so the user can stop a run
mid-flight (including mid self-heal). The last planned workflow is retained so
:meth:`rerun_from` can re-execute a node (and everything downstream) on demand.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, AsyncIterator

from agents.base import NodeOutcome, TaskContext
from agents.orchestrator import OrchestratorAgent, Workflow, WorkflowNode
from agents.registry import get_specialist_cls
from agents.review import ReviewAgent
from agents.cad.tools import freecad_bridge
from db import repository as repo
from .event_serializer import event_to_json
from .session_service import ProjectSession

logger = logging.getLogger(__name__)

# Global default repair-iteration budget (overridable via llm_config).
_DEFAULT_MAX_REPAIR_ITERS = 6

# Agents that the review loop is allowed to roll back to and re-run.
_REPAIRABLE_AGENTS = {"cad", "mesh", "cae"}

# Map a script language to a sensible file extension for the script-log header.
_SCRIPT_EXT = {"python": "py", "inp": "inp", "bash": "sh", "shell": "sh"}


def _script_ext(language: str) -> str:
    return _SCRIPT_EXT.get((language or "").lower(), "txt")


class WorkflowController:
    """Carries interrupt + breakpoint signals shared between WS handler and a run."""

    def __init__(self) -> None:
        self._interrupt = asyncio.Event()
        self._breakpoints: set[str] = set()
        self._resume_events: dict[str, asyncio.Event] = {}
        self._resume_instructions: dict[str, str] = {}

    # ── interrupt ────────────────────────────────────────────────────────────
    def interrupt(self) -> None:
        self._interrupt.set()
        # Wake up any nodes waiting at a breakpoint so they can exit.
        for ev in list(self._resume_events.values()):
            ev.set()

    def reset(self) -> None:
        self._interrupt.clear()

    @property
    def interrupted(self) -> bool:
        return self._interrupt.is_set()

    # ── breakpoints ──────────────────────────────────────────────────────────
    def set_breakpoint(self, node_id: str) -> None:
        self._breakpoints.add(node_id)

    def remove_breakpoint(self, node_id: str) -> None:
        self._breakpoints.discard(node_id)

    def has_breakpoint(self, node_id: str) -> bool:
        return node_id in self._breakpoints

    async def wait_at_breakpoint(self, node_id: str) -> str | None:
        """Block until resume() is called. Returns override instruction or None."""
        event = asyncio.Event()
        self._resume_events[node_id] = event
        await event.wait()
        del self._resume_events[node_id]
        return self._resume_instructions.pop(node_id, None)

    def resume(self, node_id: str, instruction: str | None = None) -> None:
        """Resume a node that is paused at a breakpoint, optionally with new instruction."""
        if instruction is not None:
            self._resume_instructions[node_id] = instruction
        ev = self._resume_events.pop(node_id, None)
        if ev:
            ev.set()


class WorkflowService:
    def __init__(self, session: ProjectSession, llm_config: dict[str, Any]) -> None:
        self.session = session
        self.llm_config = llm_config
        self.orchestrator = OrchestratorAgent(llm_config)
        self.reviewer = ReviewAgent(llm_config)
        self._max_repair_iters = int(
            llm_config.get("max_repair_iterations", _DEFAULT_MAX_REPAIR_ITERS)
        )
        # Retained after planning so reset/rerun can target individual nodes.
        self._workflow: Workflow | None = None
        self._run_id: int | None = None
        self._node_db_ids: dict[str, int] = {}
        self._user_request: str = ""
        self._scene_state: str = ""

    @property
    def has_workflow(self) -> bool:
        return self._workflow is not None

    def _model_url(self, filename: str) -> str:
        return f"/api/models/{self.session.project_id}/{filename}"

    # ── Planning + full run ──────────────────────────────────────────────────

    async def execute(
        self,
        user_request: str,
        scene_state: str = "",
        controller: WorkflowController | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        controller = controller or WorkflowController()
        project_id = self.session.project_id

        workflow = await self.orchestrator.plan(user_request, scene_state)
        run = repo.create_run(project_id, user_request, status="running")
        self._workflow = workflow
        self._run_id = run["id"]
        self._user_request = user_request
        self._scene_state = scene_state
        self._node_db_ids = {}
        for seq, node in enumerate(workflow.nodes, 1):
            row = repo.create_node(
                run["id"], node.id, node.agent, node.title,
                node.instruction, node.depends_on, seq,
            )
            self._node_db_ids[node.id] = row["id"]

        yield {
            "type": "workflow_plan",
            "run_id": run["id"],
            "user_request": user_request,
            "nodes": [n.to_dict() for n in workflow.nodes],
        }

        async for ev in self._run_nodes(workflow.nodes, controller):
            yield ev

    # ── Reset / rerun from a node ────────────────────────────────────────────

    async def rerun_from(
        self, node_id: str, controller: WorkflowController | None = None,
        override_instruction: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        controller = controller or WorkflowController()
        if self._workflow is None or self._run_id is None:
            yield {"type": "error", "message": "没有可重置的工作流"}
            return

        nodes = self._workflow.nodes
        idx = next((i for i, n in enumerate(nodes) if n.id == node_id), None)
        if idx is None:
            yield {"type": "error", "message": f"节点 {node_id} 不存在"}
            return

        # Apply override instruction to the target node only.
        if override_instruction:
            nodes[idx].instruction = override_instruction

        for n in nodes[idx:]:
            n.status = "pending"
            repo.set_node_status(self._node_db_ids[n.id], "pending")
            yield {"type": "workflow_node_reset", "run_id": self._run_id, "node_id": n.id}

        repo.set_run_status(self._run_id, "running")
        async for ev in self._run_nodes(nodes, controller, start_index=idx):
            yield ev

    # ── Node execution loop (closed-loop with review-driven backtracking) ─────

    async def _run_nodes(
        self, nodes: list[WorkflowNode], controller: WorkflowController,
        *, start_index: int = 0,
    ) -> AsyncIterator[dict[str, Any]]:
        run_id = self._run_id
        project_id = self.session.project_id
        context = TaskContext(project_id=project_id, run_id=run_id,
                              workspace=self.session.workspace)
        overall_ok = True
        interrupted = False
        aborted = False
        history: list[dict[str, Any]] = []

        i = start_index
        iterations = 0
        max_iters = self._max_repair_iters

        while i < len(nodes):
            node = nodes[i]
            db_id = self._node_db_ids[node.id]

            # Stop before starting another node if the user interrupted.
            if controller.interrupted:
                interrupted = True
                break

            # Pause at breakpoint — wait for resume (optionally with new instruction).
            if controller.has_breakpoint(node.id):
                yield {"type": "workflow_node_paused", "run_id": run_id,
                       "node_id": node.id, "agent": node.agent, "title": node.title}
                override = await controller.wait_at_breakpoint(node.id)
                if controller.interrupted:
                    interrupted = True
                    break
                if override:
                    node.instruction = override
                    yield {"type": "workflow_node_instruction_updated",
                           "run_id": run_id, "node_id": node.id,
                           "instruction": node.instruction}

            # ── Run the node ──────────────────────────────────────────────────
            holder: dict[str, Any] = {}
            async for ev in self._run_single_node(node, context, controller, holder):
                yield ev

            if holder.get("skipped"):
                repo.set_node_status(db_id, "skipped", summary=holder.get("summary"),
                                     mark_finish=True)
                yield {"type": "workflow_node_done", "run_id": run_id,
                       "node_id": node.id, "status": "skipped",
                       "summary": holder.get("summary"), "artifacts": []}
                i += 1
                continue

            outcome: NodeOutcome = holder["outcome"]
            artifacts = holder["artifacts"]

            if holder.get("interrupted"):
                interrupted = True
                repo.set_node_status(db_id, "interrupted",
                                     summary="用户中断", mark_finish=True)
                yield {"type": "workflow_node_done", "run_id": run_id,
                       "node_id": node.id, "status": "interrupted",
                       "summary": "用户中断", "artifacts": []}
                break

            status = "success" if outcome.ok else "failed"
            summary = (outcome.error or holder.get("summary") or "").strip()[:500]
            repo.set_node_status(db_id, status, summary=summary, mark_finish=True)

            yield {"type": "workflow_node_done", "run_id": run_id,
                   "node_id": node.id, "status": status, "summary": summary,
                   "artifacts": [{"filename": a["filename"], "kind": a["kind"],
                                  "url": self._model_url(a["filename"])}
                                 for a in artifacts]}

            # ── Decide whether this node needs a review ───────────────────────
            needs_review = (not outcome.ok) or self._is_quality_gate(node.agent)
            if not needs_review:
                i += 1
                continue

            # ── Consult the review (复盘) agent ───────────────────────────────
            decision = None
            upstream_targets = [
                {"id": nodes[j].id, "agent": nodes[j].agent, "title": nodes[j].title}
                for j in range(0, i + 1) if nodes[j].agent in _REPAIRABLE_AGENTS
            ]
            async for ev in self.reviewer.review(
                user_request=self._user_request,
                node=node.to_dict(),
                outcome={"ok": outcome.ok, "kind": outcome.kind,
                         "error": outcome.error, "diagnostics": outcome.diagnostics},
                upstream_targets=upstream_targets,
                history=history,
                scene_state=self._scene_state,
            ):
                if ev.get("type") == "review_decision":
                    decision = ev["decision"]
                else:
                    yield ev

            if decision is None:
                decision = {"action": "retry", "target_agent": node.agent,
                            "instruction": "", "reason": "复盘未返回决策，默认重试。"}

            action = decision.get("action", "retry")

            if action == "accept":
                i += 1
                continue

            if action == "abort":
                aborted = True
                overall_ok = False
                yield {"type": "error", "node_id": node.id, "agent": node.agent,
                       "message": f"复盘判定无法自动修复，终止：{decision.get('reason', '')}"}
                break

            # retry | goto → consume one repair iteration
            iterations += 1
            if iterations > max_iters:
                overall_ok = False
                yield {"type": "error", "node_id": node.id, "agent": node.agent,
                       "message": (f"已达自愈迭代上限（{max_iters} 次），停止。"
                                   f"最后的问题：{outcome.error or decision.get('reason', '')}")}
                break

            target_idx = self._resolve_target(nodes, i, decision)
            target_node = nodes[target_idx]
            target_agent = target_node.agent
            instruction = (decision.get("instruction") or "").strip()
            reason = decision.get("reason", "")

            if instruction:
                target_node.instruction = instruction

            # Reset target node + everything downstream to pending.
            for j in range(target_idx, i + 1):
                nodes[j].status = "pending"
                repo.set_node_status(self._node_db_ids[nodes[j].id], "pending")
                yield {"type": "workflow_node_reset", "run_id": run_id,
                       "node_id": nodes[j].id}

            repo.add_loopback(run_id, iterations, node.id, target_node.id,
                              target_agent, reason, instruction)
            history.append({"iteration": iterations, "from_node": node.id,
                            "to_node": target_node.id, "target_agent": target_agent,
                            "instruction": instruction})

            yield {"type": "workflow_loopback", "run_id": run_id,
                   "from_node": node.id, "to_node": target_node.id,
                   "target_agent": target_agent, "reason": reason,
                   "instruction": instruction, "iteration": iterations}

            if instruction:
                yield {"type": "workflow_node_instruction_updated", "run_id": run_id,
                       "node_id": target_node.id, "instruction": instruction}

            i = target_idx  # jump back

        final_status = ("interrupted" if interrupted
                        else "success" if (overall_ok and not aborted)
                        else "failed")
        repo.set_run_status(run_id, final_status)
        repo.touch_project(project_id)
        yield {"type": "workflow_done", "run_id": run_id, "status": final_status}

    @staticmethod
    def _is_quality_gate(agent: str) -> bool:
        """Whether a successful node of this agent still needs an LLM quality review."""
        cls = get_specialist_cls(agent)
        return bool(cls is not None and getattr(cls, "quality_gate", False))

    @staticmethod
    def _resolve_target(
        nodes: list[WorkflowNode], current_idx: int, decision: dict[str, Any]
    ) -> int:
        """Resolve the node index to jump back to. Always <= current_idx."""
        action = decision.get("action", "retry")
        target_agent = (decision.get("target_agent")
                        or nodes[current_idx].agent)
        if action == "retry" and nodes[current_idx].agent == target_agent:
            return current_idx
        # Search backwards (including current) for the nearest node of that agent.
        for j in range(current_idx, -1, -1):
            if nodes[j].agent == target_agent:
                return j
        return current_idx  # fall back to retrying the current node

    # ── Single-node execution (streams events, fills *holder*) ────────────────

    async def _run_single_node(
        self, node: WorkflowNode, context: TaskContext,
        controller: WorkflowController, holder: dict[str, Any],
    ) -> AsyncIterator[dict[str, Any]]:
        """Run one node's specialist, forwarding its events. Populates *holder*
        with ``outcome`` (NodeOutcome), ``artifacts``, ``interrupted``,
        ``summary`` and optionally ``skipped``."""
        run_id = self._run_id
        project_id = self.session.project_id
        db_id = self._node_db_ids[node.id]

        yield {"type": "workflow_node_start", "run_id": run_id,
               "node_id": node.id, "agent": node.agent, "title": node.title}

        specialist = self.session.get_specialist(node.agent)
        if specialist is None:
            holder["skipped"] = True
            holder["summary"] = f"agent '{node.agent}' 暂不可用"
            holder["outcome"] = NodeOutcome(ok=True, kind="ok")
            holder["artifacts"] = []
            holder["interrupted"] = False
            return

        repo.set_node_status(db_id, "running", mark_start=True)

        call_buf: dict[str, str] = {}
        res_buf: dict[str, str] = {}
        final_text = ""
        artifacts: list[dict[str, Any]] = []
        node_interrupted = False
        exc_error = ""
        node_result_evt: dict[str, Any] | None = None

        pending_scripts: list[str] = []
        sink_token = freecad_bridge.set_script_sink(pending_scripts.append)

        def drain_scripts():
            events = []
            while pending_scripts:
                content = pending_scripts.pop(0)
                row = repo.add_script(project_id, node.agent, "freecad",
                                      "python", content,
                                      run_id=run_id, node_id=db_id)
                events.append({"type": "script_generated",
                               "node_id": node.id, "agent": node.agent,
                               "software": "freecad", "language": "python",
                               "filename": f"freecad_{row['id']}.py",
                               "content": content})
            return events

        try:
            async for evt in specialist.run(node.instruction, context):
                for s in drain_scripts():
                    yield s

                if isinstance(evt, dict):
                    payload = dict(evt)
                    payload.setdefault("node_id", node.id)
                    payload.setdefault("agent", node.agent)
                else:
                    payload = event_to_json(evt, call_buf, res_buf,
                                            node_id=node.id, agent=node.agent)
                if payload:
                    ptype = payload["type"]
                    if ptype == "node_result":
                        # Structured outcome — capture, don't forward raw.
                        node_result_evt = payload
                        if controller.interrupted:
                            node_interrupted = True
                            break
                        continue
                    if ptype == "text_delta":
                        final_text += payload.get("text", "")
                    if ptype == "script_generated" and not payload.get("filename"):
                        # Script emitted by a non-LLM agent (mesh/cae/post).
                        software = payload.get("software") or node.agent
                        language = payload.get("language") or "text"
                        content = payload.get("content", "")
                        row = repo.add_script(project_id, node.agent, software,
                                              language, content,
                                              run_id=run_id, node_id=db_id)
                        payload["filename"] = f"{software}_{row['id']}.{_script_ext(language)}"
                    if ptype == "tool_result_end":
                        art = self._artifact_from_result(
                            payload.get("result", ""), run_id, db_id,
                            project_id, agent_name=node.agent,
                        )
                        if art:
                            artifacts.append(art)
                    if ptype == "artifact_produced":
                        filename = payload.get("filename")
                        kind = payload.get("kind", "file")
                        if filename:
                            rel_path = f"{node.agent}/{filename}"
                            abs_path = str(self.session.workspace / node.agent / filename)
                            repo.add_artifact(project_id, kind, rel_path, abs_path,
                                              run_id=run_id, node_id=db_id)
                            artifacts.append({"filename": rel_path, "kind": kind,
                                              "_emitted": False})
                        # Don't forward raw artifact_produced to client
                    else:
                        yield payload
                    for art in artifacts:
                        if art.get("_emitted"):
                            continue
                        art["_emitted"] = True
                        yield {"type": "model_ready",
                               "filename": art["filename"],
                               "url": self._model_url(art["filename"]),
                               "node_id": node.id, "agent": node.agent}

                # Honour an interrupt requested while this node streams.
                if controller.interrupted:
                    node_interrupted = True
                    break

            for s in drain_scripts():
                yield s

        except Exception as exc:
            exc_error = str(exc)
            logger.exception("Node %s (%s) failed", node.id, node.agent)
            yield {"type": "error", "message": exc_error,
                   "node_id": node.id, "agent": node.agent}
        finally:
            freecad_bridge.reset_script_sink(sink_token)

        # ── Build the structured outcome ──────────────────────────────────────
        if node_result_evt is not None:
            outcome = NodeOutcome.from_event(node_result_evt)
        elif exc_error:
            outcome = NodeOutcome(ok=False, kind="error", error=exc_error)
        else:
            # No explicit signal and no exception → treat as success (e.g. CAD
            # ReAct agents that don't emit node_result).
            outcome = NodeOutcome(ok=True, kind="ok")

        holder["outcome"] = outcome
        holder["artifacts"] = artifacts
        holder["interrupted"] = node_interrupted
        holder["summary"] = final_text.strip()[:500]

    def _artifact_from_result(
        self, result_text: str, run_id: int, node_db_id: int, project_id: int,
        agent_name: str = "",
    ) -> dict[str, Any] | None:
        try:
            parsed = json.loads(result_text)
        except (json.JSONDecodeError, TypeError):
            return None
        if not (isinstance(parsed, dict) and parsed.get("success") and parsed.get("filename")):
            return None
        filename = parsed["filename"]
        kind = parsed.get("format") or Path(filename).suffix.lstrip(".") or "stl"
        # Store file under agent sub-directory when available.
        rel_path = f"{agent_name}/{filename}" if agent_name else filename
        abs_path = str(self.session.workspace / agent_name / filename) if agent_name \
                   else str(self.session.workspace / filename)
        repo.add_artifact(project_id, kind, rel_path, abs_path,
                          run_id=run_id, node_id=node_db_id)
        return {"filename": rel_path, "kind": kind, "_emitted": False}
