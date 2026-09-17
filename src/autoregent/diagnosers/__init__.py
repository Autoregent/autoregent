"""Diagnosis providers.

`Diagnoser` is the seam: implement one method and Autoregent will use your
provider instead of the built-in ones.

    from autoregent import Autoregent, AutoregentConfig
    from autoregent.diagnosers import AnthropicDiagnoser

    gateway = Autoregent(
        config=AutoregentConfig(upstream_base_url="https://api.internal"),
        diagnoser=AnthropicDiagnoser(api_key="sk-ant-..."),
    )

Gemini is the default (its SDK is a required dependency). OpenAI and Anthropic
live behind optional extras, but importing them here is always safe -- each
adapter imports its SDK lazily, inside `diagnose`.
"""

from .anthropic import AnthropicDiagnoser
from .base import Diagnoser, WireDriftDiagnosis, build_prompt
from .gemini import GeminiDiagnoser
from .openai import OpenAIDiagnoser

__all__ = [
    "Diagnoser",
    "GeminiDiagnoser",
    "OpenAIDiagnoser",
    "AnthropicDiagnoser",
    "WireDriftDiagnosis",
    "build_prompt",
]
