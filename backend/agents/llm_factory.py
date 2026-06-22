# -*- coding: utf-8 -*-
"""
Shared LLM model factory for all agents (orchestrator + specialists).

Builds an AgentScope 2.x ChatModelBase from a plain-dict config. Extracted
from the original cad_agent module so every agent in the platform constructs
its model the same way.
"""
from __future__ import annotations

from typing import Any

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

SUPPORTED_PROVIDERS = ["openai", "anthropic", "dashscope", "ollama", "deepseek", "zhipu"]

# Zhipu AI (GLM) uses an OpenAI-compatible API.
_ZHIPU_BASE_URL = "https://open.bigmodel.cn/api/paas/v4/"


def build_model(config: dict[str, Any]) -> ChatModelBase:
    """
    Build an AgentScope ChatModelBase from a plain-dict config.

    Config fields:
        provider  : one of SUPPORTED_PROVIDERS
        model_name: e.g. "gpt-4o", "claude-sonnet-4-6", "GLM-5", "qwen-plus"
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

    elif provider == "zhipu":
        # Zhipu AI (GLM series) exposes an OpenAI-compatible chat endpoint.
        effective_base = base_url or _ZHIPU_BASE_URL
        cred = OpenAICredential(api_key=api_key, base_url=effective_base)
        return OpenAIChatModel(credential=cred, model=model_name, stream=stream)

    else:
        raise ValueError(
            f"Unknown provider '{provider}'. Supported: {SUPPORTED_PROVIDERS}"
        )
