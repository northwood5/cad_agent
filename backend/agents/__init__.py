# -*- coding: utf-8 -*-
"""
Multi-agent package for the CAx platform.

- ``llm_factory``  : shared ChatModel builder (build_model, SUPPORTED_PROVIDERS)
- ``base``         : SpecialistAgent abstraction every domain agent implements
- ``registry``     : name -> specialist class lookup used by the orchestrator
- ``cad`` / ``mesh`` / ``cae`` : domain specialist packages
"""
from .llm_factory import build_model, SUPPORTED_PROVIDERS  # noqa: F401
