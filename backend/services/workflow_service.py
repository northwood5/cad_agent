# -*- coding: utf-8 -*-
"""
WorkflowService — plan, then execute a multi-agent workflow.

:meth:`WorkflowService.execute` is an async generator yielding frontend event
dicts (the same protocol the WebSocket handler sends). It:

  1. asks the OrchestratorAgent to plan a Workflow,
  2. persists the run + nodes to SQLite,
  3. executes each node via its specialist, streaming reasoning/tool events
     (tagged with node_id + agent),
  4. detects produced artifacts (export results) → ``model_ready``,
  5. persists node/run status and emits ``workflow_*`` lifecycle events.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, AsyncIterator

from agents.base import TaskContext
from agents.orchestrator import OrchestratorAgent
from agents.cad.tools import freecad_bridge
from db import repository as repo
from .event_serializer import event_to_json
from .session_service import ProjectSession

logger = logging.getLogger(__name__)


class WorkflowService:
    def __init__(self, session: ProjectSession, llm_config: dict[str, Any]) -> None:
        self.session = session
        self.llm_config = llm_config
        self.orchestrator = OrchestratorAgent(llm_config)

    def _model_url(self, filename: str) -> str:
        return f"/api/models/{self.session.project_id}/{filename}"

    async def execute(
        self, user_request: str, scene_state: str = ""
    ) -> AsyncIterator[dict[str, Any]]:
        project_id = self.session.project_id

        # ── 1. Plan ──
        workflow = await self.orchestrator.plan(user_request, scene_state)
        run = repo.create_run(project_id, user_request, status="running")
        run_id = run["id"]

        node_db_ids: dict[str, int] = {}
        for seq, node in enumerate(workflow.nodes, 1):
            row = repo.create_node(
                run_id, node.id, node.agent, node.title,
                node.instruction, node.depends_on, seq,
            )
            node_db_ids[node.id] = row["id"]

        yield {
            "type": "workflow_plan",
            "run_id": run_id,
            "user_request": user_request,
            "nodes": [n.to_dict() for n in workflow.nodes],
        }

        # ── 2. Execute nodes in planned order ──
        context = TaskContext(project_id=project_id, run_id=run_id,
                              workspace=self.session.workspace)
        overall_ok = True

        for node in workflow.nodes:
            db_id = node_db_ids[node.id]
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

            # Capture FreeCAD scripts generated during this node for Tab 3.
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

                    payload = event_to_json(evt, call_buf, res_buf,
                                            node_id=node.id, agent=node.agent)
                    if not payload:
                        continue

                    if payload["type"] == "text_delta":
                        final_text += payload.get("text", "")

                    # Detect exported model artifacts from tool results.
                    if payload["type"] == "tool_result_end":
                        art = self._artifact_from_result(payload.get("result", ""),
                                                         run_id, db_id, project_id)
                        if art:
                            artifacts.append(art)

                    yield payload

                    # Surface a 3D-loadable model as it appears.
                    for art in artifacts:
                        if art.get("_emitted"):
                            continue
                        art["_emitted"] = True
                        yield {"type": "model_ready",
                               "filename": art["filename"],
                               "url": self._model_url(art["filename"]),
                               "node_id": node.id, "agent": node.agent}

                for s in drain_scripts():
                    yield s

            except Exception as exc:
                node_ok = False
                logger.exception("Node %s (%s) failed", node.id, node.agent)
                yield {"type": "error", "message": str(exc),
                       "node_id": node.id, "agent": node.agent}
            finally:
                freecad_bridge.reset_script_sink(sink_token)

            status = "success" if node_ok else "failed"
            summary = final_text.strip()[:500]
            repo.set_node_status(db_id, status, summary=summary, mark_finish=True)
            overall_ok = overall_ok and node_ok

            yield {"type": "workflow_node_done", "run_id": run_id,
                   "node_id": node.id, "status": status, "summary": summary,
                   "artifacts": [{"filename": a["filename"], "kind": a["kind"],
                                  "url": self._model_url(a["filename"])}
                                 for a in artifacts]}

        # ── 3. Finish ──
        final_status = "success" if overall_ok else "failed"
        repo.set_run_status(run_id, final_status)
        repo.touch_project(project_id)
        yield {"type": "workflow_done", "run_id": run_id, "status": final_status}

    def _artifact_from_result(
        self, result_text: str, run_id: int, node_db_id: int, project_id: int
    ) -> dict[str, Any] | None:
        """If a tool result reports an exported file, persist + return artifact info."""
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
