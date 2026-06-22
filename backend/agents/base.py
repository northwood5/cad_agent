# -*- coding: utf-8 -*-
"""
SpecialistAgent — the common abstraction every domain agent implements.

The orchestrator inspects each specialist's declarative metadata
(``capabilities``, ``input_kinds``, ``output_kinds``) when planning a
workflow, then drives execution through :meth:`SpecialistAgent.run`, which
yields events (AgentScope events plus the platform's own workflow/script
events) so the WebSocket layer can stream progress to the browser.

A :class:`TaskContext` is threaded through every node of a workflow so a
downstream agent can consume the artifacts an upstream agent produced
(e.g. CAD emits a STEP file → mesh agent reads it).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator


@dataclass
class TaskContext:
    """Shared state passed between workflow nodes."""

    project_id: int
    run_id: int | None
    workspace: Path                       # output/{user_id}/{project_id}
    # Artifacts produced so far, keyed by kind → most recent path.
    # e.g. {"step": Path(...), "stl": Path(...), "mesh": Path(...)}
    artifacts: dict[str, Path] = field(default_factory=dict)
    # Free-form scratch space for agents to share structured results.
    scratch: dict[str, Any] = field(default_factory=dict)

    def latest(self, kind: str) -> Path | None:
        return self.artifacts.get(kind)

    def record(self, kind: str, path: Path) -> None:
        self.artifacts[kind] = path


@dataclass
class NodeOutcome:
    """Structured result of running one workflow node.

    Specialists signal this by yielding a ``{"type": "node_result", ...}`` event
    at the end of :meth:`SpecialistAgent.run`. The WorkflowService captures it to
    decide whether to advance, retry, or loop back to an upstream node — instead
    of relying on "no exception raised == success" (which silently passed failed
    mesh/CAE nodes before).

    kind:
      "ok"      — node succeeded
      "error"   — hard failure (mesh生成失败 / 求解器报错)
      "quality" — completed but results need a quality review (e.g. CAE metrics)
    """

    ok: bool = True
    kind: str = "ok"                       # ok | error | quality
    error: str = ""
    diagnostics: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_event(cls, evt: dict[str, Any]) -> "NodeOutcome":
        return cls(
            ok=bool(evt.get("ok", True)),
            kind=str(evt.get("kind") or ("ok" if evt.get("ok", True) else "error")),
            error=str(evt.get("error") or ""),
            diagnostics=evt.get("diagnostics") or {},
        )


class SpecialistAgent(ABC):
    """Base class for CAD / mesh / CAE (and future) domain agents."""

    #: stable identifier used in the registry and workflow nodes
    name: str = "base"
    #: human-friendly label shown in the UI
    display_name: str = "Base Agent"
    #: natural-language capability description the orchestrator reads when planning
    capabilities: str = ""
    #: artifact kinds this agent can consume as input ("text" means free instruction)
    input_kinds: list[str] = ["text"]
    #: artifact kinds this agent produces
    output_kinds: list[str] = []
    #: when True, a successful run still triggers an LLM quality review (e.g. CAE
    #: results may solve fine yet be physically unreasonable → loop back upstream)
    quality_gate: bool = False

    def __init__(self, llm_config: dict[str, Any], workspace: Path) -> None:
        self.llm_config = llm_config
        self.workspace = workspace
        self.workspace.mkdir(parents=True, exist_ok=True)

    @abstractmethod
    async def run(
        self, instruction: str, context: TaskContext
    ) -> AsyncIterator[Any]:
        """Execute *instruction* for one workflow node.

        Yields events (AgentScope events and/or platform dict events). The
        method should update *context.artifacts* with anything it produces so
        downstream nodes can pick them up.
        """
        raise NotImplementedError
        yield  # pragma: no cover  (marks this as an async generator)

    @classmethod
    def describe(cls) -> dict[str, Any]:
        """Metadata block the orchestrator uses when choosing agents."""
        return {
            "name": cls.name,
            "display_name": cls.display_name,
            "capabilities": cls.capabilities,
            "input_kinds": cls.input_kinds,
            "output_kinds": cls.output_kinds,
        }
