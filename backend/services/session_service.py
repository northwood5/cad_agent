# -*- coding: utf-8 -*-
"""
Per-project session management.

A :class:`ProjectSession` lazily instantiates specialist agents (CAD, and
later mesh/CAE) for one project and keeps them alive so iterative edits retain
their geometry/state. :class:`SessionManager` is the process-wide registry,
replacing the old session dict in main.py.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from agents.base import SpecialistAgent
from agents.registry import get_specialist_cls

logger = logging.getLogger(__name__)


class ProjectSession:
    def __init__(self, project_id: int, llm_config: dict[str, Any], workspace: Path) -> None:
        self.project_id = project_id
        self.llm_config = llm_config
        self.workspace = workspace
        self.workspace.mkdir(parents=True, exist_ok=True)
        self._specialists: dict[str, SpecialistAgent] = {}

    def get_specialist(self, name: str) -> SpecialistAgent | None:
        if name not in self._specialists:
            cls = get_specialist_cls(name)
            if cls is None:
                return None
            self._specialists[name] = cls(self.llm_config, self.workspace / name)
            logger.info("Instantiated specialist '%s' for project %s", name, self.project_id)
        return self._specialists[name]

    @property
    def cad_scene(self):
        """Convenience accessor for export endpoints (None if CAD not used yet)."""
        spec = self._specialists.get("cad")
        return getattr(spec, "scene", None)


class SessionManager:
    def __init__(self) -> None:
        self._sessions: dict[int, ProjectSession] = {}

    def get_or_create(
        self, project_id: int, llm_config: dict[str, Any], workspace: Path
    ) -> ProjectSession:
        sess = self._sessions.get(project_id)
        if sess is None:
            sess = ProjectSession(project_id, llm_config, workspace)
            self._sessions[project_id] = sess
        return sess

    def get(self, project_id: int) -> ProjectSession | None:
        return self._sessions.get(project_id)

    def drop(self, project_id: int) -> None:
        self._sessions.pop(project_id, None)

    def clear(self) -> None:
        """Drop all sessions (e.g. after an LLM config change)."""
        self._sessions.clear()
