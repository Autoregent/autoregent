import asyncio
import logging

from ..diagnosis import DriftDiagnosis
from ..logging_config import log_event
from .base import Diagnoser, WireDriftDiagnosis, build_prompt

logger = logging.getLogger("autoregent.diagnoser.gemini")


class GeminiDiagnoser(Diagnoser):
    """Gemini via the `google-genai` SDK, using native structured output.

    This is the default diagnoser -- `google-genai` is a required dependency,
    so it works out of the box with just an API key. `gemini-flash-lite-latest`
    is the default model because the diagnosis call sits in the request path
    against a ~3s budget: `gemini-flash-latest` measured 14s+ and returned
    503s under load, which is unusable here.
    """

    def __init__(
        self,
        api_key: str | None,
        model: str = "gemini-flash-lite-latest",
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

        from google import genai
        from google.genai import types

        prompt = build_prompt(route, expected_schema, validation_error, original_payload)
        log_event(logger, logging.INFO, "diagnoser_request", provider="gemini", route=route, prompt=prompt)

        client = genai.Client(api_key=self._api_key)

        try:
            response = await asyncio.wait_for(
                client.aio.models.generate_content(
                    model=self._model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=WireDriftDiagnosis,
                    ),
                ),
                timeout=self._timeout_seconds,
            )
        except TimeoutError:
            log_event(logger, logging.WARNING, "diagnoser_timeout", provider="gemini", route=route, timeout_seconds=self._timeout_seconds)
            return None
        except Exception as exc:
            log_event(logger, logging.ERROR, "diagnoser_error", provider="gemini", route=route, error=str(exc))
            return None

        raw_text = response.text
        log_event(logger, logging.INFO, "diagnoser_response", provider="gemini", route=route, raw=raw_text)

        if raw_text is None:
            log_event(logger, logging.ERROR, "diagnoser_malformed_response", provider="gemini", route=route, error="empty response")
            return None

        try:
            return WireDriftDiagnosis.model_validate_json(raw_text).to_diagnosis()
        except Exception as exc:
            log_event(logger, logging.ERROR, "diagnoser_malformed_response", provider="gemini", route=route, error=str(exc))
            return None
