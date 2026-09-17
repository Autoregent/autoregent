import asyncio
import logging

from ..diagnosis import DriftDiagnosis
from ..logging_config import log_event
from .base import Diagnoser, WireDriftDiagnosis, build_prompt

logger = logging.getLogger("autoregent.diagnoser.anthropic")

_TOOL_NAME = "report_diagnosis"


class AnthropicDiagnoser(Diagnoser):
    """Claude via the `anthropic` SDK, using a forced tool call for structured
    output (Claude's tool `input_schema` is the structured-output mechanism).

    Requires the optional extra:

        pip install "autoregent[anthropic]"

    The SDK is imported lazily inside `diagnose`, so importing this module
    never requires the package to be installed -- only actually running a
    diagnosis does.
    """

    def __init__(
        self,
        api_key: str | None,
        model: str = "claude-haiku-4-5-20251001",
        timeout_seconds: float = 3.0,
        max_tokens: int = 1024,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._max_tokens = max_tokens

    async def diagnose(
        self, route: str, expected_schema: dict, validation_error: str, original_payload: dict
    ) -> DriftDiagnosis | None:
        if not self._api_key:
            log_event(logger, logging.WARNING, "diagnoser_skipped", reason="no_api_key", route=route)
            return None

        try:
            from anthropic import AsyncAnthropic
        except ImportError as exc:  # a deployment mistake, not a runtime condition
            raise ImportError(
                'AnthropicDiagnoser requires the "anthropic" package. Install it with: pip install "autoregent[anthropic]"'
            ) from exc

        prompt = build_prompt(route, expected_schema, validation_error, original_payload)
        log_event(logger, logging.INFO, "diagnoser_request", provider="anthropic", route=route, prompt=prompt)

        client = AsyncAnthropic(api_key=self._api_key)

        try:
            message = await asyncio.wait_for(
                client.messages.create(
                    model=self._model,
                    max_tokens=self._max_tokens,
                    messages=[{"role": "user", "content": prompt}],
                    tools=[
                        {
                            "name": _TOOL_NAME,
                            "description": "Report the schema drift diagnosis.",
                            "input_schema": WireDriftDiagnosis.model_json_schema(),
                        }
                    ],
                    tool_choice={"type": "tool", "name": _TOOL_NAME},
                ),
                timeout=self._timeout_seconds,
            )
        except TimeoutError:
            log_event(logger, logging.WARNING, "diagnoser_timeout", provider="anthropic", route=route, timeout_seconds=self._timeout_seconds)
            return None
        except Exception as exc:
            log_event(logger, logging.ERROR, "diagnoser_error", provider="anthropic", route=route, error=str(exc))
            return None

        tool_use = next((block for block in message.content if getattr(block, "type", None) == "tool_use"), None)
        log_event(logger, logging.INFO, "diagnoser_response", provider="anthropic", route=route, raw=str(getattr(tool_use, "input", None)))

        if tool_use is None:
            log_event(logger, logging.ERROR, "diagnoser_malformed_response", provider="anthropic", route=route, error="no tool_use block")
            return None

        try:
            return WireDriftDiagnosis.model_validate(tool_use.input).to_diagnosis()
        except Exception as exc:
            log_event(logger, logging.ERROR, "diagnoser_malformed_response", provider="anthropic", route=route, error=str(exc))
            return None
