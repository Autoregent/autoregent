import json
import logging
from datetime import datetime, timezone

from fastapi import Response
from pydantic import ValidationError as PydanticValidationError

from .circuit import CircuitRegistry
from .config import AutoregentConfig
from .diagnosis import DriftDiagnosis
from .dispatch import dispatch_upstream
from .events import EventStore, HealEvent
from .diagnosers.base import Diagnoser
from .heal_executor import apply_field_mapping, validate_healed_payload
from .logging_config import log_event
from .rules import RouteClass, RouteRules
from .signing import sign_event
from .transaction_context import TransactionContext

logger = logging.getLogger("autoregent.heal")


def _parse_json(content: bytes) -> dict | list | None:
    if not content:
        return None
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return None


def _extract_fallback_target(content: bytes) -> str | None:
    payload = _parse_json(content)
    if not isinstance(payload, dict):
        return None
    target = payload.get("fallback_target")
    return target if isinstance(target, str) else None


def _loud_response(ctx: TransactionContext, status: int, content: bytes, headers: dict, *, synthesize: bool) -> Response:
    response_headers = dict(headers)
    if ctx.idempotency_key:
        response_headers["Idempotency-Key"] = ctx.idempotency_key

    if not synthesize:
        # The upstream failure was already loud (5xx/timeout) -- show the real thing.
        return Response(content=content, status_code=status, headers=response_headers)

    # The upstream returned 200 with a drifted body. Passing that through would be
    # exactly the silent failure this library exists to prevent, so we replace it
    # with a loud error instead of the deceptive success.
    response_headers["content-type"] = "application/json"
    body = json.dumps({
        "error": "schema_drift_unresolved",
        "trace_id": ctx.trace_id,
        "detail": "response diverged from the expected schema and could not be healed",
    }).encode()
    return Response(content=body, status_code=502, headers=response_headers, media_type="application/json")


class HealPipeline:
    """Owns nothing global: bound to one config, one rule set, one circuit
    registry, one event store. Everything that used to be a module-level
    singleton is now just an attribute here, so two HealPipeline instances
    (e.g. in tests, or two Autoregent instances in one process) never share
    state by accident."""

    def __init__(
        self,
        config: AutoregentConfig,
        rules: RouteRules,
        circuits: CircuitRegistry,
        events: EventStore,
        diagnoser: Diagnoser | None = None,
    ) -> None:
        self.config = config
        self.rules = rules
        self.circuits = circuits
        self.events = events
        # None means no provider is configured at all. Every drift then fails
        # loud -- a supported mode, not an error state.
        self.diagnoser = diagnoser

    def validate_against_schema(self, path: str, content: bytes) -> str | None:
        """Returns a description of the drift, or None if the payload matches
        the expected shape (or no shape is registered for this route)."""
        schema = self.rules.schema_for(path)
        if schema is None:
            return None
        if not content:
            return "response body was empty"
        try:
            # Strict + JSON mode: a stringified float ("542.1") is still rejected
            # since JSON has a native number type, but an ISO datetime string is
            # accepted since JSON has no native datetime type. model_validate()
            # on a plain dict would instead coerce both -- silently swallowing
            # exactly the kind of type drift this gate exists to catch.
            schema.model_validate_json(content, strict=True)
        except PydanticValidationError as exc:
            return str(exc)
        return None

    def _record_event(
        self,
        ctx: TransactionContext,
        outcome: str,
        original_content: bytes | dict | list,
        *,
        failure_reason: str | None = None,
        is_transport_failure: bool = False,
        diagnosis: DriftDiagnosis | None = None,
        healed_payload: dict | None = None,
    ) -> HealEvent:
        original_payload = (
            original_content if isinstance(original_content, (dict, list)) else _parse_json(original_content)
        )
        event = HealEvent(
            trace_id=ctx.trace_id,
            timestamp=datetime.now(timezone.utc),
            route=ctx.route,
            route_class=ctx.route_class.value,
            outcome=outcome,
            failure_reason=failure_reason,
            is_transport_failure=is_transport_failure,
            original_payload=original_payload,
            healed_payload=healed_payload,
            diagnosis=diagnosis,
            call_stack=list(ctx.call_stack),
            heal_count=ctx.heal_count,
            signature=None,
        )
        event.signature = sign_event(self.config.hmac_secret, event)
        self.events.append(event)
        log_event(
            logger, logging.WARNING, "heal_event",
            outcome=outcome, trace_id=ctx.trace_id, route=ctx.route,
            route_class=ctx.route_class.value, heal_count=ctx.heal_count,
            call_stack=list(ctx.call_stack),
        )
        return event

    async def handle_circuit_open(self, ctx: TransactionContext) -> Response:
        outcome = "budget_exhausted" if ctx.route_class == RouteClass.INFORMATIONAL else "failed_loud"
        self._record_event(ctx, outcome, b"", failure_reason="circuit_open_precheck", is_transport_failure=True)
        headers = {"content-type": "application/json"}
        if ctx.idempotency_key:
            headers["Idempotency-Key"] = ctx.idempotency_key
        body = json.dumps({"error": "circuit_open", "trace_id": ctx.trace_id, "route": ctx.route}).encode()
        return Response(content=body, status_code=503, headers=headers, media_type="application/json")

    async def handle_transactional_failure(
        self, ctx: TransactionContext, status: int, content: bytes, headers: dict
    ) -> Response:
        """Short-circuits the entire heal pipeline. No loop detector, no
        budget check, no diagnosis -- one strike trips the circuit."""
        self.circuits.get(ctx.route).trip()
        self._record_event(
            ctx, "failed_loud", content,
            failure_reason="transactional_short_circuit", is_transport_failure=True,
        )
        return _loud_response(ctx, status, content, headers, synthesize=False)

    async def handle_informational_failure(
        self, ctx: TransactionContext, status: int, content: bytes, headers: dict, *, is_transport_failure: bool,
    ) -> Response:
        circuit = self.circuits.get(ctx.route)

        fallback_target = _extract_fallback_target(content)
        if fallback_target is not None and not ctx.push(fallback_target):
            self._record_event(
                ctx, "loop_suppressed", content,
                failure_reason="loop_detected", is_transport_failure=is_transport_failure,
            )
            return _loud_response(ctx, status, content, headers, synthesize=not is_transport_failure)

        transaction_exhausted = ctx.heal_count >= self.config.max_heals_per_transaction
        if transaction_exhausted or circuit.is_window_exhausted():
            circuit.trip()
            reason = "budget_exhausted_transaction" if transaction_exhausted else "budget_exhausted_window"
            self._record_event(
                ctx, "budget_exhausted", content,
                failure_reason=reason, is_transport_failure=is_transport_failure,
            )
            return _loud_response(ctx, status, content, headers, synthesize=not is_transport_failure)

        ctx.heal_count += 1
        circuit.record_heal()

        if fallback_target is not None:
            result = await dispatch_upstream(self.config, fallback_target, "GET", {}, b"", {})
            if result.ok:
                return Response(content=result.content, status_code=result.status_code, headers=result.headers)
            return await self.handle_informational_failure(
                ctx, result.status_code, result.content, result.headers, is_transport_failure=True,
            )

        if is_transport_failure:
            # A real upstream failure (5xx/timeout) -- there's no payload shape to
            # diagnose or remap, only a genuine error to show.
            self._record_event(
                ctx, "failed_loud", content,
                failure_reason="transport_failure_no_diagnosis", is_transport_failure=True,
            )
            return _loud_response(ctx, status, content, headers, synthesize=False)

        return await self._attempt_ai_heal(ctx, status, content, headers)

    async def _attempt_ai_heal(self, ctx: TransactionContext, status: int, content: bytes, headers: dict) -> Response:
        """Gemini can authorise a heal; it can never force one through. Every exit
        from an uncertain diagnosis is a loud failure."""
        schema = self.rules.schema_for(ctx.route)
        original_payload = _parse_json(content)

        if schema is None or not isinstance(original_payload, dict):
            self._record_event(ctx, "failed_loud", content, failure_reason="no_expected_schema")
            return _loud_response(ctx, status, content, headers, synthesize=True)

        validation_error = self.validate_against_schema(ctx.route, content) or "response diverged from the expected schema"

        diagnosis = (
            await self.diagnoser.diagnose(ctx.route, schema.model_json_schema(), validation_error, original_payload)
            if self.diagnoser is not None
            else None
        )

        if diagnosis is None:
            self._record_event(ctx, "failed_loud", original_payload, failure_reason="diagnoser_unavailable")
            return _loud_response(ctx, status, content, headers, synthesize=True)

        if (
            diagnosis.recommendation != "heal"
            or diagnosis.confidence < self.config.confidence_threshold
            or diagnosis.drift_type == "unrecoverable"
        ):
            log_event(
                logger, logging.INFO, "heal_declined",
                route=ctx.route, recommendation=diagnosis.recommendation,
                confidence=diagnosis.confidence, drift_type=diagnosis.drift_type,
            )
            self._record_event(ctx, "failed_loud", original_payload, failure_reason="diagnosis_declined", diagnosis=diagnosis)
            return _loud_response(ctx, status, content, headers, synthesize=True)

        healed_payload = apply_field_mapping(diagnosis.field_mapping, original_payload)
        if healed_payload is None:
            log_event(logger, logging.WARNING, "heal_executor_missing_source", route=ctx.route)
            self._record_event(
                ctx, "failed_loud", original_payload,
                failure_reason="heal_executor_missing_source", diagnosis=diagnosis,
            )
            return _loud_response(ctx, status, content, headers, synthesize=True)

        gate_error = validate_healed_payload(schema, healed_payload)
        if gate_error is not None:
            log_event(logger, logging.ERROR, "validation_gate_blocked", route=ctx.route, detail=gate_error)
            self._record_event(
                ctx, "failed_loud", original_payload,
                failure_reason="validation_gate_blocked", diagnosis=diagnosis,
            )
            return _loud_response(ctx, status, content, headers, synthesize=True)

        event = self._record_event(ctx, "healed", original_payload, diagnosis=diagnosis, healed_payload=healed_payload)
        log_event(
            logger, logging.ERROR, "divergence_alert",
            trace_id=ctx.trace_id, route=ctx.route, drift_type=diagnosis.drift_type,
            confidence=diagnosis.confidence, signature=event.signature,
        )
        response_headers = {
            "content-type": "application/json",
            "X-Autoregent-Healed": "true",
            "X-Autoregent-Trace-Id": ctx.trace_id,
            "X-Autoregent-Drift-Type": diagnosis.drift_type,
            "X-Autoregent-Confidence": str(diagnosis.confidence),
            "X-Autoregent-Signature": event.signature,
        }
        if ctx.idempotency_key:
            response_headers["Idempotency-Key"] = ctx.idempotency_key
        return Response(
            content=json.dumps(healed_payload).encode(),
            status_code=200,
            headers=response_headers,
            media_type="application/json",
        )
