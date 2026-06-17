# -*- coding: utf-8 -*-
"""
Builds and returns an AgentScope 2.x Agent configured for CAD modeling.
Supports OpenAI, Anthropic, DashScope (Qwen), Ollama, and DeepSeek.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from agentscope.agent import Agent
from agentscope.model import (
    ChatModelBase,
    OpenAIChatModel,
    AnthropicChatModel,
    DashScopeChatModel,
    OllamaChatModel,
    DeepSeekChatModel,
)
from agentscope.credential import (
    OpenAICredential,
    AnthropicCredential,
    DashScopeCredential,
    OllamaCredential,
    DeepSeekCredential,
)
from agentscope.tool import Toolkit

from .tools.cad_engine import CADScene
from .tools.cad_tools import build_cad_toolkit

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are an expert CAD modeling assistant powered by a Python-based geometry engine.
Your role is to translate the user's natural language descriptions into precise 3-D CAD models by
calling the provided tools step by step.

## Available Tools
- **create_primitive** – Create box, cylinder, sphere, cone, or torus
- **boolean_operation** – union / difference / intersection of two shapes
- **transform_shape** – Translate, rotate, or scale an existing shape
- **extrude_polygon** – Extrude a 2-D polygon profile into a 3-D solid
- **list_shapes** – Inspect all shapes currently in the scene
- **export_model** – Export the finished model as STL for 3-D preview
- **reset_scene** – Clear the scene and start over

## Workflow
1. **Understand** – parse the user's request for geometry, dimensions, and intent.
2. **Plan** – mentally decompose the model into primitives and operations. If dimensions are unspecified, infer sensible defaults (e.g. 10 mm cube, M5 through-hole has radius 2.5 mm).
3. **Execute** – call tools one by one; use descriptive names like "main_body", "drill_hole", "left_arm".
4. **Verify** – call `list_shapes` if you need to inspect the scene state.
5. **Export** – always call `export_model` as the final step so the user can see the result.
6. **Describe** – briefly tell the user what was built and offer next steps.

## Coordinate System
- Right-hand XYZ; Z is up.
- All dimensions in millimetres unless the user specifies otherwise.
- trimesh centres primitives at the origin by default.

## Boolean Operations Tip
When drilling a hole, the cutting tool must be larger or protrude beyond the base shape to avoid
surface artefacts. Translate the cutting shape to the correct position **before** calling
`boolean_operation` with `difference`.

## Iteration
If the user asks to modify the existing model, call `list_shapes` first, then apply only the
necessary operations (do not reset unless explicitly asked).
"""

# ---------------------------------------------------------------------------
# Model factory
# ---------------------------------------------------------------------------

SUPPORTED_PROVIDERS = ["openai", "anthropic", "dashscope", "ollama", "deepseek"]


def build_model(config: dict[str, Any]) -> ChatModelBase:
    """
    Build an AgentScope ChatModelBase from a plain-dict config.

    Config fields:
        provider  : one of SUPPORTED_PROVIDERS
        model_name: e.g. "gpt-4o", "claude-sonnet-4-6", "qwen-plus"
        api_key   : (not needed for Ollama)
        base_url  : optional override (OpenAI-compatible endpoints, Ollama host, …)
        stream    : bool (default True)
    """
    provider = config.get("provider", "openai").lower()
    model_name: str = config["model_name"]
    api_key: str = config.get("api_key", "")
    base_url: str | None = config.get("base_url") or None
    stream: bool = config.get("stream", True)

    if provider == "openai":
        cred = OpenAICredential(api_key=api_key, base_url=base_url)
        return OpenAIChatModel(credential=cred, model=model_name, stream=stream)

    elif provider == "anthropic":
        cred = AnthropicCredential(api_key=api_key, base_url=base_url)
        return AnthropicChatModel(credential=cred, model=model_name, stream=stream)

    elif provider == "dashscope":
        cred = DashScopeCredential(api_key=api_key)
        return DashScopeChatModel(credential=cred, model=model_name, stream=stream)

    elif provider == "ollama":
        host = base_url or "http://localhost:11434"
        cred = OllamaCredential(host=host)
        return OllamaChatModel(credential=cred, model=model_name, stream=stream)

    elif provider == "deepseek":
        cred = DeepSeekCredential(api_key=api_key)
        return DeepSeekChatModel(credential=cred, model=model_name, stream=stream)

    else:
        raise ValueError(
            f"Unknown provider '{provider}'. Supported: {SUPPORTED_PROVIDERS}"
        )


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
    )
    return agent, scene
