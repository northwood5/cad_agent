# -*- coding: utf-8 -*-
"""
Connection-independent run registry.

A workflow run used to live inside a single WebSocket handler's local state, so
refreshing the page (which drops the socket) cancelled the run. :class:`ActiveRun`
detaches the running task from any one connection: the task drives the workflow
generator, appends every event to an in-memory ``event_log`` and fans each event
out to all currently-subscribed connections. A reconnecting client replays the
log to rebuild the live view, then keeps receiving incremental events.

State lives in-process only; a backend restart still loses in-flight runs (out of
scope — that would require serialising agent internals).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator, Awaitable, Callable

from db import repository as repo
from services.workflow_service import WorkflowController, WorkflowService

logger = logging.getLogger(__name__)


class ActiveRun:
    """The latest workflow run for one project, shared across connections."""

    def __init__(self, project_id: int) -> None:
        self.project_id = project_id
        self.service: WorkflowService | None = None
        self.controller: WorkflowController | None = None
        self.task: asyncio.Task | None = None
        self.event_log: list[dict[str, Any]] = []
        self.subscribers: set[asyncio.Queue] = set()
        self.running: bool = False

    # ── subscription (one queue per connection) ───────────────────────────────
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)

    def _broadcast(self, ev: dict[str, Any]) -> None:
        self.event_log.append(ev)
        for q in list(self.subscribers):
            q.put_nowait(ev)

    # ── driving the workflow generator ─────────────────────────────────────────
    async def _drive(self, gen: AsyncIterator[dict[str, Any]]) -> None:
        """Consume a workflow generator, broadcast events, persist the reply."""
        self.running = True
        self._broadcast({"type": "agent_start"})
        reply_buf: list[str] = []
        try:
            async for ev in gen:
                if ev.get("type") == "text_delta":
                    reply_buf.append(ev.get("text", ""))
                self._broadcast(ev)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — surface to client, keep run alive
            logger.exception("Workflow error  project=%s", self.project_id)
            self._broadcast({"type": "error", "message": str(exc)})
        reply_text = "".join(reply_buf).strip()
        if reply_text:
            repo.add_message(self.project_id, "agent", reply_text)
        self._broadcast({"type": "agent_done"})
        self.running = False

    def start(self, gen: AsyncIterator[dict[str, Any]]) -> None:
        """Begin a fresh run: clear the log and detach a driving task."""
        self.event_log.clear()
        self.task = asyncio.create_task(self._drive(gen))

    @property
    def busy(self) -> bool:
        return self.task is not None and not self.task.done()

    def interrupt(self) -> None:
        if self.controller is not None:
            self.controller.interrupt()


class RunManager:
    """Process-wide registry of one :class:`ActiveRun` per project."""

    def __init__(self) -> None:
        self._runs: dict[int, ActiveRun] = {}

    def get_or_create(self, project_id: int) -> ActiveRun:
        run = self._runs.get(project_id)
        if run is None:
            run = ActiveRun(project_id)
            self._runs[project_id] = run
        return run

    def get(self, project_id: int) -> ActiveRun | None:
        return self._runs.get(project_id)

    def drop(self, project_id: int) -> None:
        """Interrupt and forget a project's run (e.g. new_session / delete)."""
        run = self._runs.pop(project_id, None)
        if run is not None:
            run.interrupt()

    def clear(self) -> None:
        """Interrupt and forget every run (e.g. after an LLM config change)."""
        for run in list(self._runs.values()):
            run.interrupt()
        self._runs.clear()
