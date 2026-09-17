import logging
import time

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from .circuit import CircuitRegistry, CircuitState
from .config import AutoregentConfig
from .diagnosers.base import Diagnoser
from .diagnosers.gemini import GeminiDiagnoser
from .dispatch import dispatch_upstream
from .events import EventStore, HealEvent
from .heal_pipeline import HealPipeline
from .logging_config import configure_logging, log_event
from .rules import RouteClass, RouteRules
from .transaction_context import TransactionContext

__version__ = "0.1.0"


def _default_diagnoser(config: AutoregentConfig) -> Diagnoser | None:
    """Gemini is the zero-config default, but only when a key is actually
    present. No key means no diagnoser at all, which means every drift fails
    loud rather than silently passing through."""
    if not config.gemini_api_key:
        return None
    return GeminiDiagnoser(
        api_key=config.gemini_api_key,
        model=config.gemini_model,
        timeout_seconds=config.gemini_timeout_seconds,
    )


class Autoregent:
    """The gateway itself, as an importable, instantiable object.

        from autoregent import Autoregent, AutoregentConfig, RouteRules
        from myapp.schemas import AccountBalance

        gateway = Autoregent(
            config=AutoregentConfig(gemini_api_key="...", upstream_base_url="https://api.internal"),
            rules=RouteRules().transactional("*/transfer/*").expect("accounts/*", AccountBalance),
        )

        # Mount into an existing FastAPI app:
        app.mount("/gateway", gateway.app)

        # ...or run it standalone:
        # uvicorn.run(gateway.app)

    Every Autoregent instance owns its own circuit registry and event store --
    nothing here is module-level global state, so multiple instances (or
    multiple test runs) never bleed into each other.
    """

    def __init__(
        self,
        config: AutoregentConfig | None = None,
        rules: RouteRules | None = None,
        diagnoser: Diagnoser | None = None,
        *,
        cors_allow_origins: list[str] | None = None,
    ) -> None:
        self.config = config or AutoregentConfig()
        self.rules = rules or RouteRules()
        self.diagnoser = diagnoser if diagnoser is not None else _default_diagnoser(self.config)
        self.circuits = CircuitRegistry(self.config)
        self.events = EventStore()
        self._pipeline = HealPipeline(self.config, self.rules, self.circuits, self.events, self.diagnoser)

        configure_logging(self.config.log_level)
        self._logger = logging.getLogger("autoregent")

        self.app = self._build_app(cors_allow_origins)

    def _build_app(self, cors_allow_origins: list[str] | None) -> FastAPI:
        app = FastAPI(title="Autoregent Gateway", version=__version__)

        if cors_allow_origins:
            # /health and /events are read-only -- if you're driving a dashboard
            # from a different origin (e.g. a static site), it needs the browser
            # to allow the cross-origin GET. Off by default: only opt in to the
            # origins you actually trust.
            app.add_middleware(
                CORSMiddleware,
                allow_origins=cors_allow_origins,
                allow_methods=["GET"],
                allow_headers=["*"],
            )

        @app.middleware("http")
        async def request_logger(request: Request, call_next):
            start = time.monotonic()
            response = await call_next(request)
            duration_ms = round((time.monotonic() - start) * 1000, 1)
            log_event(
                self._logger, logging.INFO, "request",
                method=request.method, path=request.url.path,
                status_code=response.status_code, duration_ms=duration_ms,
            )
            return response

        @app.api_route("/proxy/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
        async def proxy(path: str, request: Request):
            return await self._handle_proxy(path, request)

        @app.get("/health")
        async def health():
            return self._health_payload()

        @app.get("/events", response_model=list[HealEvent])
        async def get_events():
            return self.events.all()

        return app

    def _health_payload(self) -> dict:
        return {
            "status": "ok",
            "service": "autoregent-gateway",
            "circuits": self.circuits.snapshot(),
            "budget_config": {
                "max_heals_per_transaction": self.config.max_heals_per_transaction,
                "rolling_window_seconds": self.config.rolling_window_seconds,
                "rolling_window_max_heals": self.config.rolling_window_max_heals,
                "circuit_cooldown_seconds": self.config.circuit_cooldown_seconds,
            },
        }

    async def _handle_proxy(self, path: str, request: Request) -> Response:
        route_class = self.rules.classify(path)
        ctx = TransactionContext(
            route=path,
            route_class=route_class,
            idempotency_key=request.headers.get("idempotency-key"),
        )
        circuit = self.circuits.get(path)

        if not circuit.allow_request():
            return await self._pipeline.handle_circuit_open(ctx)

        was_half_open = circuit.state == CircuitState.HALF_OPEN

        ctx.push(path)
        body = await request.body()
        result = await dispatch_upstream(self.config, path, request.method, dict(request.headers), body, request.query_params)

        if result.ok:
            drift = (
                self._pipeline.validate_against_schema(path, result.content)
                if route_class == RouteClass.INFORMATIONAL
                else None
            )

            if drift is None:
                # Genuinely clean: only this counts as probe success. A 200 with
                # drifted content must NOT close the circuit early.
                if was_half_open:
                    circuit.record_probe_result(success=True)
                return Response(content=result.content, status_code=result.status_code, headers=result.headers)

            if was_half_open:
                circuit.record_probe_result(success=False)
            log_event(logging.getLogger("autoregent.proxy"), logging.WARNING, "schema_drift_detected", route=path, detail=drift)
            return await self._pipeline.handle_informational_failure(
                ctx, result.status_code, result.content, result.headers, is_transport_failure=False,
            )

        if was_half_open:
            circuit.record_probe_result(success=False)

        if route_class == RouteClass.TRANSACTIONAL:
            return await self._pipeline.handle_transactional_failure(ctx, result.status_code, result.content, result.headers)

        return await self._pipeline.handle_informational_failure(
            ctx, result.status_code, result.content, result.headers, is_transport_failure=True,
        )
