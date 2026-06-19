# -*- coding: utf-8 -*-
"""
Agent registry — maps a specialist's ``name`` to its class.

The orchestrator consults :func:`describe_agents` when planning a workflow
and :func:`get_specialist_cls` when instantiating an agent for a node.
Adding a new domain agent is a one-line registration here.
"""
from __future__ import annotations

from typing import Any

from .base import SpecialistAgent
from .cad.agent import CADSpecialist
from .mesh.agent import MeshSpecialist
from .cae.agent import CAESpecialist
from .post.agent import PostSpecialist

# name -> SpecialistAgent subclass
AGENT_REGISTRY: dict[str, type[SpecialistAgent]] = {
    CADSpecialist.name: CADSpecialist,
    MeshSpecialist.name: MeshSpecialist,
    CAESpecialist.name: CAESpecialist,
    PostSpecialist.name: PostSpecialist,
}


def get_specialist_cls(name: str) -> type[SpecialistAgent] | None:
    return AGENT_REGISTRY.get(name)


def describe_agents() -> list[dict[str, Any]]:
    """Capability metadata for every registered agent (for the planner prompt)."""
    return [cls.describe() for cls in AGENT_REGISTRY.values()]


def available_agent_names() -> list[str]:
    return list(AGENT_REGISTRY.keys())
