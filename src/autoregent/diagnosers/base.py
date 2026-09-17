import json
from abc import ABC, abstractmethod
from typing import Literal

from pydantic import BaseModel

from ..diagnosis import DriftDiagnosis


class Diagnoser(ABC):
    """The provider seam. One method, one contract: look at a drifted payload
    and either authorise a heal or decline.

    Returning `None` is always safe -- it means "no usable diagnosis", and the
    pipeline fails loud. Every failure mode an implementation can hit (missing
    credentials, timeout, transport error, a response that doesn't satisfy the
    expected shape) must be converted into `None` rather than raised, so that
    an unreachable provider degrades into a loud failure instead of a 500.

    The one exception is a missing SDK: that's a deployment mistake, not a
    runtime condition, so the built-in adapters raise ImportError with the
    extra to install.
    """

    @abstractmethod
    async def diagnose(
        self, route: str, expected_schema: dict, validation_error: str, original_payload: dict
    ) -> DriftDiagnosis | None:
        """Diagnose the drift, or return None to force a loud failure."""


class _FieldMappingEntry(BaseModel):
    expected_field: str
    source_field: str


class WireDriftDiagnosis(BaseModel):
    """The shape actually requested over the wire, shared by every adapter.

    `field_mapping` is a list of pairs rather than the `dict[str, str]` that
    `DriftDiagnosis` exposes: strict structured-output modes reject schemas
    containing `additionalProperties`, which is exactly what Pydantic emits for
    an open-ended dict. The Gemini Developer API rejects it outright (Vertex AI
    Enterprise mode does not), and OpenAI's strict json_schema mode has the
    same constraint. Converted back into a real dict after parsing.
    """

    drift_type: Literal["field_rename", "type_change", "nesting_change", "missing_field", "unrecoverable"]
    recommendation: Literal["heal", "fail_loud"]
    confidence: float
    field_mapping: list[_FieldMappingEntry]
    reasoning: str

    def to_diagnosis(self) -> DriftDiagnosis:
        return DriftDiagnosis(
            drift_type=self.drift_type,
            recommendation=self.recommendation,
            confidence=self.confidence,
            field_mapping={e.expected_field: e.source_field for e in self.field_mapping},
            reasoning=self.reasoning,
        )


def build_prompt(route: str, expected_schema: dict, validation_error: str, original_payload: dict) -> str:
    """Provider-agnostic prompt. Kept identical across adapters so that
    switching providers changes the model, not the instructions."""
    return (
        "An API gateway received a response that failed validation against its expected schema. "
        "Diagnose the drift and decide whether it is safely healable by pure field remapping -- "
        "never by inventing or defaulting a value.\n\n"
        f"Route: {route}\n\n"
        f"Expected JSON schema:\n{json.dumps(expected_schema, indent=2)}\n\n"
        f"Actual response payload:\n{json.dumps(original_payload, indent=2)}\n\n"
        f"Pydantic validation error:\n{validation_error}\n\n"
        "field_mapping must have one entry per field in the expected schema: expected_field is "
        "exactly that field's name, source_field is the exact key in the actual payload where its "
        "value can be found. Include an entry only if the value genuinely exists in the actual "
        "payload. If any required field has no source, or the drift cannot be resolved by pure "
        'remapping, set drift_type to "unrecoverable" and recommendation to "fail_loud".'
    )
