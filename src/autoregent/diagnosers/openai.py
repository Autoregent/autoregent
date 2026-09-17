import asyncio
import logging

from ..diagnosis import DriftDiagnosis
from ..logging_config import log_event
from .base import Diagnoser, WireDriftDiagnosis, build_prompt

logger = logging.getLogger("autoregent.diagnoser.openai")


class OpenAIDiagnoser(Diagnoser):
    """OpenAI via the `openai` SDK, using strict structured output.

    Requires the optional extra:

        pip install "autoregent[openai]"

    The SDK is imported lazily inside `diagnose`, so importing this module
    never requires the package to be installed -- only actually running a
    diagnosis does.
    """

    def __init__(
        self,
        api_key: str | None,
        model: str = "gpt-4o-mini",
        timeout_seconds: float = 3.0,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._timeout_seconds = timeout_seconds

    async def diagnose(
        self, route: str, expected_schema: dict, validation_error: str, original_payload: dict
    ) -> DriftDiagnosis | None:
        if not self._api_key:
            log_event(logger, logging.WARNING, "diagnoser_skipped", reason="no_api_key", route=route)
            return None

        try:
            from openai import AsyncOpenAI
        except ImportError as exc:  # a deployment mistake, not a runtime condition
            raise ImportError(
                'OpenAIDiagnoser requires the "openai" package. Install it with: pip install "autoregent[openai]"'
            ) from exc

        prompt = build_prompt(route, expected_schema, validation_error, original_payload)
        log_event(logger, logging.INFO, "diagnoser_request", provider="openai", route=route, prompt=prompt)

        client = AsyncOpenAI(api_key=self._api_key)

        try:
            completion = await asyncio.wait_for(
                client.chat.completions.parse(
                    model=self._model,
                    messages=[{"role": "user", "content": prompt}],
                    response_format=WireDriftDiagnosis,
                ),
                timeout=self._timeout_seconds,
            )
        except TimeoutError:
            log_event(logger, logging.WARNING, "diagnoser_timeout", provider="openai", route=route, timeout_seconds=self._timeout_seconds)
            return None
        except Exception as exc:
            log_event(logger, logging.ERROR, "diagnoser_error", provider="openai", route=route, error=str(exc))
            return None

        parsed = completion.choices[0].message.parsed
        log_event(logger, logging.INFO, "diagnoser_response", provider="openai", route=route, raw=str(parsed))

        if parsed is None:
            # A refusal, or a response the SDK couldn't coerce into the schema.
            log_event(logger, logging.ERROR, "diagnoser_malformed_response", provider="openai", route=route, error="no parsed content")
            return None

        try:
            return parsed.to_diagnosis()
        except Exception as exc:
            log_event(logger, logging.ERROR, "diagnoser_malformed_response", provider="openai", route=route, error=str(exc))
            return None
