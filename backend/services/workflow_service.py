# -*- coding: utf-8 -*-
"""
WorkflowService — plan, then execute a multi-agent workflow.

Execution is an async generator yielding frontend event dicts. It:

  1. asks the OrchestratorAgent to plan a Workflow,
  2. persists the run + nodes to SQLite,
  3. executes each node via its specialist, streaming reasoning/tool events
     (tagged with node_id + agent),
  4. detects produced artifacts (export results) → ``model_ready``,
  5. persists node/run status and emits ``workflow_*`` lifecycle events.

Interruption / reset
--------------------
A :class:`WorkflowController` carries an interrupt flag. Execution checks it
before each node and after each streamed event, so the user can stop a run
mid-flight. The last planned workflow is retained so :meth:`rerun_from` can
re-execute a node (and everything downstream) on demand.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, AsyncIterator

from agents.base import TaskContext
from agents.orchestrator import OrchestratorAgent, Workflow, WorkflowNode
from agents.cad.tools import freecad_bridge
from db import repository as repo
from .event_serializer import event_to_json
from .session_service import ProjectSession

logger = logging.getLogger(__name__)


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
        # Retained after planning so reset/rerun can target individual nodes.
        self._workflow: Workflow | None = None
        self._run_id: int | None = None
        self._node_db_ids: dict[str, int] = {}

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

        subset = nodes[idx:]
        # Apply override instruction to the target node only.
        if override_instruction:
            subset[0].instruction = override_instruction

        for n in subset:
            n.status = "pending"
            repo.set_node_status(self._node_db_ids[n.id], "pending")
            yield {"type": "workflow_node_reset", "run_id": self._run_id, "node_id": n.id}

        repo.set_run_status(self._run_id, "running")
        async for ev in self._run_nodes(subset, controller):
            yield ev

    # ── Node execution loop ──────────────────────────────────────────────────

    async def _run_nodes(
        self, nodes: list[WorkflowNode], controller: WorkflowController
    ) -> AsyncIterator[dict[str, Any]]:
        project_id = self.session.project_id
        run_id = self._run_id
        context = TaskContext(project_id=project_id, run_id=run_id,
                              workspace=self.session.workspace)
        overall_ok = True
        interrupted = False

        for node in nodes:
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

            specialist = self.session.get_specialist(node.agent)
            yield {"type": "workflow_node_start", "run_id": run_id,
                   "node_id": node.id, "agent": node.agent, "title": node.title}

            if specialist is None:
                repo.set_node_status(db_id, "skipped",
                                     summary=f"agent '{node.agent}' 暂不可用",
                                     mark_start=True, mark_finish=True)
                yield {"type": "workflow_node_done", "run_id": run_id,
                       "node_id": node.id, "status": "skipped",
                       "summary": f"agent '{node.agent}' 暂不可用", "artifacts": []}
                continue

            repo.set_node_status(db_id, "running", mark_start=True)

            call_buf: dict[str, str] = {}
            res_buf: dict[str, str] = {}
            final_text = ""
            artifacts: list[dict[str, Any]] = []
            node_ok = True
            node_interrupted = False

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
                        if ptype == "text_delta":
                            final_text += payload.get("text", "")
                        if ptype == "tool_result_end":
                            art = self._artifact_from_result(payload.get("result", ""),
                                                             run_id, db_id, project_id)
                            if art:
                                artifacts.append(art)
                        if ptype == "artifact_produced":
                            # Emitted by non-LLM agents (mesh/cae/post)
                            filename = payload.get("filename")
                            kind = payload.get("kind", "file")
                            if filename:
                                path = str(self.session.workspace / filename)
                                repo.add_artifact(project_id, kind, filename, path,
                                                  run_id=run_id, node_id=db_id)
                                artifacts.append({"filename": filename, "kind": kind,
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
                node_ok = False
                logger.exception("Node %s (%s) failed", node.id, node.agent)
                yield {"type": "error", "message": str(exc),
                       "node_id": node.id, "agent": node.agent}
            finally:
                freecad_bridge.reset_script_sink(sink_token)

            if node_interrupted:
                interrupted = True
                repo.set_node_status(db_id, "interrupted",
                                     summary="用户中断", mark_finish=True)
                yield {"type": "workflow_node_done", "run_id": run_id,
                       "node_id": node.id, "status": "interrupted",
                       "summary": "用户中断", "artifacts": []}
                break

            status = "success" if node_ok else "failed"
            summary = final_text.strip()[:500]
            repo.set_node_status(db_id, status, summary=summary, mark_finish=True)
            overall_ok = overall_ok and node_ok

            yield {"type": "workflow_node_done", "run_id": run_id,
                   "node_id": node.id, "status": status, "summary": summary,
                   "artifacts": [{"filename": a["filename"], "kind": a["kind"],
                                  "url": self._model_url(a["filename"])}
                                 for a in artifacts]}

            if not node_ok:
                break  # stop the pipeline on failure

        final_status = ("interrupted" if interrupted
                        else "success" if overall_ok else "failed")
        repo.set_run_status(run_id, final_status)
        repo.touch_project(project_id)
        yield {"type": "workflow_done", "run_id": run_id, "status": final_status}

    def _artifact_from_result(
        self, result_text: str, run_id: int, node_db_id: int, project_id: int
    ) -> dict[str, Any] | None:
        try:
            parsed = json.loads(result_text)
        except (json.JSONDecodeError, TypeError):
            return None
        if not (isinstance(parsed, dict) and parsed.get("success") and parsed.get("filename")):
            return None
        filename = parsed["filename"]
        kind = parsed.get("format") or Path(filename).suffix.lstrip(".") or "stl"
        path = str(self.session.workspace / filename)
        repo.add_artifact(project_id, kind, filename, path,
                          run_id=run_id, node_id=node_db_id)
        return {"filename": filename, "kind": kind, "_emitted": False}
